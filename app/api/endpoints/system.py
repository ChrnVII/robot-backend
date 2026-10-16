"""系统域：平台信息、健康/就绪探针、版本与摄像头快照。

迁移来源：RobotController.health/platform、SystemController.java。
- 根路径（/healthz、/readyz、/version）挂 root_router；
- /api/v1 路径（/platform、/cam/stream）挂 router。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from pydantic import BaseModel
from urllib.parse import urlparse
from urllib.request import url2pathname

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from app.api.deps import current_settings, get_media_service, get_robot_service
from app.common.clock import utc_now_iso
from app.common.exceptions import AppError
from app.common.responses import ok, trace_of
from app.schemas.media import MediaCaptureRequest
from app.schemas.system import PlatformResponse
from app.services.media import MediaService
from app.services.robot import RobotService
from app.video import get_camera_stream_service

root_router = APIRouter()
router = APIRouter()


@root_router.get("/healthz", tags=["system"])
async def healthz(request: Request, robot: RobotService = Depends(get_robot_service)) -> dict:
    core_status = await robot.core_status()
    status = "ok" if core_status == "ok" else "degraded"
    return ok({
        "status": status,
        "service": current_settings().app_name,
        "robotId": robot.robot_id,
        "coreStatus": core_status,
        "timestamp": utc_now_iso(),
    }, trace_of(request))


@root_router.get("/readyz", tags=["system"])
def readyz(request: Request) -> dict:
    return ok({"ready": True, "status": "ok"}, trace_of(request))


@root_router.get("/version", tags=["system"])
def version(request: Request) -> dict:
    return ok({"version": current_settings().version, "apiVersion": "v1"}, trace_of(request))


@router.get("/platform", tags=["system"])
def platform(request: Request, robot: RobotService = Depends(get_robot_service)) -> dict:
    payload = PlatformResponse(robotId=robot.robot_id, mode=robot.mode)
    return ok(payload.model_dump(), trace_of(request))


@router.get("/cam/stream", tags=["system"])
async def camera_stream(
    request: Request,
    preset: int | None = Query(default=None),
    frame: str | None = Query(default=None),
    media: MediaService = Depends(get_media_service),
) -> FileResponse:
    """返回一张真实 JPEG 快照（控制台预览轮询）；迁移 SystemController.cameraStream。

    低延迟预览（/ws/camera/preview）不可用时，前端回退到这里；两条链路互不影响。
    """
    task_run_id = f"preview-{_safe_token(frame, 'live')}"
    waypoint = "preview" if preset is None else f"preset-{preset}"
    asset = await media.capture(MediaCaptureRequest(
        taskRunId=task_run_id, waypointId=waypoint, deviceId="camera",
        mediaType="image", parameters={},
    ))
    uri = asset.get("uri")
    if uri is None:
        raise AppError("sensor-worker did not return an image URI", code="INTERNAL_ERROR", status_code=500)
    path = _uri_to_path(str(uri))
    if not path.is_file():
        raise AppError("captured image is not available", code="INTERNAL_ERROR", status_code=500)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/cam/stream/status", tags=["system"])
def camera_stream_status(request: Request) -> dict:
    """球形摄像头低延迟预览的诊断快照（是否启用、编码、分辨率、订阅者、丢帧）。"""
    return ok(get_camera_stream_service().status(), trace_of(request))


def _safe_token(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    token = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    return token[:64] if token else fallback


def _uri_to_path(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(url2pathname(urlparse(uri).path))
    return Path(uri)


class RecordingControl(BaseModel):
    action: Literal["start", "stop"]


@router.get("/cam/recordings", tags=["system"])
def camera_recordings(request: Request) -> dict:
    return ok(get_camera_stream_service().recorder.files(), trace_of(request))


@router.post("/cam/recordings", tags=["system"])
def control_camera_recording(body: RecordingControl, request: Request) -> dict:
    service = get_camera_stream_service()
    if body.action == "start":
        state = service.status()
        if not service.started or not state.get("connected") or state.get("fatal"):
            raise AppError("摄像头码流尚未连接，无法开始录像", code="STREAM_UNAVAILABLE", status_code=409)
        try:
            result = service.recorder.start()
        except OSError as exc:
            raise AppError("录像目录不可写", code="RECORDING_STORAGE_ERROR", status_code=503) from exc
    else:
        result = service.recorder.finish(stop=True)
    return ok(result, trace_of(request))


@router.get("/cam/recordings/{name}", tags=["system"])
def download_camera_recording(name: str) -> FileResponse:
    path = get_camera_stream_service().recorder.file(name)
    return FileResponse(path, media_type="video/x-matroska", filename=path.name)


@router.delete("/cam/recordings/{name}", tags=["system"])
def delete_camera_recording(name: str, request: Request) -> dict:
    get_camera_stream_service().recorder.file(name).unlink()
    return ok({"deleted": name}, trace_of(request))
