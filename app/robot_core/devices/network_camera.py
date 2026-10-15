from __future__ import annotations

import asyncio
import base64
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib import error, parse, request
from xml.sax.saxutils import escape

from app.robot_core.common import utc_now
from app.robot_core.contracts import CommandResult, DeviceCommand, DeviceState, HealthState


class NetworkCameraError(RuntimeError):
    """A camera control or health request failed."""


@dataclass(frozen=True)
class NetworkCameraControlSettings:
    """Configurable HTTP adapter for vendor-specific PTZ cameras.

    The camera vendor protocol is intentionally not hard-coded. Each action
    path can be set to the vendor endpoint in hardware.yaml. Requests use a
    small JSON envelope by default and can also be sent as query parameters.
    """

    base_url: str = "http://127.0.0.1:8080"
    snapshot_path: str = "/API/picture"
    ptz_path: Optional[str] = None
    preset_path: Optional[str] = None
    goto_preset_path: Optional[str] = None
    reboot_path: Optional[str] = None
    stop_path: Optional[str] = None
    state_path: Optional[str] = None
    method: str = "POST"
    timeout_seconds: float = 5.0
    username: Optional[str] = None
    password: Optional[str] = None
    request_format: str = "json"
    protocol: str = "generic"
    auth_scheme: str = "basic"
    channel: int = 1

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "NetworkCameraControlSettings":
        def path(name: str, default: Optional[str] = None) -> Optional[str]:
            value = values.get(name, default)
            if value in (None, ""):
                return None
            result = str(value).strip()
            return "/" + result.lstrip("/")

        base_url = str(values.get("base_url", "")).strip().rstrip("/")
        if not base_url:
            host = str(values.get("host", "127.0.0.1")).strip()
            port = int(values.get("port", 8080))
            base_url = f"http://{host}:{port}"
        timeout = float(values.get("timeout_seconds", cls.timeout_seconds))
        protocol = str(values.get("protocol", cls.protocol)).lower()
        auth_scheme = str(values.get("auth_scheme", "digest" if protocol == "hikvision_isapi" else cls.auth_scheme)).lower()
        method = str(values.get("method", "PUT" if protocol == "hikvision_isapi" else cls.method)).upper()
        request_format = str(values.get("request_format", "xml" if protocol == "hikvision_isapi" else cls.request_format)).lower()
        channel = int(values.get("channel", cls.channel))
        if timeout <= 0 or timeout > 60:
            raise ValueError("camera.control.timeout_seconds must be greater than 0 and at most 60")
        if method not in {"GET", "POST", "PUT", "PATCH"}:
            raise ValueError("camera.control.method must be GET, POST, PUT or PATCH")
        if request_format not in {"json", "xml", "query"}:
            raise ValueError("camera.control.request_format must be json, xml or query")
        if auth_scheme not in {"basic", "digest", "none"}:
            raise ValueError("camera.control.auth_scheme must be basic, digest or none")
        if channel < 1:
            raise ValueError("camera.control.channel must be positive")
        if protocol == "hikvision_isapi":
            channel_path = f"/ISAPI/PTZCtrl/channels/{channel}"
            values = dict(values)
            values.setdefault("snapshot_path", f"/ISAPI/Streaming/channels/{channel}/picture")
            values.setdefault("ptz_path", f"{channel_path}/continuous")
            values.setdefault("preset_path", f"{channel_path}/presets/{{preset}}")
            values.setdefault("goto_preset_path", f"{channel_path}/presets/{{preset}}/goto")
            values.setdefault("reboot_path", "/ISAPI/System/reboot")
            values.setdefault("stop_path", f"{channel_path}/continuous")
        return cls(
            base_url=base_url,
            snapshot_path=path("snapshot_path", "/API/picture") or "/API/picture",
            ptz_path=path("ptz_path"), preset_path=path("preset_path"), goto_preset_path=path("goto_preset_path"),
            reboot_path=path("reboot_path"), stop_path=path("stop_path"),
            state_path=path("state_path"), method=method, timeout_seconds=timeout,
            username=values.get("username"), password=values.get("password"),
            request_format=request_format, protocol=protocol, auth_scheme=auth_scheme, channel=channel,
        )


