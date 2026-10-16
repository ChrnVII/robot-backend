import asyncio
import base64
from unittest.mock import patch

import pytest

from app.services.inspection import _render_frames


@pytest.mark.no_database
def test_preview_reference_preserves_public_base64_fields(tmp_path):
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"jpeg" * 20000)
    frame = {"available": True, "camera_id": "cam01", "jpeg_path": str(path)}
    with patch("pathlib.Path.read_bytes", autospec=True, return_value=path.read_bytes()) as read:
        value = _render_frames({"frame": frame, "frames": {"frames": {"cam01": frame}}})
    assert read.call_count == 1
    assert "jpeg_path" not in value["frame"]
    assert base64.b64decode(value["frame"]["jpeg_base64"]) == b"jpeg" * 20000
    assert value["frames"]["frames"]["cam01"] == value["frame"]


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


def test_video_subscriptions_keep_camera_selection_independent(client):
    from app.api.deps import get_inspection_worker_gateway
    gateway = get_inspection_worker_gateway()
    original = gateway.call

    async def frames(request):
        if request.get("operation") == "snapshot":
            camera_id = request.get("camera_id")
            return {"status": {"state": "running"},
                    "frame": {"available": True, "camera_id": camera_id, "jpeg_base64": camera_id}}
        return await original(request)

    gateway.call = frames
    with client.websocket_connect("/ws/video/shared?cameraId=cam01") as one:
        with client.websocket_connect("/ws/video/shared?cameraId=cam02") as two:
            first, second = one.receive_json(), two.receive_json()
    assert first["data"]["frame"]["cameraId"] == "cam01"
    assert second["data"]["frame"]["cameraId"] == "cam02"
    assert set(first) == {"type", "data", "timestamp"}
    assert set(first["data"]) == {"streamId", "status", "frame"}
