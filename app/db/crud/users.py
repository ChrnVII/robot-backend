"""用户与刷新令牌数据访问（users / refresh_tokens 表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RefreshToken, User


def get_user_by_username(session: Session, username: str) -> User | None:
    return session.scalars(select(User).where(User.username == username)).first()


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def count_users(session: Session) -> int:
    return len(session.scalars(select(User.id)).all())


def create_user(session: Session, username: str, password_hash: str, role: str, created_at: str) -> User:
    user = User(username=username, password_hash=password_hash, role=role, created_at=created_at)
    session.add(user)
    session.commit()
    return user


def add_refresh_token(session: Session, user_id: int, token_hash: str, expires_at: str, created_at: str) -> None:
    session.add(RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at, created_at=created_at))
    session.commit()


def get_refresh_token(session: Session, token_hash: str) -> RefreshToken | None:
    return session.scalars(select(RefreshToken).where(RefreshToken.token_hash == token_hash)).first()


def revoke_refresh_token(session: Session, token_hash: str) -> bool:
    row = get_refresh_token(session, token_hash)
    if row is None or row.revoked:
        return False
    row.revoked = True
    session.commit()
    return True
