"""相机目录：球形摄像头与多路抓图相机的唯一编号来源。

前端通道、巡检校验与媒体采集都应基于这份清单，而不是各自写死 ``camera`` 或 8 路编号。

除目录之外，这里还有两个**只读**接口：

- ``GET /cameras/presets``：预置点清单（相机为准）；
- ``GET /cameras/snapshot``：抓一张 JPEG，直接返回 ``image/jpeg``。

为什么这两个放在这里而不是工作台：云端发起的 ``camera.preset.list`` 与
``camera.snapshot`` 由 cloud-agent 进程调用，它需要的是"最小、无副作用、
不依赖业务库"的入口。工作台的 ``/cam/presets`` 会合并本地名字缓存，那个逻辑
属于页面，不属于链路。

两者都不产生设备动作，因此**不走控制租约**——云台转动才需要租约。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.deps import get_camera_directory, get_control_service
from app.common.responses import ok, trace_of
from app.services.cameras import CameraDirectoryService
from app.services.control import ControlService

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


@router.get("/cameras/presets", tags=["cameras"])
async def list_presets(
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
    control: ControlService = Depends(get_control_service),
) -> dict:
    """预置点清单：只问相机，不掺本地缓存。

    相机不可达时返回 502 —— 调用方（cloud-agent）需要知道"问不到"，
    而不是收到一个空列表后误以为"相机上没有任何预置点"。
    """
    camera = directory.require_ptz(cameraId)
    presets = await control.camera_presets()
    return ok({
        "cameraId": camera.id,
        "items": presets,
        "source": "camera",
    }, trace_of(request))


@router.get("/cameras/snapshot", tags=["cameras"])
async def camera_snapshot(
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    directory: CameraDirectoryService = Depends(get_camera_directory),
    control: ControlService = Depends(get_control_service),
) -> Response:
    """抓一张 JPEG。

    直接返回二进制而不是塞进 JSON 信封：图像进 JSON 会 base64 膨胀 33%，
    而且调用方（云端 / 页面）本来就要的是字节流。元信息走响应头，
    这样"拍到了什么时候的图"和"图本身"不会因为两次请求而错位。
    """
    directory.require_ptz(cameraId)
    image, meta = await control.camera_snapshot()
    return Response(
        content=image,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Camera-Bytes": str(meta.get("size") or len(image)),
            "X-Camera-Captured-At": str(meta.get("captured_at") or ""),
            "X-Trace-Id": trace_of(request),
        },
    )
