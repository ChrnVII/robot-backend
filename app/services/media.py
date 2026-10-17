"""媒体服务：采集请求与资产查询（迁移 MediaCaptureService.java）。

``deviceId`` 语义收敛为「相机编号」：可传球形摄像头的设备名 ``camera``（兼容旧调用）、
球形编号（如 ``VAR-1``）或多路抓图相机编号（``cam01``…）。服务端把它映射成
sensor-worker 的 provider key，避免“都叫 camera”时全部落到同一路。
"""

from __future__ import annotations

from app.common.exceptions import ValidationFailed
from app.contracts import Op
from app.gateways.base import camelize
from app.gateways.sensor_worker import SensorWorkerGateway
from app.schemas.media import MediaCaptureRequest
from app.services.cameras import CameraDirectoryService

# sensor-worker 固定注册的非相机 provider
AUDIO_DEVICE_ID = "audio"
THERMAL_DEVICE_ID = "thermal"


class MediaService:
    def __init__(self, gateway: SensorWorkerGateway, directory: CameraDirectoryService) -> None:
        self._gateway = gateway
        self._directory = directory

    async def capture(self, request: MediaCaptureRequest) -> dict:
        device_id = str(request.deviceId)
        media_type = str(request.mediaType)
        if media_type == "image":
            provider = self._camera_provider(device_id)
        elif media_type == "audio" and device_id == AUDIO_DEVICE_ID:
            provider = AUDIO_DEVICE_ID
        elif media_type == "thermal" and device_id == THERMAL_DEVICE_ID:
            provider = THERMAL_DEVICE_ID
        else:
            raise ValidationFailed(
                f"deviceId 与 mediaType 不匹配：deviceId={device_id}、mediaType={media_type}；"
                "图片采集请传相机编号，录音传 audio，热成像传 thermal"
            )
        return camelize(await self._gateway.call({
            "operation": Op.CAPTURE,
            "task_run_id": request.taskRunId,
            "waypoint_id": request.waypointId,
            "device_id": provider,
            "media_type": media_type,
            "parameters": request.parameters,
        }))

    def _camera_provider(self, device_id: str) -> str:
        """相机编号 → sensor-worker provider key。"""
        ptz = self._directory.ptz()
        if device_id == ptz.device_id:
            # 旧调用直接写设备名 camera：仍然指球形，避免破坏既有前端
            return ptz.device_id
        entry = self._directory.resolve(device_id)
        if not entry.enabled:
            raise ValidationFailed(f"{entry.label}（{entry.id}）已在配置里禁用，无法采集")
        return entry.device_id if entry.is_ptz else entry.id
