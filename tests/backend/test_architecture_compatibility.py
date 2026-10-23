import asyncio
import base64
from unittest.mock import patch

import pytest

@pytest.mark.parametrize(
    "state,status,code",
    [
        # rejected：设备动作未发生 → 409，客户端可刷新租约后重试一次。
        ("rejected", 409, "COMMAND_REJECTED"),
        # failed：已下发但失败，可能部分生效 → 500，禁止自动重试。
        ("failed", 500, "COMMAND_FAILED"),
    ],
)
def test_rejected_command_returns_conflict_without_workbench_side_effects(client, state, status, code):
    from app.api.deps import get_robot_core_gateway
    gateway = get_robot_core_gateway()
    original = gateway.call

    async def reject(request):
        if request.get("operation") == "command":
            return {"command_id": "rejected-1", "state": state, "reject_reason": "control_lease_invalid"}
        return await original(request)

    gateway.call = reject
    response = client.post("/api/v1/devices/commands", json={
        "target": "lift", "action": "lift.up", "issuer": "ui", "leaseId": "lease_x",
        "idempotencyKey": "rejected", "traceId": "trace", "payload": {"task": "task-rejected"},
    })
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.json()["message"] == "control_lease_invalid"
    assert response.json()["data"]["state"] == state
    assert client.get("/api/v1/lift/state").json()["data"]["motion"] == "idle"
    assert client.get("/api/v1/runs").json()["data"] == []


def test_emergency_ack_survives_workbench_database_failure(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("workbench database offline")

    monkeypatch.setattr("app.services.workbench.WorkbenchService.create_alert", fail)
    response = client.post("/api/v1/emergency-stop", json={"traceId": "offline-ui", "reason": "operator"})
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "emergency"
