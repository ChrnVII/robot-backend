"""登录与令牌：JWT 签发与校验、密码散列、当前用户依赖。

- auth_enabled=False（默认）：所有接口开放，保持旧前端联调契约；
- auth_enabled=True：/api/v1/*（除 /auth/* 与系统只读接口）校验 Bearer 访问令牌；
- 密码散列使用 stdlib pbkdf2_hmac（不引入额外依赖）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.common.clock import iso_z, utc_now, utc_now_iso
from app.common.config import Settings, get_settings
from app.common.exceptions import AppError
from app.db import crud
from app.db.database import get_db

PBKDF2_ITERATIONS = 120_000


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2$sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, digest_name, iterations, salt_hex, expected_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2" or digest_name != "sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations),
    )
    return hmac.compare_digest(digest.hex(), expected_hex)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _encode(claims: dict[str, Any], settings: Settings) -> str:
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(username: str, role: str, settings: Settings) -> str:
    now = utc_now()
    return _encode({
        "sub": username,
        "role": role,
        "type": "access",
        "jti": secrets.token_hex(16),
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
    }, settings)


def create_refresh_token(username: str, settings: Settings) -> tuple[str, str, str]:
    """返回（令牌明文、令牌散列、过期时间 ISO）。"""
    now = utc_now()
    expires = now + timedelta(seconds=settings.refresh_token_ttl_seconds)
    token = _encode({
        "sub": username,
        "type": "refresh",
        "jti": secrets.token_hex(16),
        "iat": now,
        "exp": expires,
    }, settings)
    return token, token_hash(token), iso_z(expires)


def decode_token(token: str, settings: Settings, expected_type: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("invalid or expired token") from exc
    if claims.get("type") != expected_type:
        raise UnauthorizedError("invalid token type")
    return claims


def get_current_user(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """当前用户依赖：auth 关闭时返回 None（开放模式）。"""
    if not settings.auth_enabled:
        return None
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise UnauthorizedError("missing bearer token")
    claims = decode_token(header[len("Bearer "):].strip(), settings, "access")
    user = crud.users.get_user_by_username(session, str(claims.get("sub", "")))
    if user is None or not user.is_active:
        raise UnauthorizedError("unknown or disabled user")
    return user


def require_authentication(user=Depends(get_current_user)):
    """全局依赖：auth 关闭时零影响；开启时 /api/v1（除 /auth/*）必须携带有效 Bearer。"""
    settings = get_settings()
    if settings.auth_enabled and user is None:
        raise UnauthorizedError("authentication required")
    return user


def ensure_default_admin(session: Session, settings: Settings) -> bool:
    """auth 开启且无用户时，创建默认管理员（生产必须立即改密）。"""
    if crud.users.count_users(session) > 0:
        return False
    crud.users.create_user(
        session,
        username=settings.default_admin_username,
        password_hash=hash_password(settings.default_admin_password),
        role="admin",
        created_at=utc_now_iso(),
    )
    return True
