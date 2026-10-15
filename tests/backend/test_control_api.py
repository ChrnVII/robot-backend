"""控制域：租约、命令（幂等/校验）与急停（含工作台副作用）。"""


def test_acquire_and_release_lease(client):
    acquired = client.post(
        "/api/v1/control/leases",
        json={"holder": "vue-user-001", "mode": "manual_local"},
    ).json()
    assert acquired["code"] == "OK"
    lease = acquired["data"]
    assert lease["leaseId"].startswith("lease_")
    assert lease["holder"] == "vue-user-001"
    assert lease["mode"] == "manual_local"
    assert lease["priority"] == 80

    released = client.delete(f"/api/v1/control/leases/{lease['leaseId']}").json()
    assert released["data"] == {"released": True}


def test_lease_validation_error(client):
    response = client.post("/api/v1/control/leases", json={"holder": "x", "mode": "flying"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "data" in body and body["data"]


def test_command_completes_and_mirrors_workbench(client):
    command = {
        "target": "lift", "action": "lift.up", "issuer": "ui-1",
        "leaseId": "lease_x", "idempotencyKey": "ik-1", "traceId": "tr-1",
        "payload": {"task": "task-9"},
    }
    first = client.post("/api/v1/devices/commands", json=command).json()["data"]
    assert first["state"] == "completed"
    assert first["commandId"].startswith("cmd_")

    # 幂等键重复执行 -> duplicate
    second = client.post("/api/v1/devices/commands", json=command).json()["data"]
    assert second["state"] == "duplicate"
    assert second["rejectReason"] == "idempotency_key_exists"

    # 副作用：升降状态更新 + 任务运行记录建立
    lift = client.get("/api/v1/lift/state").json()["data"]
    assert lift["motion"] == "moving_up"
    runs = client.get("/api/v1/runs").json()["data"]
    assert any(run.get("taskId") == "task-9" for run in runs)


def test_command_expiry_validation(client):
    response = client.post("/api/v1/devices/commands", json={
        "target": "motion", "action": "motion.stop", "issuer": "ui",
        "leaseId": "l1", "idempotencyKey": "ik-exp", "traceId": "tr-exp",
        "issuedAt": "2026-08-20T10:00:05Z", "expiresAt": "2026-08-20T10:00:00Z",
    })
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_emergency_stop_creates_critical_alert(client):
    body = client.post("/api/v1/emergency-stop", json={
        "traceId": "trace-estop-001", "reason": "operator_pressed_stop",
    }).json()
    assert body["code"] == "OK"
    assert body["data"]["state"] == "emergency"

    alerts = client.get("/api/v1/alerts").json()["data"]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["level"] == "critical"
    assert alert["status"] == "open"
    assert "operator_pressed_stop" in alert["message"]
    assert alert["source"] == "safety"
