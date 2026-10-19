from __future__ import annotations

import io
import logging
from typing import Any, Callable, Dict, Union

from app.robot_core.contracts import CaptureRequest, CaptureResult
from app.robot_core.storage import MediaStore

logger = logging.getLogger(__name__)

#: 采集器可以返回裸 bytes（相机等既有实现），也可以返回 CaptureResult（音频，
#: 带质量判定与电平指标）；SensorService 负责归一化。
CaptureProvider = Callable[[CaptureRequest], Union[bytes, CaptureResult]]

_EXTENSIONS = {
    "image": (".jpg", "image/jpeg"),
    "thermal": (".bin", "application/octet-stream"),
    "audio": (".wav", "audio/wav"),
}


class SensorService:
    def __init__(self, media: MediaStore, providers: Dict[str, CaptureProvider]):
        self.media = media
        self.providers = providers

    def capture(self, request: CaptureRequest) -> Dict[str, Any]:
        provider = self.providers.get(request.device_id)
        if provider is None:
            raise RuntimeError("capture device unavailable")
        result = provider(request)
        if isinstance(result, CaptureResult):
            content, quality_state, metrics = result.content, result.quality_state, dict(result.metrics)
        else:
            content, quality_state, metrics = result, "valid", {}
        extension, mime = _EXTENSIONS.get(request.media_type, (".bin", "application/octet-stream"))
        asset = self.media.save(
            io.BytesIO(content), task_run_id=request.task_run_id,
            waypoint_id=request.waypoint_id, device_id=request.device_id,
            media_type=request.media_type, extension=extension, mime_type=mime,
            quality_state=quality_state,
        )
        if quality_state != "valid":
            logger.warning("capture quality %s: asset=%s device=%s metrics=%s",
                           quality_state, asset["asset_id"], request.device_id, metrics)
        if metrics:
            # 指标随响应回传（不入库，避免为诊断字段再加一列）。
            asset["metrics"] = metrics
        return asset
