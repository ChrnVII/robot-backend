"""数据库 URL 校验：本平台仅支持 PostgreSQL（SQLite 支持已移除）。"""

from __future__ import annotations

_POSTGRES_SCHEMES = ("postgresql://", "postgresql+psycopg://", "postgres://")


def is_postgres_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(_POSTGRES_SCHEMES)


def psycopg_dsn(url: str) -> str:
    """SQLAlchemy DSN -> psycopg conninfo（psycopg 不识别 ``+psycopg`` 方言后缀）。

    供 WS 事件监听（LISTEN）等直接使用 psycopg 的场景复用，
    避免各处重复实现同一段字符串处理。
    """
    value = str(url)
    if value.startswith("postgresql+psycopg://"):
        return "postgresql://" + value.split("://", 1)[1]
    return value


def require_postgres_url(url: str | None, env_name: str) -> str:
    """返回可用的 PostgreSQL DSN；否则抛出带明确指名的错误。"""
    if not is_postgres_url(url):
        raise RuntimeError(
            f"{env_name} 必须配置为 PostgreSQL DSN（SQLite 支持已移除），"
            "例如 postgresql+psycopg://robot:***@127.0.0.1:5432/robot_platform"
        )
    return str(url)
