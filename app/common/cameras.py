"""相机目录：球形（PTZ）与多路抓图相机的唯一 id 来源。

历史问题：同一台相机在不同层有三套名字——robot-core 的控制设备名（``deviceId``）、
sensor-worker 的采集 provider key、巡检/检测/证据使用的相机编号（``cameraId``）。
本模块把它们收敛成一份清单，供配置校验、API 与前端共用：

- 球形摄像头：``deviceId = "camera"``（控制设备/provider），``cameraId`` 取
  ``devices.camera.camera_id``（未配置时沿用旧引擎的兜底 ``cam01``）；
- 多路抓图相机：``devices.cameras.items[].id``，``deviceId`` 与 ``cameraId`` 同名；
- 因此 ``cameraId`` 才是跨层唯一的相机编号，``deviceId`` 只用于指定采集通道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.common.deployment import ConfigurationError

# 与 API 层 CameraId / 巡检 schema 的字符集保持一致
CAMERA_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# robot-core 设备名，同时是 sensor-worker 的球形采集 provider key
PTZ_DEVICE_ID = "camera"
# 旧巡检引擎的兜底编号：devices.camera 未声明 camera_id 时沿用，避免契约漂移
DEFAULT_PTZ_CAMERA_ID = "cam01"
PTZ_LABEL = "球形摄像头"
PTZ_SOURCE = "devices.camera.camera_id"
INSPECTION_SOURCE = "devices.cameras.items[].id"


@dataclass(frozen=True)
class Camera:
    """一条相机记录：``id`` 是跨层唯一的相机编号。"""

    id: str
    device_id: str
    kind: str  # ptz | inspection
    label: str
    enabled: bool = True
    host: str = ""

    @property
    def is_ptz(self) -> bool:
        return self.kind == "ptz"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "deviceId": self.device_id,
            "kind": self.kind,
            "label": self.label,
            "enabled": self.enabled,
            "host": self.host,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def devices_of(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(values.get("devices"))


def ptz_config(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(devices_of(values).get("camera"))


def inspection_items(values: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cameras = _mapping(devices_of(values).get("cameras"))
    items = cameras.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def ptz_camera_id(values: Mapping[str, Any]) -> str:
    """球形摄像头的相机编号：配置优先，缺省沿用旧引擎的 ``cam01``。"""
    return str(ptz_config(values).get("camera_id") or DEFAULT_PTZ_CAMERA_ID).strip()


def cameras_from_values(values: Mapping[str, Any]) -> list[Camera]:
    """按配置生成相机目录：球形在前，多路抓图相机按配置顺序。"""
    entries: list[Camera] = []
    camera = ptz_config(values)
    if camera:
        entries.append(Camera(
            id=ptz_camera_id(values),
            device_id=PTZ_DEVICE_ID,
            kind="ptz",
            label=str(camera.get("label") or camera.get("name") or PTZ_LABEL),
            enabled=bool(camera.get("enabled", True)),
            host=str(camera.get("host") or ""),
        ))
    for index, item in enumerate(inspection_items(values), start=1):
        camera_id = str(item.get("id") or item.get("camera_id") or "").strip()
        entries.append(Camera(
            id=camera_id,
            device_id=camera_id,
            kind="inspection",
            label=str(item.get("label") or item.get("name") or f"摄像头 {index:02d}"),
            enabled=bool(item.get("enabled", True)),
            host=str(item.get("host") or ""),
        ))
    return entries


def provider_ids(values: Mapping[str, Any]) -> list[str]:
    """sensor-worker 的采集 provider key：``camera`` + 各路抓图相机编号。"""
    ids = [PTZ_DEVICE_ID]
    ids.extend(entry.id for entry in cameras_from_values(values) if entry.kind == "inspection" and entry.id)
    return ids


def validate_cameras(values: Mapping[str, Any]) -> None:
    """校验相机编号唯一且合法。

    sensor-worker 用 dict 承载 provider，同 key 后写覆盖前写且没有告警；
    因此重复编号必须在配置加载期直接失败，而不是静默少一路相机。
    """
    seen: dict[str, str] = {}
    for entry in cameras_from_values(values):
        source = PTZ_SOURCE if entry.kind == "ptz" else INSPECTION_SOURCE
        if not entry.id:
            raise ConfigurationError(f"{source} must not be empty")
        if not CAMERA_ID_PATTERN.fullmatch(entry.id):
            raise ConfigurationError(f"camera id {entry.id!r} must match [A-Za-z0-9._-]{{1,64}}")
        key = entry.id.lower()
        if key in seen:
            raise ConfigurationError(f"duplicate camera id {entry.id!r}: {seen[key]} and {source}")
        seen[key] = source


def inspection_camera_ids(values: Mapping[str, Any]) -> list[str]:
    """已配置的巡检相机编号（保持配置顺序）。"""
    return [entry.id for entry in cameras_from_values(values) if entry.kind == "inspection" and entry.id]


def find_camera(entries: Iterable[Camera], camera_id: str) -> Camera | None:
    wanted = str(camera_id).strip().lower()
    for entry in entries:
        if entry.id.lower() == wanted:
            return entry
    return None
