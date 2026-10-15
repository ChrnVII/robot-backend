"""鉴权与权限（JWT；默认关闭以保持旧前端联调契约，生产通过 ROBOT_AUTH_ENABLED=true 开启）。"""

from app.security.auth import (
    ForbiddenError,
    UnauthorizedError,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    require_authentication,
    token_hash,
    verify_password,
)
from app.security.permissions import ROLES, require_roles

__all__ = [
    "ForbiddenError",
    "ROLES",
    "UnauthorizedError",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_current_user",
    "hash_password",
    "require_authentication",
    "require_roles",
    "token_hash",
    "verify_password",
]
