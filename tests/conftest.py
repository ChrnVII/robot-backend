"""测试夹具：全部用例运行在 PostgreSQL 测试库上（SQLite 支持已移除）。

数据库地址来自环境变量 ``ROBOT_TEST_DATABASE_URL``；未设置时使用本地默认
``postgresql+psycopg://robot:robot@127.0.0.1:5432/robot_platform_test``。
每个用例开始前重建 public schema 保证完全隔离；PostgreSQL 不可用时整组跳过。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.common.clock import utc_now, utc_now_iso
from app.gateways.base import InspectionWorkerError, RobotCoreError, SensorWorkerError

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


# ---------------------------------------------------------------------------
# 测试替身：后端接口测试不依赖真实机器人五进程（UDS），因此在测试侧用进程内
# 替身替换网关实现。替身仅存在于测试代码中；应用代码已不包含任何 mock 实现。
# ---------------------------------------------------------------------------


class FakeRobotCoreGateway:
    """进程内替身：设备值、命令幂等与急停状态（仅在测试中使用）。"""

    def __init__(self) -> None:
        self._commands: dict[str, dict[str, Any]] = {}
        self._device_values: dict[str, dict[str, Any]] = {}
        self._robot_state = "idle"

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = str(request.get("operation"))
        if operation == "health":
            return self._health()
        if operation == "device.state":
            return self._device_state(str(request.get("device_id")))
        if operation == "lease.acquire":
            return self._acquire_lease(request)
        if operation == "lease.release":
            return {"released": True}
        if operation == "command":
            return self._execute_command(request)
        if operation == "emergency.stop":
            return self._emergency_stop(request)
        raise RobotCoreError("unsupported_operation", f"Unsupported core operation: {operation}")

    def _health(self) -> dict[str, Any]:
        devices = {
            name: self._device_state(name)
            for name in ("environment", "motion", "battery", "lift", "light", "camera")
        }
        return {"service": "robot-core", "state": self._robot_state, "timestamp": utc_now_iso(), "devices": devices}

    def _device_state(self, device_id: str) -> dict[str, Any]:
        defaults: dict[str, dict[str, Any]] = {
            "environment": {"temperature_c": 24.8, "humidity_percent": 68.5, "ammonia_ppm": 12.3, "illuminance_lux": 320.0},
            "battery": {"soc_percent": 82.0, "battery_percent": 82.0, "voltage_v": 25.4},
            "motion": {"speed_mps": 0.0, "moving": False, "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0}, "position_m": 0.0},
            "lift": {"height": 0.0, "motion": "idle", "enabled": False, "fault_code": 0},
            "light": {"enabled": False, "mode": 0},
            "camera": {"online": True, "streaming": True, "preset": 1, "fps": 15, "resolution": "1280x720"},
        }
        values = dict(defaults.get(device_id, {}))
        values.update(self._device_values.get(device_id, {}))
        return {
            "device_id": device_id,
            "health": "ok",
            "sampled_at": utc_now_iso(),
            "source": "python-mock",
            "values": values,
            "quality": "valid",
        }

    def _acquire_lease(self, request: Mapping[str, Any]) -> dict[str, Any]:
        issued = utc_now()
        return {
            "lease_id": f"lease_{uuid4()}",
            "holder": request.get("holder"),
            "mode": request.get("mode"),
            "priority": self._priority(str(request.get("mode"))),
            "issued_at": issued.isoformat(),
            "expires_at": (issued + timedelta(seconds=3)).isoformat(),
        }

    def _execute_command(self, request: Mapping[str, Any]) -> dict[str, Any]:
        idempotency_key = str(request.get("idempotency_key"))
        existing = self._commands.get(idempotency_key)
        if existing is not None:
            return {
                "command_id": existing["command_id"],
                "state": "duplicate",
                "completed_at": utc_now_iso(),
                "details": {"original_state": existing["state"]},
                "reject_reason": "idempotency_key_exists",
            }
        target = str(request.get("target"))
        action = str(request.get("action"))
        payload = dict(request.get("payload") or {})
        state = self._device_values.setdefault(target, {})
        if action.endswith(".up"):
            state["motion"] = "moving_up"; state["moving"] = True
        elif action.endswith(".down"):
            state["motion"] = "moving_down"; state["moving"] = True
        elif action.endswith(".stop") or action.endswith(".home"):
            state["motion"] = "idle"; state["moving"] = False
        elif action.endswith(".move_to"):
            state["height"] = payload.get("height"); state["motion"] = "positioning"
        elif action.endswith(".enable"):
            state["enabled"] = payload.get("enabled")
        elif action == "light.on":
            state["enabled"] = True
        elif action == "light.off":
            state["enabled"] = False
        elif action == "camera.goto_preset":
            state["preset"] = payload.get("preset")
        elif action == "camera.ptz":
            state["lastPtz"] = payload
        elif action == "camera.reboot":
            state["online"] = True
        result = {
            "command_id": f"cmd_{uuid4()}",
            "state": "completed",
            "completed_at": utc_now_iso(),
            "details": {"target": target, "action": action, "mock": True},
        }
        self._commands[idempotency_key] = dict(result)
        return result

    def _emergency_stop(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._robot_state = "emergency"
        return {"state": self._robot_state, "reason": request.get("reason", "requested"), "timestamp": utc_now_iso()}

    @staticmethod
    def _priority(mode: str) -> int:
        return {
            "emergency": 100,
            "manual_local": 80,
            "manual_remote": 60,
            "maintenance": 40,
        }.get(mode, 20)


class FakeSensorWorkerGateway:
    """进程内替身：仅支持 capture，返回替身资产（仅在测试中使用）。"""

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("operation") != "capture":
            raise SensorWorkerError("unsupported_operation", f"Unsupported sensor operation: {request.get('operation')}")
        media_type = str(request.get("media_type"))
        extension = ".wav" if media_type == "audio" else ".jpg" if media_type == "image" else ".bin"
        mime_type = "audio/wav" if media_type == "audio" else "image/jpeg" if media_type == "image" else "application/octet-stream"
        asset_id = f"asset_{uuid4()}"
        return {
            "asset_id": asset_id,
            "task_run_id": request.get("task_run_id"),
            "waypoint_id": request.get("waypoint_id"),
            "device_id": request.get("device_id"),
            "media_type": media_type,
            "mime_type": mime_type,
            "path": f"mock/{asset_id}{extension}",
            "captured_at": utc_now_iso(),
            "mock": True,
        }


class FakeInspectionWorkerGateway:
    """进程内替身：内存任务表，检测列表为空（仅在测试中使用）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = str(request.get("operation"))
        if operation == "health":
            return {"state": "ok", "active": {"state": "idle"}}
        if operation == "start":
            return self._start(request)
        if operation == "stop":
            return self._stop(str(request.get("task_run_id")))
        if operation == "status":
            return self._status(str(request.get("task_run_id") or ""))
        if operation == "detections":
            return {
                "items": [],
                "offset": request.get("offset", 0),
                "limit": request.get("limit", 50),
                "total": 0,
            }
        if operation == "latest_frame":
            return {"available": False, "task_run_id": str(request.get("task_run_id") or "")}
        if operation == "latest_frames":
            return {"task_run_id": str(request.get("task_run_id") or ""), "frames": {}}
        if operation == "resolve_image":
            raise InspectionWorkerError("FileNotFoundError", "test double does not contain inspection evidence images")
        raise InspectionWorkerError("unsupported_operation", f"Unsupported inspection operation: {operation}")

    def _start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        task_run_id = str(request.get("task_run_id") or f"inspection_{uuid4()}")
        task = {
            "task_run_id": task_run_id,
            "mode": str(request.get("mode", "single")),
            "pipeline": str(request.get("pipeline", "inference")),
            "state": "running",
            "processed": 0,
            "positive": 0,
            "camera_ids": list(request.get("camera_ids") or []),
        }
        self._tasks[task_run_id] = task
        return dict(task)

    def _stop(self, task_run_id: str) -> dict[str, Any]:
        task = self._task(task_run_id)
        task["state"] = "stopped"
        return dict(task)

    def _status(self, task_run_id: str) -> dict[str, Any]:
        if not task_run_id:
            return {"state": "idle"}
        return dict(self._task(task_run_id))

    def _task(self, task_run_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_run_id)
        if task is None:
            raise InspectionWorkerError("KeyError", "inspection task not found")
        return task


def _install_fake_gateways(monkeypatch) -> None:
    """用测试替身替换 deps 中的网关工厂；配合缓存清理在每次用例生效。"""
    from app.api import deps

    monkeypatch.setattr(deps, "create_robot_core_gateway", lambda _settings: FakeRobotCoreGateway())
    monkeypatch.setattr(deps, "create_sensor_worker_gateway", lambda _settings: FakeSensorWorkerGateway())
    monkeypatch.setattr(deps, "create_inspection_worker_gateway", lambda _settings: FakeInspectionWorkerGateway())


def _make_client(monkeypatch, **env: str) -> TestClient:
    monkeypatch.setenv("ROBOT_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("ROBOT_AUTH_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _install_fake_gateways(monkeypatch)
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
