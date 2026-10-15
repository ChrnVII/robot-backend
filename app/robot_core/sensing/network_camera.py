from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener, urlopen

from app.robot_core.contracts import CaptureRequest

from .camera import CameraCaptureError


@dataclass(frozen=True)
class NetworkCameraSettings:
    host: str = "192.168.136.11"
    port: int = 8080
    path: str = "/API/picture"
    width: int = 1280
    height: int = 720
    timeout_seconds: float = 5.0
    username: Optional[str] = None
    password: Optional[str] = None
    auth_scheme: str = "basic"
    stream: int = 0
    jpeg_quality: int = 90

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "NetworkCameraSettings":
        values = values or {}
        host = str(values.get("host", cls.host)).strip()
        path = str(values.get("path", cls.path)).strip()
        if not host:
            raise ValueError("camera.host must not be empty")
        if not path.startswith("/"):
            path = "/" + path
        width = int(values.get("width", cls.width))
        height = int(values.get("height", cls.height))
        port = int(values.get("port", cls.port))
        timeout = float(values.get("timeout_seconds", cls.timeout_seconds))
        stream = int(values.get("stream", cls.stream))
        quality = int(values.get("jpeg_quality", cls.jpeg_quality))
        auth_scheme = str(values.get("auth_scheme", cls.auth_scheme)).lower()
        if not 1 <= port <= 65535:
            raise ValueError("camera.port must be between 1 and 65535")
        if width < 1 or height < 1:
            raise ValueError("camera width and height must be positive")
        if timeout <= 0 or timeout > 60:
            raise ValueError("camera.timeout_seconds must be greater than 0 and at most 60")
        if stream < 0:
            raise ValueError("camera.stream must not be negative")
        if not 1 <= quality <= 100:
            raise ValueError("camera jpeg_quality must be between 1 and 100")
        if auth_scheme not in {"basic", "digest", "none"}:
            raise ValueError("camera.auth_scheme must be basic, digest or none")
        return cls(host, port, path, width, height, timeout,
                   values.get("username"), values.get("password"), auth_scheme, stream, quality)


class HttpJpegCameraProvider:
    """Fetch one JPEG from the robot network camera HTTP API."""

    def __init__(self, settings: NetworkCameraSettings | None = None, *, opener: Any = None):
        self.settings = settings or NetworkCameraSettings()
        if opener is not None:
            self._opener = opener
        elif self.settings.auth_scheme == "digest" and self.settings.username:
            manager = HTTPPasswordMgrWithDefaultRealm()
            manager.add_password(None, self._origin(), self.settings.username, self.settings.password or "")
            self._opener = build_opener(HTTPDigestAuthHandler(manager)).open
        else:
            self._opener = urlopen

    def _origin(self) -> str:
        return f"http://{self.settings.host}:{self.settings.port}"

    def capture(self, request: CaptureRequest) -> bytes:
        settings = self._settings_for(request)
        query = urlencode({
            "videoResolutionWidth": settings.width,
            "videoResolutionHeight": settings.height,
        } if settings.path.startswith("/ISAPI/Streaming/") else {
            "width": settings.width,
            "height": settings.height,
            "imageType": "JPEG",
        })
        url = f"http://{settings.host}:{settings.port}{settings.path}?{query}"
        headers = {"Accept": "image/jpeg"}
        if settings.auth_scheme == "basic" and settings.username is not None:
            credentials = f"{settings.username}:{settings.password or ''}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        try:
            response = self._opener(Request(url, headers=headers), timeout=settings.timeout_seconds)
            with response:
                content = response.read()
        except (HTTPError, URLError, OSError, TimeoutError) as error:
            details = str(error).replace(url, self._safe_url(url))
            details = __import__("re").sub(r"(?i)(password|passwd|pwd)=([^&\s]+)", r"\1=***", details)
            raise CameraCaptureError(f"network camera request failed: {details}") from error
        if not content or not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
            raise CameraCaptureError("network camera returned invalid JPEG data")
        return content

    def _settings_for(self, request: CaptureRequest) -> NetworkCameraSettings:
        if not request.parameters:
            return self.settings
        values = {
            "host": request.parameters.get("host", self.settings.host),
            "port": request.parameters.get("port", self.settings.port),
            "path": request.parameters.get("path", self.settings.path),
            "width": request.parameters.get("width", self.settings.width),
            "height": request.parameters.get("height", self.settings.height),
            "timeout_seconds": request.parameters.get("timeout_seconds", self.settings.timeout_seconds),
            "username": request.parameters.get("username", self.settings.username),
            "password": request.parameters.get("password", self.settings.password),
            "auth_scheme": request.parameters.get("auth_scheme", self.settings.auth_scheme),
            "stream": request.parameters.get("stream", self.settings.stream),
            "jpeg_quality": request.parameters.get("jpeg_quality", self.settings.jpeg_quality),
        }
        return NetworkCameraSettings.from_mapping(values)

    @staticmethod
    def _safe_url(value: str) -> str:
        try:
            parts = urlsplit(value)
            if parts.username is not None or parts.password is not None:
                host = parts.hostname or "unknown-host"
                if parts.port:
                    host = f"{host}:{parts.port}"
                return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
        except ValueError:
            pass
        return value


class RtspCameraProvider:
    """Capture one JPEG frame from an RTSP network camera using OpenCV FFmpeg."""

    def __init__(self, settings: NetworkCameraSettings | None = None, *, cv2_module: Any = None):
        self.settings = settings or NetworkCameraSettings()
        self._cv2 = cv2_module

    def capture(self, request: CaptureRequest) -> bytes:
        cv2 = self._load_cv2()
        settings = self._settings_for(request)
        url = self._rtsp_url(settings)
        backend = getattr(cv2, "CAP_FFMPEG", 0)
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|timeout;5000000")
        camera = cv2.VideoCapture(url, backend)
        try:
            if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
                camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not camera.isOpened():
                raise CameraCaptureError(f"RTSP camera unavailable: {settings.host}")
            ok, frame = camera.read()
            if not ok or frame is None:
                raise CameraCaptureError("RTSP camera frame capture failed")
            encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, settings.jpeg_quality])
            if not encoded:
                raise CameraCaptureError("RTSP JPEG encoding failed")
            return bytes(buffer)
        finally:
            camera.release()

    @staticmethod
    def _rtsp_url(settings: NetworkCameraSettings) -> str:
        user = quote(settings.username or "", safe="")
        password = quote(settings.password or "", safe="")
        auth = f"{user}:{password}@" if user else ""
        channel = settings.stream if settings.stream in (101, 102, 103) else 101
        return f"rtsp://{auth}{settings.host}:{settings.port}/Streaming/Channels/{channel}"

    def _load_cv2(self) -> Any:
        if self._cv2 is not None:
            return self._cv2
        try:
            import cv2
        except ImportError as error:
            raise CameraCaptureError("install the media extra with OpenCV before using RTSP capture") from error
        self._cv2 = cv2
        return cv2

    def _settings_for(self, request: CaptureRequest) -> NetworkCameraSettings:
        if not request.parameters:
            return self.settings
        values = {name: request.parameters.get(name, getattr(self.settings, name)) for name in (
            "host", "port", "path", "width", "height", "timeout_seconds", "username", "password", "stream", "jpeg_quality"
        )}
        return NetworkCameraSettings.from_mapping(values)
