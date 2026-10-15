"""媒体域：/media/captures 媒体采集（经 sensor-worker 网关）。

迁移来源：MediaCaptureService.java；采集成功后由 sensor-worker 自行登记资产。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_media_service
from app.common.responses import ok, trace_of
from app.schemas.media import MediaCaptureRequest
from app.services.media import MediaService

router = APIRouter()


@router.post("/media/captures", tags=["media"])
async def capture_media(
    request: Request,
    body: MediaCaptureRequest,
    media: MediaService = Depends(get_media_service),
) -> dict:
    return ok(await media.capture(body), trace_of(request))
