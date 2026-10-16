"""数据库 URL 校验：本平台仅支持 PostgreSQL（SQLite 支持已移除）。"""

from __future__ import annotations

_POSTGRES_SCHEMES = ("postgresql://", "postgresql+psycopg://", "postgres://")


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(_POSTGRES_SCHEMES)


def require_postgres_url(url: str | None, env_name: str) -> str:
    """返回可用的 PostgreSQL DSN；否则抛出带明确指名的错误。"""
    if not is_postgres_url(url):
        raise RuntimeError(
            f"{env_name} 必须配置为 PostgreSQL DSN（SQLite 支持已移除），"
            "例如 postgresql+psycopg://robot:***@127.0.0.1:5432/robot_platform"
        )
    return str(url)
