"""相机目录服务：把平台 YAML 的相机清单收敛成 API 输出，作为唯一 id 来源。

- 有 ``ROBOT_PLATFORM_CONFIG``：读 YAML 的 ``devices.camera`` + ``devices.cameras.items``；
- 没配置 YAML（开发/单机联调）：只暴露球形摄像头，编号沿用配置文件缺省值。

球形之外的一切“这台相机是谁”的判断都走这里，避免前端/巡检/采集各写一份。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.common.cameras import (
    DEFAULT_PTZ_CAMERA_ID,
    PTZ_DEVICE_ID,
    PTZ_LABEL,
    Camera,
    cameras_from_values,
    find_camera,
)
from app.common.config import Settings
from app.common.deployment import load_platform_values
from app.common.exceptions import ValidationFailed


@lru_cache(maxsize=8)
def _platform_values(path: str) -> dict:
    """配置在进程生命周期内不变，读一次即可。"""
    return load_platform_values(Path(path))


class CameraDirectoryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def source(self) -> str:
        return self._settings.platform_config or "settings"

    @property
    def configured(self) -> bool:
        """是否读到了平台 YAML；未配置时无法判断“这台机器上有哪几路”，校验应放行。"""
        return bool(self._settings.platform_config)

    def entries(self) -> list[Camera]:
        if self._settings.platform_config:
            return cameras_from_values(_platform_values(self._settings.platform_config))
        return [self._settings_ptz()]

    def _settings_ptz(self) -> Camera:
        url = self._settings.camera_stream_url or ""
        return Camera(
            id=DEFAULT_PTZ_CAMERA_ID,
            device_id=PTZ_DEVICE_ID,
            kind="ptz",
            label=PTZ_LABEL,
            enabled=bool(self._settings.camera_stream_enabled),
            host=urlsplit(url).hostname or "",
        )

    def ptz(self) -> Camera:
        for entry in self.entries():
            if entry.is_ptz:
                return entry
        return self._settings_ptz()

    def inspection_ids(self) -> list[str]:
        """已启用的巡检相机编号（与 inspection-worker 的装配规则一致）。"""
        return [entry.id for entry in self.entries() if entry.kind == "inspection" and entry.id and entry.enabled]

    def available_ids(self) -> list[str]:
        return [entry.id for entry in self.entries() if entry.id]

    def resolve(self, camera_id: str | None) -> Camera:
        """按编号找相机；留空表示“那台球形摄像头”。

        参数可能是 FastAPI 的 ``Query`` 占位对象（直接调用路由函数时），
        因此只接受字符串，其余一律按“缺省”处理。
        """
        if not isinstance(camera_id, str) or not camera_id.strip():
            return self.ptz()
        found = find_camera(self.entries(), camera_id)
        if found is None:
            raise ValidationFailed(
                f"未知的 cameraId：{camera_id}；可用编号：{', '.join(self.available_ids()) or '（未配置相机）'}"
            )
        return found

    def require_ptz(self, camera_id: str | None) -> Camera:
        """云台/录像/预览目前只有球形一台，非球形编号直接给出可读错误。"""
        entry = self.resolve(camera_id)
        if not entry.is_ptz:
            ptz = self.ptz()
            raise ValidationFailed(
                f"{entry.label}（{entry.id}）不是球形摄像头，该接口仅支持球形摄像头 {ptz.id}"
            )
        return entry
