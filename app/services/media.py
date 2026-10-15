"""媒体服务：采集请求与资产查询（迁移 MediaCaptureService.java）。"""

from __future__ import annotations

from app.common.exceptions import ValidationFailed
from app.gateways.base import camelize
from app.gateways.sensor_worker import SensorWorkerGateway
from app.schemas.media import MediaCaptureRequest


class MediaService:
    def __init__(self, gateway: SensorWorkerGateway) -> None:
        self._gateway = gateway

    async def capture(self, request: MediaCaptureRequest) -> dict:
        matched = (
            (request.deviceId == "camera" and request.mediaType == "image")
            or (request.deviceId == "audio" and request.mediaType == "audio")
            or (request.deviceId == "thermal" and request.mediaType == "thermal")
        )
        if not matched:
            raise ValidationFailed("deviceId and mediaType do not match")
        return camelize(await self._gateway.call({
            "operation": "capture",
            "task_run_id": request.taskRunId,
            "waypoint_id": request.waypointId,
            "device_id": request.deviceId,
            "media_type": request.mediaType,
            "parameters": request.parameters,
        }))
