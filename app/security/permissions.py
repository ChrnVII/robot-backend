"""角色权限矩阵：viewer / operator / mission_editor / model_admin / config_admin / safety_admin。

目标 API（API.md §6）给出权限标注；本模块提供 require_roles 依赖工厂，
端点在启用鉴权（ROBOT_AUTH_ENABLED=true）时按需挂载。admin 角色始终放行。
"""

from __future__ import annotations

from fastapi import Depends

from app.common.config import get_settings
from app.security.auth import ForbiddenError, get_current_user

ROLES = (
    "viewer",
    "operator",
    "mission_editor",
    "mission_admin",
    "model_admin",
    "config_admin",
    "config_viewer",
    "safety_admin",
    "reviewer",
    "report_operator",
    "sync_operator",
    "data_admin",
    "admin",
)


def require_roles(*allowed: str):
    """依赖工厂：auth 开启时要求当前用户角色在 allowed（或 admin）之内。"""

    def dependency(user=Depends(get_current_user)):
        settings = get_settings()
        if not settings.auth_enabled:
            return None
        if user is None:
            raise ForbiddenError("authentication required")
        if user.role == "admin" or user.role in allowed:
            return user
        raise ForbiddenError(f"role {user.role} is not allowed")

    return dependency
