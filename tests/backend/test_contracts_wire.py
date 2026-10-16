"""共享契约核：错误码归一化与 IPC 载荷模型（双端共用）。

纯单元用例：不依赖 PostgreSQL 与机器人进程。
"""

import pytest

from app.contracts import (
    Channels,
    CommandWire,
    EmergencyStopWire,
    EventTopic,
    IpcErrorCode,
    LeaseAcquireWire,
    LeaseReleaseWire,
    Op,
    ipc_error_code,
    is_not_found_code,
)
from app.contracts.wire import WireError

pytestmark = pytest.mark.no_database


def test_ipc_error_code_normalizes_exceptions():
    assert ipc_error_code(KeyError("missing")) == IpcErrorCode.NOT_FOUND
    assert ipc_error_code(FileNotFoundError("gone")) == IpcErrorCode.NOT_FOUND
    assert ipc_error_code(ValueError("bad field")) == IpcErrorCode.INVALID_REQUEST
    assert ipc_error_code(TypeError("bad type")) == IpcErrorCode.INVALID_REQUEST
    assert ipc_error_code(RuntimeError("boom")) == IpcErrorCode.INTERNAL


def test_not_found_code_accepts_legacy_exception_names():
    # 滚动升级期间旧版五进程仍以异常类名回传错误码。
    assert is_not_found_code(IpcErrorCode.NOT_FOUND)
    assert is_not_found_code("KeyError")
    assert is_not_found_code("FileNotFoundError")
    assert not is_not_found_code(IpcErrorCode.INVALID_REQUEST)


def test_command_wire_round_trip_preserves_wire_format():
    wire = CommandWire(
        target="lift", action="lift.up", issuer="operator", lease_id="lease-1",
        idempotency_key="idem-1", trace_id="trace-1",
        issued_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:00:01Z",
        payload={"speed": 1},
    )
    payload = wire.to_payload()
    # 线上字段名与旧契约逐字一致（snake_case），不得漂移。
    assert set(payload) == {
        "operation", "target", "action", "issuer", "lease_id",
        "idempotency_key", "trace_id", "issued_at", "expires_at", "payload",
    }
    assert payload["operation"] == Op.COMMAND
    assert CommandWire.from_mapping(payload) == wire


def test_command_wire_reports_missing_field():
    try:
        CommandWire.from_mapping({"action": "lift.up"})
    except WireError as error:
        assert "target" in str(error)
    else:  # pragma: no cover - 必须抛出
        raise AssertionError("missing field must raise WireError")


def test_command_wire_uses_caller_defaults_for_timestamps():
    wire = CommandWire.from_mapping(
        {
            "target": "lift", "action": "lift.up", "issuer": "op", "lease_id": "l",
            "idempotency_key": "k", "trace_id": "t",
        },
        default_issued_at="2026-01-01T00:00:00Z",
        default_expires_at="2026-01-01T00:00:01Z",
    )
    assert wire.issued_at == "2026-01-01T00:00:00Z"
    assert wire.expires_at == "2026-01-01T00:00:01Z"


def test_lease_and_emergency_stop_payloads():
    acquire = LeaseAcquireWire(holder="operator", mode="manual_remote").to_payload()
    assert acquire == {"operation": Op.LEASE_ACQUIRE, "holder": "operator", "mode": "manual_remote"}

    release = LeaseReleaseWire.from_mapping({"lease_id": "lease-1"})
    assert release.reason == "released"  # 缺省值与旧实现一致

    stop = EmergencyStopWire.from_mapping({"trace_id": "trace-1"})
    assert stop.reason == "requested"
    assert stop.to_payload() == {"operation": Op.EMERGENCY_STOP, "reason": "requested", "trace_id": "trace-1"}


def test_event_channel_and_topics_are_shared_constants():
    assert Channels.EVENTS == "robot_events"
    assert (EventTopic.STATE, EventTopic.ENVIRONMENT, EventTopic.INSPECTION) == (
        "state", "environment", "inspection",
    )


def test_contracts_kernel_has_no_application_dependencies():
    """契约核必须保持零依赖：不得 import app.* 其他子包。"""
    from pathlib import Path

    package = Path(__file__).resolve().parents[2] / "app" / "contracts"
    for source in package.glob("*.py"):
        for line in source.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import app.", "from app.")):
                assert "app.contracts" in stripped, f"{source.name} 不得依赖应用层：{stripped}"
