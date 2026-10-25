"""检测记录分页读取：内存占用只与单条记录有关，与任务跑了多久无关。

运行中的记录写在 ``.inspection.records``（JSON Lines，只追加），收尾时才合并成
``inspection.json`` 的 ``records`` 数组。两种格式形态不同，但分页需求相同：

- 大文件不能 ``json.load`` 进内存（8 路跑 24h 是几十万条、几百 MB）；
- 分页只取窗口内的 ``limit`` 条，总量单独统计并在文件未变时复用。

因此这里用 ``json.JSONDecoder.raw_decode`` 做流式解析：读一块、解析一条、丢一块。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator, TextIO

CHUNK = 1 << 16
#: 顶层记录数组的键。写侧固定输出 ``"records"``（不带空格），这里容忍空格。
_KEY = '"records"'
_MARKER = '"metadata"'
_TOTAL_CACHE: "OrderedDict[tuple[str, int, int], int]" = OrderedDict()
_CACHE_SIZE = 8


def _open_document(handle: TextIO) -> tuple[str, int] | None:
    """定位顶层 ``records`` 数组的起始下标；不是 JSON 文档时返回 None。"""
    buffer = ""
    searched = 0
    while True:
        found = buffer.find(_KEY, searched)
        if found >= 0:
            cursor = found + len(_KEY)
            while True:
                while cursor < len(buffer) and buffer[cursor] in " \t\r\n:":
                    cursor += 1
                if cursor < len(buffer):
                    break
                chunk = handle.read(CHUNK)
                if not chunk:
                    return None
                buffer += chunk
            if buffer[cursor] == "[":
                return buffer, cursor + 1
            searched = found + len(_KEY)
            continue
        chunk = handle.read(CHUNK)
        if not chunk:
            return None
        searched = max(0, len(buffer) - len(_KEY))
        buffer += chunk


def _iter_document(handle: TextIO) -> Iterator[dict[str, Any]]:
    opened = _open_document(handle)
    if opened is None:
        return
    buffer, index = opened
    decoder = json.JSONDecoder()
    while True:
        while index < len(buffer) and buffer[index] in " \t\r\n,":
            index += 1
        while index >= len(buffer):
            chunk = handle.read(CHUNK)
            if not chunk:
                return
            buffer += chunk
            while index < len(buffer) and buffer[index] in " \t\r\n,":
                index += 1
        if buffer[index] == "]":
            return
        while True:
            try:
                value, end = decoder.raw_decode(buffer, index)
            except ValueError:
                chunk = handle.read(CHUNK)
                if not chunk:
                    return
                buffer += chunk
                continue
            break
        yield value
        index = end
        # 及时丢弃已解析的前缀，避免缓冲区随文件线性增长
        if index > CHUNK * 4:
            buffer = buffer[index:]
            index = 0


def _iter_journal(handle: TextIO) -> Iterator[dict[str, Any]]:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            # 进程被杀时最后一行可能只写了一半；跳过而不是让整页 500。
            continue
        if isinstance(value, dict):
            yield value


def iter_records(handle: TextIO) -> Iterator[dict[str, Any]]:
    """自动识别 JSON Lines 日志与完整的 ``inspection.json``。"""
    head = handle.read(CHUNK)
    if not head:
        return
    handle.seek(0)
    if _MARKER in head or _KEY in head:
        yield from _iter_document(handle)
    else:
        yield from _iter_journal(handle)


def _cache_key(path: Path) -> tuple[tuple[str, int, int], int | None]:
    metadata = path.stat()
    key = (str(path.resolve()), metadata.st_size, metadata.st_mtime_ns)
    return key, _TOTAL_CACHE.get(key)


def _remember(key: tuple[str, int, int], total: int) -> None:
    _TOTAL_CACHE[key] = total
    _TOTAL_CACHE.move_to_end(key)
    while len(_TOTAL_CACHE) > _CACHE_SIZE:
        _TOTAL_CACHE.popitem(last=False)


def count(path: str | Path) -> int:
    """记录总数；文件未变化时直接复用缓存。"""
    path = Path(path)
    if not path.is_file():
        return 0
    key, known = _cache_key(path)
    if known is not None:
        return known
    total = 0
    with path.open(encoding="utf-8") as handle:
        for _record in iter_records(handle):
            total += 1
    _remember(key, total)
    return total


def page(path: str | Path, offset: int = 0, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    """返回 ``(items, total)``；``items`` 长度不超过 ``limit``。"""
    path = Path(path)
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    if not path.is_file():
        return [], 0
    key, known_total = _cache_key(path)
    items: list[dict[str, Any]] = []
    total = 0
    with path.open(encoding="utf-8") as handle:
        for record in iter_records(handle):
            if offset <= total < offset + limit:
                items.append(record)
            total += 1
            # 文件自上次统计后没有变化，窗口取满即可用缓存的总数提前结束扫描。
            if known_total is not None and total > offset + limit:
                total = known_total
                break
    _remember(key, total)
    return items, total
