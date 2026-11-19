"""预览转封装：把 MKV 变成浏览器能播的 MP4（只换容器，不重编码）。

为什么要这一步：录像链路为了可靠性统一写 matroska（中断也能读），但 Chrome/Edge
不支持 matroska 容器，`<video src="x.mkv">` 直接没有画面。里面的码流是 H.264，
浏览器本来就会解，所以只要把容器换成 fragmented/normal MP4 即可 —— 全程 `copy`，
不碰解码器，一个 10 分钟的单路文件通常几秒完成。

三个约束：

1. **缓存键必须包含 mtime 与 size**：同名文件重录后不能命中旧缓存，否则播出来是上一次的。
2. **缓存目录在 `run/preview/`，不在 `data/` 下**：`data/` 会被 cloud-agent 递归扫描并上传，
   预览缓存放进去等于把派生文件当采集数据传走，白占云端存储。
3. **只允许受管目录**：cams / adhoc / missions / dome-cam。预览接口不能变成任意文件读取。
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from app.common.exceptions import AppError

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
#: 允许预览的目录（都在 data 下）。刻意用白名单而不是"data 下任意路径"以外的规则：
#: 以后新增产物目录要在这里显式加一行，避免无意中把别的目录暴露成可读。
ALLOWED_ROOTS = (
    REPO_ROOT / "data" / "cams",
    REPO_ROOT / "data" / "adhoc",
    REPO_ROOT / "data" / "missions",
    REPO_ROOT / "data" / "dome-cam",
)
#: 缓存目录：run/ 只放运行时文件（socket、缓存），不放业务数据，也不会被云端扫。
CACHE_DIR = REPO_ROOT / "run" / "preview"
CACHE_TTL_SECONDS = 24 * 3600


def resolve_source(raw_path: str) -> Path:
    """把请求路径解析成受管目录内的真实文件；越界一律 400。"""
    if not raw_path or "\x00" in raw_path:
        raise AppError("缺少文件路径", code="INVALID_REQUEST", status_code=400)
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise AppError("必须传绝对路径", code="INVALID_REQUEST", status_code=400)
    try:
        target = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AppError("文件不存在", code="ARTIFACT_NOT_FOUND", status_code=404) from error
    if not target.is_file():
        raise AppError("不是文件", code="ARTIFACT_NOT_FOUND", status_code=404)
    if not any(target.is_relative_to(root.resolve()) for root in ALLOWED_ROOTS):
        raise AppError("该路径不在可预览范围内", code="ARTIFACT_FORBIDDEN", status_code=403)
    return target


def _cache_key(source: Path) -> str:
    stat = source.stat()
    seed = f"{source}|{stat.st_mtime_ns}|{stat.st_size}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def _remux(source: Path, destination: Path) -> None:
    """MKV → MP4，流复制。失败抛 RuntimeError，由调用方转成可读错误。"""
    import av

    temporary = destination.with_suffix(".part")
    temporary.unlink(missing_ok=True)
    try:
        with av.open(str(source)) as source_container, av.open(
            str(temporary), "w", format="mp4", options={"movflags": "+faststart"}
        ) as target_container:
            mapping = {}
            for stream in source_container.streams:
                # 每路都要有 mapping：只搬视频流会让带字幕/多轨的文件错位。
                mapping[stream.index] = target_container.add_stream_from_template(stream)
            for packet in source_container.demux():
                if packet.size == 0:      # demux 末尾的空包必须跳过，否则 mux 报 EOF
                    continue
                packet.stream = mapping[packet.stream.index]
                target_container.mux(packet)
        os.replace(temporary, destination)
    except Exception:                     # noqa: BLE001 - 统一转成 500 + 可读原因
        temporary.unlink(missing_ok=True)
        raise


def preview_file(raw_path: str) -> Path:
    """返回可供浏览器播放的 MP4 路径（命中缓存直接返回）。"""
    source = resolve_source(raw_path)
    if source.suffix.lower() == ".mp4":
        return source                    # 已经是 MP4，不再转一遍
    if source.suffix.lower() != ".mkv":
        raise AppError("只有 MKV 需要转封装，其他类型请直接下载", code="PREVIEW_UNSUPPORTED", status_code=400)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destination = CACHE_DIR / f"{_cache_key(source)}.mp4"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination

    try:
        _remux(source, destination)
    except Exception as error:            # noqa: BLE001
        # 正在录制的文件会在这里失败：容器还没写完 / 索引不可读。
        logger.warning("preview remux failed for %s: %s", source, error)
        raise AppError(
            "无法生成预览：文件可能仍在录制或已损坏，可稍后重试或直接下载",
            code="PREVIEW_FAILED",
            status_code=422,
        ) from error
    return destination


def clean_cache() -> int:
    """清掉过期缓存（惰性调用，不挂后台线程）。返回删除的文件数。"""
    if not CACHE_DIR.is_dir():
        return 0
    import time

    deadline = time.time() - CACHE_TTL_SECONDS
    removed = 0
    for path in CACHE_DIR.glob("*.mp4"):
        try:
            if path.stat().st_mtime < deadline:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
