"""媒体域：采集 + 资产只读查询 / 音频回放与下载。

- ``POST /media/captures``：媒体采集（经 sensor-worker 网关，迁移 MediaCaptureService.java）；
- ``GET  /media/assets``：资产列表（分页，可按 taskRunId / mediaType 筛选）；
- ``GET  /media/assets/{assetId}``：资产元数据；
- ``GET  /media/assets/{assetId}/content``：文件内容，支持 Range（音频拖动播放）。

只读接口由 MediaLibraryService 直接读 media_assets 与本地文件；
写入权仍属于感知进程 —— 与 InferenceQueryService 同一套约定。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.responses import FileResponse

from app.api.deps import get_media_library_service, get_media_service
from app.common.responses import ok, trace_of
from app.schemas.media import MediaCaptureRequest
from app.services.media import MediaLibraryService, MediaService

logger = logging.getLogger(__name__)

router = APIRouter()

_RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")

#: 音频播放器的典型 Range 请求：按 1 MiB 分块回文件。
_RANGE_CHUNK = 1024 * 1024


@router.post("/media/captures", tags=["media"])
async def capture_media(
    request: Request,
    body: MediaCaptureRequest,
    media: MediaService = Depends(get_media_service),
) -> dict:
    return ok(await media.capture(body), trace_of(request))


@router.get("/media/assets", tags=["media"])
async def list_media_assets(
    request: Request,
    taskRunId: str | None = Query(default=None, max_length=128),
    mediaType: str | None = Query(default=None, pattern=r"^(image|audio|thermal)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    library: MediaLibraryService = Depends(get_media_library_service),
) -> dict:
    return ok(
        library.list_assets(task_run_id=taskRunId, media_type=mediaType, offset=offset, limit=limit),
        trace_of(request),
    )


@router.get("/media/assets/{asset_id}", tags=["media"])
async def get_media_asset(
    request: Request,
    asset_id: str,
    library: MediaLibraryService = Depends(get_media_library_service),
) -> dict:
    return ok(library.asset(asset_id), trace_of(request))


@router.get("/media/assets/{asset_id}/content", tags=["media"])
async def download_media_asset(
    request: Request,
    asset_id: str,
    library: MediaLibraryService = Depends(get_media_library_service),
) -> Response:
    path, item = library.file(asset_id)
    mime = str(item.get("mimeType") or "application/octet-stream")
    file_name = str(item.get("fileName") or Path(path).name)
    range_header = request.headers.get("range")
    if range_header:
        return _range_response(request, path, mime, file_name, range_header)
    return FileResponse(
        path, media_type=mime, filename=file_name,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"},
    )


def _range_response(request: Request, path: Path, mime: str, file_name: str,
                    range_header: str) -> Response:
    """处理单区间 Range 请求；多区间/非法区间退回整文件（浏览器不会发）。"""
    match = _RANGE_PATTERN.match(range_header.strip())
    try:
        size = path.stat().st_size
    except OSError as error:
        logger.warning("media stat failed: %s (%s)", path, error)
        return Response(status_code=404)
    start = 0
    end = size - 1
    if match:
        raw_start, raw_end = match.group(1), match.group(2)
        if raw_start:
            start = int(raw_start)
            if raw_end:
                end = min(int(raw_end), size - 1)
        elif raw_end:  # bytes=-N：最后 N 字节
            start = max(0, size - int(raw_end))
    if start > end or start >= size:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    length = end - start + 1
    if length > _RANGE_CHUNK:
        end = start + _RANGE_CHUNK - 1
        length = _RANGE_CHUNK

    def _stream():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return Response(
        content=_stream(),
        status_code=206,
        media_type=mime,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"inline; filename=\"{file_name}\"",
        },
    )
