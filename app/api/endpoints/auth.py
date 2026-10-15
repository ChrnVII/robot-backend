"""认证与用户域：/auth/login、/auth/refresh、/auth/logout、/users/me。

迁移来源：目标 API（old_project/backend/docs/API.md §6.1）；实现见 app/security/。
默认 auth_enabled=false（开放模式，保持旧前端联调契约）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import current_settings
from app.common.clock import utc_now, utc_now_iso
from app.common.responses import ok, trace_of
from app.db import crud
from app.db.database import get_db
from app.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest
from app.security.auth import (
    UnauthorizedError,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    token_hash,
    verify_password,
)

router = APIRouter()


@router.post("/auth/login", tags=["auth"])
def login(request: Request, body: LoginRequest, session: Session = Depends(get_db)) -> dict:
    settings = current_settings()
    user = crud.users.get_user_by_username(session, body.username)
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise UnauthorizedError("invalid username or password")
    access = create_access_token(user.username, user.role, settings)
    refresh, refresh_digest, expires_at = create_refresh_token(user.username, settings)
    crud.users.add_refresh_token(session, user.id, refresh_digest, expires_at, utc_now_iso())
    return ok({
        "accessToken": access,
        "refreshToken": refresh,
        "tokenType": "Bearer",
        "expiresIn": settings.access_token_ttl_seconds,
        "user": {"username": user.username, "role": user.role},
    }, trace_of(request))


@router.post("/auth/refresh", tags=["auth"])
def refresh(request: Request, body: RefreshRequest, session: Session = Depends(get_db)) -> dict:
    settings = current_settings()
    claims = decode_token(body.refreshToken, settings, "refresh")
    row = crud.users.get_refresh_token(session, token_hash(body.refreshToken))
    if row is None or row.revoked:
        raise UnauthorizedError("refresh token revoked")
    if _is_expired(str(row.expires_at)):
        raise UnauthorizedError("refresh token expired")
    user = crud.users.get_user_by_username(session, str(claims.get("sub", "")))
    if user is None or not user.is_active:
        raise UnauthorizedError("unknown or disabled user")
    access = create_access_token(user.username, user.role, settings)
    return ok({
        "accessToken": access,
        "tokenType": "Bearer",
        "expiresIn": settings.access_token_ttl_seconds,
    }, trace_of(request))


@router.post("/auth/logout", tags=["auth"])
def logout(request: Request, body: LogoutRequest, session: Session = Depends(get_db)) -> dict:
    revoked = crud.users.revoke_refresh_token(session, token_hash(body.refreshToken))
    return ok({"revoked": revoked}, trace_of(request))


@router.get("/users/me", tags=["auth"])
def me(request: Request, user=Depends(get_current_user)) -> dict:
    settings = current_settings()
    if user is None:
        # 开放模式：返回匿名视图者信息，前端无需登录流程即可联调
        return ok({"username": "anonymous", "role": "viewer", "authEnabled": False}, trace_of(request))
    return ok({"username": user.username, "role": user.role, "authEnabled": True}, trace_of(request))


def _is_expired(value: str) -> bool:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed <= utc_now().astimezone(timezone.utc)
