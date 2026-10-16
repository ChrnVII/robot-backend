"""数据库引擎 / Session / Base（SQLAlchemy，同步）。

- 后端自有业务库：database_url（PostgreSQL，由 ROBOT_DATABASE_URL 提供）；
- 写入权属：本后端只写自有表（ui_* 工作台数据与用户表）；
  控制表由 robot-core 写入，媒体、巡检与推理表由感知进程写入；
- 表结构统一由 app.migrations 管理，生产关闭自动迁移后只检查版本。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.common.config import get_settings
from app.common.dburl import require_postgres_url


class Base(DeclarativeBase):
    """所有后端自有表的基类。"""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = require_postgres_url(get_settings().database_url, "ROBOT_DATABASE_URL")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def reset_engine() -> None:
    """重建引擎（测试用：先修改环境变量 ROBOT_DATABASE_URL 再调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def init_db() -> None:
    """Apply the same versioned migrations as the robot runtime."""
    from app.migrations import check_database, upgrade_database

    if get_settings().auto_migrate:
        upgrade_database(get_engine())
    else:
        check_database(get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话（写操作由 crud 显式提交）。"""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
