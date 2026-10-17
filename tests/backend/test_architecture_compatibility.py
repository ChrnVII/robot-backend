import asyncio
import base64
from unittest.mock import patch

import pytest

def test_rejected_command_does_not_mirror_success_into_workbench(client):
    from app.api.deps import get_robot_core_gateway
    gateway = get_robot_core_gateway()
    original = gateway.call

    async def reject(request):
        if request.get("operation") == "command":
            return {"command_id": "rejected-1", "state": "rejected", "reject_reason": "control_lease_invalid"}
        return await original(request)

    gateway.call = reject
    response = client.post("/api/v1/devices/commands", json={
        "target": "lift", "action": "lift.up", "issuer": "ui", "leaseId": "lease_x",
        "idempotencyKey": "rejected", "traceId": "trace", "payload": {"task": "task-rejected"},
    })
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "rejected"
    assert client.get("/api/v1/lift/state").json()["data"]["motion"] == "idle"
    assert client.get("/api/v1/runs").json()["data"] == []


def test_emergency_ack_survives_workbench_database_failure(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("workbench database offline")

    monkeypatch.setattr("app.services.workbench.WorkbenchService.create_alert", fail)
    response = client.post("/api/v1/emergency-stop", json={"traceId": "offline-ui", "reason": "operator"})
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "emergency"
