"""测试夹具：全部用例运行在 PostgreSQL 测试库上（SQLite 支持已移除）。

数据库地址来自环境变量 ``ROBOT_TEST_DATABASE_URL``；未设置时使用本地默认
``postgresql+psycopg://robot:robot@127.0.0.1:5432/robot_platform_test``。
每个用例开始前重建 public schema 保证完全隔离；PostgreSQL 不可用时整组跳过。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

_DEFAULT_TEST_DSN = "postgresql+psycopg://robot:robot@127.0.0.1:5432/robot_platform_test"
TEST_DSN = os.environ.get("ROBOT_TEST_DATABASE_URL") or _DEFAULT_TEST_DSN
os.environ["ROBOT_TEST_DATABASE_URL"] = TEST_DSN

_CACHED_PROVIDERS = (
    "get_robot_core_gateway",
    "get_sensor_worker_gateway",
    "get_inspection_worker_gateway",
    "get_robot_service",
    "get_control_service",
    "get_media_service",
    "get_inspection_service",
    "get_inference_query_service",
)


def _raw_dsn(url: str) -> str:
    """psycopg（非 SQLAlchemy）不识别 ``+psycopg`` 后缀。"""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _reset_schema() -> None:
    import psycopg

    database_name = TEST_DSN.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in database_name.lower():
        raise RuntimeError(
            f"拒绝清空非测试库（库名“{database_name}”不含 test）：{TEST_DSN}；"
            "请将 ROBOT_TEST_DATABASE_URL 指向专用 PostgreSQL 测试库。"
        )
    with psycopg.connect(_raw_dsn(TEST_DSN), autocommit=True, connect_timeout=5) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")


def _reset_all() -> None:
    from app.api import deps
    from app.common.config import get_settings
    from app.db.database import reset_engine

    for name in _CACHED_PROVIDERS:
        getattr(deps, name).cache_clear()
    get_settings.cache_clear()
    reset_engine()


def _dispose_all() -> None:
    from app.api import deps

    if deps.get_inference_query_service.cache_info().currsize:
        deps.get_inference_query_service().dispose()
    _reset_all()


@pytest.fixture(scope="session", autouse=True)
def _postgres_available():
    try:
        import psycopg

        with psycopg.connect(_raw_dsn(TEST_DSN), connect_timeout=5):
            pass
    except Exception as error:  # noqa: BLE001 - 明确提示后整组跳过
        pytest.skip(
            f"PostgreSQL 测试库不可用（{TEST_DSN}）：{error}。"
            "请设置 ROBOT_TEST_DATABASE_URL 指向可用的 PostgreSQL 测试库后重跑。"
        )


@pytest.fixture(autouse=True)
def _clean_database(_postgres_available):
    _reset_schema()
    yield


def _make_client(monkeypatch, **env: str) -> TestClient:
    monkeypatch.setenv("ROBOT_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("ROBOT_ROBOT_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("ROBOT_AUTH_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _reset_all()
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def client(monkeypatch):
    test_client = _make_client(monkeypatch)
    with test_client:
        yield test_client
    _dispose_all()


@pytest.fixture()
def auth_client(monkeypatch):
    test_client = _make_client(
        monkeypatch,
        ROBOT_AUTH_ENABLED="true",
        ROBOT_JWT_SECRET="test-secret-0123456789abcdef0123456789abcdef",
    )
    with test_client:
        yield test_client
    _dispose_all()
