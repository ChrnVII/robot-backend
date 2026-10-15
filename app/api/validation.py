"""路径参数手工校验：对齐旧 Java @Pattern 失败 -> 400 VALIDATION_ERROR。

（FastAPI 新版本会把 Path(pattern=...) 用于路由匹配，不匹配时返回 404；
旧契约是 400，这里改为进入处理器后手工校验。）
"""

from __future__ import annotations

import re

from app.common.exceptions import ValidationFailed

_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_CAMERA_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_LEASE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_IMAGE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}:[A-Za-z0-9._-]{1,160}\.jpg")


def _ensure(value: str, pattern: re.Pattern[str]) -> str:
    if not value or not pattern.fullmatch(value):
        raise ValidationFailed("Request validation failed")
    return value


def ensure_id(value: str) -> str:
    """taskRunId / streamId：``[A-Za-z0-9._-]{1,128}``。"""
    return _ensure(value, _ID)


def ensure_camera_id(value: str) -> str:
    return _ensure(value, _CAMERA_ID)


def ensure_lease_id(value: str) -> str:
    return _ensure(value, _LEASE_ID)


def ensure_image_id(value: str) -> str:
    """证据图 ID：``{cameraId}:{fileName}.jpg``。"""
    return _ensure(value, _IMAGE_ID)
