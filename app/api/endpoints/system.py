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

from app.api.deps import current_settings, get_camera_directory, get_media_service, get_robot_service
from app.common.clock import utc_now_iso
from app.common.exceptions import AppError
from app.common.local_folder import reveal_in_file_manager as _reveal_in_file_manager
from app.common.responses import ok, trace_of
from app.schemas.media import MediaCaptureRequest
from app.schemas.system import PlatformResponse
from app.services.cameras import CameraDirectoryService
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
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    media: MediaService = Depends(get_media_service),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> FileResponse:
    """返回一张真实 JPEG 快照（控制台预览轮询）；迁移 SystemController.cameraStream。

    低延迟预览（/ws/camera/preview）不可用时，前端回退到这里；两条链路互不影响。
    """
    directory.require_ptz(cameraId)
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
def camera_stream_status(
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    """球形摄像头低延迟预览的诊断快照（是否启用、编码、分辨率、订阅者、丢帧）。"""
    directory.require_ptz(cameraId)
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
def camera_recordings(
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    directory.require_ptz(cameraId)
    recorder = get_camera_stream_service().recorder
    # 同时返回目录绝对路径：前端「打开文件夹」需要它，且不需要用户手填。
    return ok({"rows": recorder.files(), "dir": str(recorder.directory)}, trace_of(request))


@router.post("/cam/recordings/open", tags=["system"])
def open_camera_recordings_dir(
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    """打开球形摄像头录像目录（服务端执行）。

    打不开不算错误：无桌面会话的部署下前端会退回到「复制路径」。
    """
    directory.require_ptz(cameraId)
    recorder = get_camera_stream_service().recorder
    target = recorder.directory
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppError("录像目录不可写", code="RECORDING_STORAGE_ERROR", status_code=503) from exc

    opened, detail = _reveal_in_file_manager(str(target))
    payload: dict[str, object] = {"opened": opened, "dir": str(target)}
    if not opened:
        payload["detail"] = detail
    return ok(payload, trace_of(request))


@router.post("/cam/recordings", tags=["system"])
def control_camera_recording(
    body: RecordingControl,
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    directory.require_ptz(cameraId)
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
def download_camera_recording(
    name: str,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> FileResponse:
    directory.require_ptz(cameraId)
    path = get_camera_stream_service().recorder.file(name)
    return FileResponse(path, media_type="video/x-matroska", filename=path.name)


@router.delete("/cam/recordings/{name}", tags=["system"])
def delete_camera_recording(
    name: str,
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    directory.require_ptz(cameraId)
    get_camera_stream_service().recorder.file(name).unlink()
    return ok({"deleted": name}, trace_of(request))
