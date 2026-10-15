"""视频透传域：/video/streams（不推理的连续 JPEG 流，配套 ws/video）。

迁移来源：VideoStreamController.java；streamId 同时是 inspection-worker 的任务标识。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_inspection_service
from app.api.validation import ensure_id
from app.common.responses import ok, trace_of
from app.schemas.inspection import StartVideoStreamRequest
from app.services.inspection import InspectionService

router = APIRouter()


@router.post("/video/streams", tags=["video"])
async def start_stream(
    request: Request,
    body: StartVideoStreamRequest,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    return ok(await inspection.start_video_stream(body), trace_of(request))


@router.get("/video/streams/{stream_id}", tags=["video"])
async def stream_status(
    request: Request,
    stream_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(stream_id)
    return ok(await inspection.status(stream_id), trace_of(request))


@router.delete("/video/streams/{stream_id}", tags=["video"])
async def stop_stream(
    request: Request,
    stream_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(stream_id)
    return ok(await inspection.stop(stream_id), trace_of(request))
