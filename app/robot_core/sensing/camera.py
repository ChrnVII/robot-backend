from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.robot_core.contracts import CaptureRequest


class CameraCaptureError(RuntimeError):
    """Raised when a camera cannot be opened or a frame cannot be encoded."""


@dataclass(frozen=True)
class CameraSettings:
    device: str = "/dev/video0"
    width: int = 1280
    height: int = 720
    warmup_frames: int = 3
    jpeg_quality: int = 90

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "CameraSettings":
        values = values or {}
        device = str(values.get("device", "/dev/video0"))
        width = int(values.get("width", 1280))
        height = int(values.get("height", 720))
        warmup_frames = int(values.get("warmup_frames", 3))
        jpeg_quality = int(values.get("jpeg_quality", 90))
        if not device:
            raise ValueError("camera.device must not be empty")
        if width < 1 or height < 1:
            raise ValueError("camera width and height must be positive")
        if warmup_frames < 0 or warmup_frames > 30:
            raise ValueError("camera warmup_frames must be between 0 and 30")
        if jpeg_quality < 1 or jpeg_quality > 100:
            raise ValueError("camera jpeg_quality must be between 1 and 100")
        return cls(device, width, height, warmup_frames, jpeg_quality)


class OpenCVCameraProvider:
    """Capture one JPEG frame from a V4L2 camera."""

    def __init__(self, settings: CameraSettings | None = None, *, cv2_module: Any = None):
        self.settings = settings or CameraSettings()
        self._cv2 = cv2_module

    def capture(self, request: CaptureRequest) -> bytes:
        cv2 = self._load_cv2()
        settings = self._settings_for(request)
        source = self._camera_source(settings.device)
        # Linux 工控机统一使用 V4L2 采集后端；getattr 兜底以兼容精简的 OpenCV
        # 构建与轻量测试替身。
        backend = getattr(cv2, "CAP_V4L2", 0)
        camera = cv2.VideoCapture(source, backend)
        try:
            if not camera.isOpened():
                raise CameraCaptureError(f"camera unavailable: {settings.device}")
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, settings.width)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.height)
            for _ in range(settings.warmup_frames):
                ok, _ = camera.read()
                if not ok:
                    raise CameraCaptureError("camera warmup failed")
            ok, frame = camera.read()
            if not ok or frame is None:
                raise CameraCaptureError("camera frame capture failed")
            encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, settings.jpeg_quality])
            if not encoded:
                raise CameraCaptureError("JPEG encoding failed")
            return bytes(buffer)
        finally:
            camera.release()

    def _load_cv2(self) -> Any:
        if self._cv2 is not None:
            return self._cv2
        try:
            import cv2
        except ImportError as error:
            raise CameraCaptureError("install the media extra with OpenCV before using camera capture") from error
        self._cv2 = cv2
        return cv2

    @staticmethod
    def _camera_source(device: str) -> Any:
        return int(device) if device.isdigit() else device

    def _settings_for(self, request: CaptureRequest) -> CameraSettings:
        if not request.parameters:
            return self.settings
        values = {
            "device": request.parameters.get("device", self.settings.device),
            "width": request.parameters.get("width", self.settings.width),
            "height": request.parameters.get("height", self.settings.height),
            "warmup_frames": request.parameters.get("warmup_frames", self.settings.warmup_frames),
            "jpeg_quality": request.parameters.get("jpeg_quality", self.settings.jpeg_quality),
        }
        return CameraSettings.from_mapping(values)
