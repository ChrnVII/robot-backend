"""后端测试夹具：每个用例使用独立临时 SQLite，并重置全局缓存（配置 / 引擎 / 网关）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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


def _make_client(monkeypatch, tmp_path, **env: str) -> TestClient:
    base = tmp_path.as_posix()
    monkeypatch.setenv("ROBOT_DATABASE_URL", f"sqlite:///{base}/app.db")
    monkeypatch.setenv("ROBOT_ROBOT_DATABASE_URL", f"sqlite:///{base}/robot.db")
    monkeypatch.setenv("ROBOT_AUTH_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _reset_all()
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    test_client = _make_client(monkeypatch, tmp_path)
    with test_client:
        yield test_client
    _dispose_all()


@pytest.fixture()
def auth_client(monkeypatch, tmp_path):
    test_client = _make_client(
        monkeypatch, tmp_path,
        ROBOT_AUTH_ENABLED="true",
        ROBOT_JWT_SECRET="test-secret-0123456789abcdef0123456789abcdef",
    )
    with test_client:
        yield test_client
    _dispose_all()