class NetworkCameraDriver:
    """Robot-core driver for a network PTZ camera.

    Capture remains owned by sensor-worker. This driver owns only control and
    health requests, so there is one writer for PTZ actions and no blocking
    urllib call on robot-core's event loop.
    """

    _DIRECTIONS = {"up", "down", "left", "right", "zoom_in", "zoom_out", "stop"}

    def __init__(self, settings: NetworkCameraControlSettings, *, device_id: str = "camera",
                 opener: Optional[Callable[..., Any]] = None) -> None:
        self.device_id = device_id
        self.settings = settings
        if opener is not None:
            self._opener = opener
        elif settings.auth_scheme == "digest" and settings.username is not None:
            manager = request.HTTPPasswordMgrWithDefaultRealm()
            manager.add_password(None, settings.base_url, settings.username, settings.password or "")
            self._opener = request.build_opener(request.HTTPDigestAuthHandler(manager)).open
        else:
            self._opener = request.urlopen
        self._opened = False
        self._last_values: dict[str, Any] = {}

    def _url(self, path: str) -> str:
        return f"{self.settings.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, image/jpeg"}
        if self.settings.auth_scheme == "basic" and self.settings.username is not None:
            token = f"{self.settings.username}:{self.settings.password or ''}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
        return headers

    def _request_sync(self, path: str, payload: Any = None, *, method: Optional[str] = None) -> Any:
        payload = dict(payload or {}) if isinstance(payload, Mapping) else payload
        url = self._url(path)
        headers = self._headers()
        data = None
        http_method = method or self.settings.method
        if self.settings.request_format == "query" or http_method == "GET":
            if isinstance(payload, Mapping) and payload:
                url = f"{url}?{parse.urlencode(payload)}"
        elif payload is not None:
            if isinstance(payload, bytes):
                data = payload
                if self.settings.request_format == "xml":
                    headers["Content-Type"] = "application/xml"
            elif self.settings.request_format == "xml":
                data = str(payload).encode("utf-8")
                headers["Content-Type"] = "application/xml"
            else:
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=http_method)
        try:
            with self._opener(req, timeout=self.settings.timeout_seconds) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except (OSError, error.URLError, TimeoutError) as exc:
            raise NetworkCameraError(f"camera request failed: {url}: {exc}") from exc
        if status >= 400:
            raise NetworkCameraError(f"camera returned HTTP {status}: {url}")
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"response_bytes": len(raw)}
        if isinstance(value, Mapping):
            code = value.get("code")
            if code not in (None, 0, 200, "0", "200", "OK", "ok", True):
                raise NetworkCameraError(str(value.get("msg") or value.get("message") or "camera rejected request"))
        return value

    async def _request(self, path: str, payload: Any = None, *, method: Optional[str] = None) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._request_sync(path, payload, method=method))

    async def open(self) -> None:
        self._opened = True

    async def self_test(self) -> DeviceState:
        return await self.read_state()

    async def read_state(self) -> DeviceState:
        if not self._opened:
            return DeviceState(self.device_id, HealthState.UNAVAILABLE, utc_now(), self.settings.base_url,
                               {}, quality="invalid", raw_fault_code="device_not_open")
        try:
            if self.settings.state_path:
                response = await self._request(self.settings.state_path)
                values = dict(response) if isinstance(response, Mapping) else {"response": response}
            else:
                # A GET to the configured snapshot endpoint is a vendor-neutral
                # reachability check when no status endpoint is supplied.
                response = await self._request_sync_get(self.settings.snapshot_path)
                values = {"reachable": True, "response": response}
            values.update({"base_url": self.settings.base_url, "control_configured": any((self.settings.ptz_path, self.settings.preset_path, self.settings.reboot_path))})
            self._last_values = values
            return DeviceState(self.device_id, HealthState.OK, utc_now(), self.settings.base_url, values)
        except NetworkCameraError as exc:
            return DeviceState(self.device_id, HealthState.UNAVAILABLE, utc_now(), self.settings.base_url,
                               dict(self._last_values), quality="invalid", raw_fault_code=str(exc)[:256])

    async def _request_sync_get(self, path: str) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._request_sync(path, method="GET"))

    def _hikvision_ptz_xml(self, direct: str, speed: float) -> bytes:
        value = int(round(speed * 100))
        pan = 0
        tilt = 0
        zoom = 0
        if direct == "left":
            pan = -value
        elif direct == "right":
            pan = value
        elif direct == "up":
            tilt = value
        elif direct == "down":
            tilt = -value
        elif direct == "zoom_in":
            zoom = value
        elif direct == "zoom_out":
            zoom = -value
        return f"<PTZData><pan>{pan}</pan><tilt>{tilt}</tilt><zoom>{zoom}</zoom></PTZData>".encode("utf-8")

    async def execute(self, command: DeviceCommand) -> CommandResult:
        try:
            action = command.action
            payload = dict(command.payload)
            method = "PUT" if self.settings.protocol == "hikvision_isapi" else None
            if action == "camera.ptz":
                direct = str(payload.get("direct", "")).lower()
                if direct not in self._DIRECTIONS:
                    raise NetworkCameraError("invalid camera.ptz direction")
                if self.settings.stop_path and direct == "stop":
                    path = self.settings.stop_path
                else:
                    path = self.settings.ptz_path
                if not path:
                    raise NetworkCameraError("camera.ptz endpoint is not configured")
                speed = float(payload.get("speed", 1))
                if not math.isfinite(speed) or not 0 <= speed <= 1:
                    raise NetworkCameraError("camera.ptz speed must be between 0 and 1")
                if self.settings.protocol == "hikvision_isapi":
                    payload = self._hikvision_ptz_xml(direct, 0 if direct == "stop" else speed)
                else:
                    payload = {"action": "ptz", "direct": direct, "speed": speed}
            elif action == "camera.goto_preset":
                path = self.settings.goto_preset_path or self.settings.preset_path
                if not path:
                    raise NetworkCameraError("camera.goto_preset endpoint is not configured")
                preset = int(payload.get("preset"))
                if preset < 1:
                    raise NetworkCameraError("camera preset must be positive")
                if self.settings.protocol == "hikvision_isapi":
                    path = path.replace("{preset}", str(preset))
                    payload = b""
                else:
                    payload = {"action": "goto_preset", "preset": preset}
            elif action == "camera.save_preset":
                path = self.settings.preset_path
                if not path:
                    raise NetworkCameraError("camera.save_preset endpoint is not configured")
                preset = int(payload.get("preset"))
                if preset < 1:
                    raise NetworkCameraError("camera preset must be positive")
                path = path.replace("{preset}", str(preset))
                if self.settings.protocol == "hikvision_isapi":
                    name = escape(str(payload.get("name") or f"预置点 {preset}"))
                    payload = (f'<?xml version="1.0" encoding="UTF-8"?>'
                               f'<PTZPreset xmlns="http://www.isapi.org/ver20/XMLSchema" version="2.0">'
                               f'<id>{preset}</id><presetName>{name}</presetName></PTZPreset>').encode("utf-8")
                else:
                    payload = {"action": "save_preset", "preset": preset, "name": payload.get("name")}
            elif action == "camera.delete_preset":
                path = self.settings.preset_path
                if not path:
                    raise NetworkCameraError("camera.delete_preset endpoint is not configured")
                preset = int(payload.get("preset"))
                if preset < 1:
                    raise NetworkCameraError("camera preset must be positive")
                path = path.replace("{preset}", str(preset))
                method = "DELETE" if self.settings.protocol == "hikvision_isapi" else None
                payload = b"" if self.settings.protocol == "hikvision_isapi" else {"action": "delete_preset", "preset": preset}
            elif action == "camera.reboot":
                path = self.settings.reboot_path
                if not path:
                    raise NetworkCameraError("camera.reboot endpoint is not configured")
                payload = b"" if self.settings.protocol == "hikvision_isapi" else {"action": "reboot"}
            elif action == "stop":
                path = self.settings.stop_path or self.settings.ptz_path
                if not path:
                    raise NetworkCameraError("camera stop endpoint is not configured")
                payload = self._hikvision_ptz_xml("stop", 0) if self.settings.protocol == "hikvision_isapi" else {"action": "stop", "direct": "stop", "speed": 0}
            else:
                raise NetworkCameraError(f"unsupported camera action: {action}")
            response = await self._request(path, payload, method=method)
            return CommandResult(command.command_id, "succeeded", details={"device": self.device_id, "action": action, "response": response})
        except (NetworkCameraError, TypeError, ValueError) as exc:
            return CommandResult(command.command_id, "rejected", reject_reason=f"camera_request_failed: {exc}")

    async def close(self) -> None:
        self._opened = False
