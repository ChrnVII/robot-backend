from __future__ import annotations

import io
from typing import Any, Callable, Dict

from app.robot_core.contracts import CaptureRequest
from app.robot_core.storage import MediaStore


CaptureProvider = Callable[[CaptureRequest], bytes]


class SensorService:
    def __init__(self, media: MediaStore, providers: Dict[str, CaptureProvider]):
        self.media = media
        self.providers = providers

    def capture(self, request: CaptureRequest) -> Dict[str, Any]:
        provider = self.providers.get(request.device_id)
        if provider is None:
            raise RuntimeError("capture device unavailable")
        content = provider(request)
        extension, mime = {"image": (".jpg", "image/jpeg"), "thermal": (".bin", "application/octet-stream"), "audio": (".wav", "audio/wav")}.get(request.media_type, (".bin", "application/octet-stream"))
        return self.media.save(io.BytesIO(content), task_run_id=request.task_run_id,
                               waypoint_id=request.waypoint_id, device_id=request.device_id,
                               media_type=request.media_type, extension=extension, mime_type=mime)

