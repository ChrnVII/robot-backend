"""Bounded asynchronous JSON journal with atomic, complete snapshots.

记录写入分三层::

    add() ──► 有界队列 ──► 写盘线程 ──► .inspection.records（只追加，JSON Lines）
                                        └► inspection.json（元数据 + 计数，5s 一次）
    收尾   ──► 一次合并：journal 顺序写成 inspection.json 的 records 数组

运行期只追加、只写几百字节的元数据快照，收尾时单次顺序合并；写入量只与记录数
成正比，与任务时长无关。记录分页由 :mod:`app.robot_core.inspection.records`
直接流式读 journal，不把整份文件载入内存。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

#: 元数据快照周期（秒）。运行期快照只含元数据与计数，代价与记录数、任务时长无关。
SNAPSHOT_INTERVAL_SECONDS = 5.0
#: journal flush 周期（秒）。分页接口读的是 journal，flush 越勤数据越新。
FLUSH_INTERVAL_SECONDS = 1.0
#: 收尾合并的最长等待（秒）；磁盘挂起时不能无限期卡住 stopping。
CLOSE_TIMEOUT_SECONDS = 30.0


class InspectionOutput:
    def __init__(self, directory, metadata, *, snapshot_interval: float = SNAPSHOT_INTERVAL_SECONDS):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'inspection.json'
        self.journal = self.root / '.inspection.records'
        self.metadata = dict(metadata)
        self.error = ''
        self.count = 0
        self.snapshot_interval = max(0.5, float(snapshot_interval))
        self._queue = queue.Queue(maxsize=1024)
        self._thread = threading.Thread(target=self._run, name='inspection-json', daemon=True)
        # 第一条快照要让「任务刚启动」的读者看到合法 JSON（records 为空）。
        self._snapshot()
        self._thread.start()

    def add(self, record):
        if self.error:
            raise RuntimeError(self.error)
        try:
            self._queue.put_nowait(record)
        except queue.Full as exc:
            raise RuntimeError('检测记录写盘积压，任务已停止') from exc

    # ---------- 快照 ---------- #

    def _snapshot(self):
        """只写元数据与计数；运行期 records 恒为空数组，完整记录见 journal。"""
        temporary = self.path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as out:
            out.write('{"metadata":')
            json.dump(self.metadata, out, ensure_ascii=False)
            out.write(',"records_total":%d' % self.count)
            out.write(',"records_complete":false')
            out.write(',"records":[]')
            out.write('}')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)

    # ---------- 写盘线程 ---------- #

    def _run(self):
        last_flush = last_snapshot = time.monotonic()
        try:
            with self.journal.open('a', encoding='utf-8') as journal:
                while True:
                    try:
                        record = self._queue.get(timeout=1)
                    except queue.Empty:
                        record = {}
                    if isinstance(record, tuple):
                        # 合并前先关掉追加句柄：收尾读的是同一路径，避免读写句柄叠加。
                        journal.flush()
                        os.fsync(journal.fileno())
                        journal.close()
                        self._finalize(record[0])
                        break
                    if record:
                        journal.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
                        self.count += 1
                    now = time.monotonic()
                    if now - last_flush >= FLUSH_INTERVAL_SECONDS:
                        journal.flush()
                        last_flush = now
                    if now - last_snapshot >= self.snapshot_interval:
                        journal.flush()
                        self._snapshot()
                        last_snapshot = now
        except Exception as exc:
            self.error = f'检测记录写盘失败：{exc}'

    def _finalize(self, metadata):
        """收尾：把 journal 顺序合并进 inspection.json，然后删除 journal。"""
        self.metadata.update(metadata)
        temporary = self.path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as out, self.journal.open(encoding='utf-8') as source:
            out.write('{"metadata":')
            json.dump(self.metadata, out, ensure_ascii=False)
            out.write(',"records_total":%d' % self.count)
            out.write(',"records_complete":true,"records":[')
            first = True
            for line in source:
                line = line.strip()
                if not line:
                    continue
                if not first:
                    out.write(',')
                out.write(line)
                first = False
            out.write(']}')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)
        self.journal.unlink(missing_ok=True)

    def close(self, metadata, *, timeout: float = CLOSE_TIMEOUT_SECONDS):
        """收尾并等待写盘线程退出；超时不阻塞任务状态落库，但会记录清理错误。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._thread.is_alive():
            try:
                self._queue.put((dict(metadata),), timeout=.2)
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    self.error = self.error or '检测记录写盘积压，收尾超时'
                    return
                continue
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            self.error = self.error or '检测记录收尾超时（磁盘可能已挂起）'
        if self.error:
            raise RuntimeError(self.error)
