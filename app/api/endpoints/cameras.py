"""相机目录：球形摄像头与多路抓图相机的唯一编号来源。

前端通道、巡检校验与媒体采集都应基于这份清单，而不是各自写死 ``camera`` 或 8 路编号。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_camera_directory
from app.common.responses import ok, trace_of
from app.services.cameras import CameraDirectoryService

router = APIRouter()


@router.get("/cameras", tags=["cameras"])
def list_cameras(
    request: Request,
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    return ok({
        "source": directory.source,
        "cameras": [entry.as_dict() for entry in directory.entries()],
    }, trace_of(request))
