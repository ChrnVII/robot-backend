from __future__ import annotations

import os
import re
import time
from typing import Any, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

from .camera import CameraCaptureError
from .network_camera import HttpJpegCameraProvider, NetworkCameraSettings


class OpenCVStreamReader:
    """Persistent OpenCV reader for V4L2, DirectShow and RTSP sources."""

    def __init__(self, source: Any, *, width: int = 1280, height: int = 720, backend: int | None = None):
        self.source = source
        self.width = width
        self.height = height
        self.backend = backend
        self._cv2: Any = None
        self._capture: Any = None

    def __call__(self) -> Any:
        cv2 = self._load_cv2()
        if self._capture is None or not self._capture.isOpened():
            self._open(cv2)
        ok, frame = self._capture.read()
        if not ok or frame is None:
            self._close()
            raise CameraCaptureError(f"stream frame capture failed: {self._safe_source()}")
        return frame

    def close(self) -> None:
        self._close()

    def _open(self, cv2: Any) -> None:
        is_rtsp = isinstance(self.source, str) and self.source.lower().startswith(("rtsp://", "rtsps://"))
        backend = self.backend if self.backend is not None else (
            getattr(cv2, "CAP_FFMPEG", 0) if is_rtsp else 0
        )
        if is_rtsp:
            # OpenCV's FFmpeg backend otherwise chooses UDP on some builds,
            # which is unreliable on the robot's routed camera network.
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;5000000")
        self._capture = cv2.VideoCapture(self.source, backend)
        if not self._capture.isOpened():
            self._close()
            raise CameraCaptureError(f"stream unavailable: {self._safe_source()}")
        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _safe_source(self) -> str:
        if not isinstance(self.source, str):
            return str(self.source)
        try:
            parts = urlsplit(self.source)
            if parts.scheme.lower() in {"rtsp", "rtsps"}:
                host = parts.hostname or "unknown-host"
                if parts.port:
                    host = f"{host}:{parts.port}"
                return urlunsplit((parts.scheme, host, parts.path, "", ""))
        except ValueError:
            pass
        return re.sub(r"(?<=//)[^/@\s]+@", "***@", self.source)

    def _load_cv2(self) -> Any:
        if self._cv2 is None:
            try:
                import cv2
            except ImportError as error:
                raise CameraCaptureError("install the media extra before starting realtime inspection") from error
            self._cv2 = cv2
        return self._cv2


class HttpSnapshotReader:
    """Decode the robot HTTP JPEG endpoint into OpenCV frames."""

    def __init__(self, settings: NetworkCameraSettings):
        self.provider = HttpJpegCameraProvider(settings)
        self._cv2: Any = None

    def __call__(self) -> Any:
        try:
            import cv2
            import numpy as np
        except ImportError as error:
            raise CameraCaptureError("opencv-python and numpy are required for HTTP camera streams") from error
        self._cv2 = cv2
        content = self.provider.capture(_Request())
        image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise CameraCaptureError("HTTP camera returned an undecodable JPEG")
        return image


class _Request:
    parameters: Mapping[str, Any] = {}


def build_camera_reader(camera: Mapping[str, Any]) -> Any:
    transport = str(camera.get("transport", "rtsp")).lower()
    width = int(camera.get("width", 1280))
    height = int(camera.get("height", 720))
    if transport == "http":
        return HttpSnapshotReader(NetworkCameraSettings.from_mapping(camera))
    if transport == "v4l2":
        source = str(camera.get("device", "0"))
        source = int(source) if source.isdigit() else source
        return OpenCVStreamReader(source, width=width, height=height)
    if transport == "rtsp":
        host = str(camera["host"])
        port = int(camera.get("port", 554))
        user = quote(str(camera.get("username") or ""), safe="")
        password = quote(str(camera.get("password") or ""), safe="")
        auth = f"{user}:{password}@" if user else ""
        stream = int(camera.get("stream", 101))
        channel = stream if stream in (101, 102, 103) else 101
        path = camera.get("path") or f"/Streaming/Channels/{channel}"
        url = f"rtsp://{auth}{host}:{port}{path}"
        return OpenCVStreamReader(url, width=width, height=height)
    raise ValueError(f"unsupported camera transport: {transport}")
