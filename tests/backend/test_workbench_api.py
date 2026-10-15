"""工作台域：任务 / 模板 / 运行 / 告警 / 参数 / 地图 / 预置点 / 升降（DB 持久化）。"""


def test_tasks_crud(client):
    assert client.get("/api/v1/tasks").json()["data"] == []
    created = client.post("/api/v1/tasks", json={"taskId": "t-1", "name": "巡检任务", "enabled": True}).json()["data"]
    assert created["taskId"] == "t-1"
    assert created["enabled"] is True
    assert created["createdAt"].endswith("Z")
    assert created["updatedAt"].endswith("Z")

    listed = client.get("/api/v1/tasks").json()["data"]
    assert [item["taskId"] for item in listed] == ["t-1"]

    updated = client.post("/api/v1/tasks", json={"taskId": "t-1", "name": "改名", "enabled": False}).json()["data"]
    assert updated["name"] == "改名"
    assert updated["enabled"] is False
    assert updated["createdAt"] == created["createdAt"]

    assert client.delete("/api/v1/tasks/t-1").json()["data"] == {"deleted": True}
    assert client.get("/api/v1/tasks").json()["data"] == []


def test_tasks_auto_id(client):
    created = client.post("/api/v1/tasks", json={"name": "无 ID"}).json()["data"]
    assert created["taskId"].startswith("task-")
    assert created["enabled"] is True


def test_templates_crud(client):
    created = client.post("/api/v1/task-templates", json={"name": "默认路线"}).json()["data"]
    assert created["templateId"].startswith("tmpl-")
    assert len(client.get("/api/v1/task-templates").json()["data"]) == 1
    assert client.delete(f"/api/v1/task-templates/{created['templateId']}").json()["data"] == {"deleted": True}


def test_runs_resume_and_delete(client):
    resumed = client.post("/api/v1/runs/run-x/resume", json={"currentPosition": 3}).json()["data"]
    assert resumed["taskRunId"] == "run-x"
    assert resumed["status"] == 1
    assert resumed["currentPosition"] == 3
    assert len(client.get("/api/v1/runs").json()["data"]) == 1
    assert client.delete("/api/v1/runs/run-x").json()["data"] == {"deleted": True}


def test_alerts_handle_and_delete(client):
    handled = client.post("/api/v1/alerts/alert-x", json={"status": "closed", "note": "已处理"}).json()["data"]
    assert handled["alertId"] == "alert-x"
    assert handled["status"] == "closed"
    assert handled["note"] == "已处理"
    assert handled["updatedAt"].endswith("Z")
    listed = client.get("/api/v1/alerts").json()["data"]
    assert len(listed) == 1
    assert client.delete("/api/v1/alerts/alert-x").json()["data"] == {"deleted": True}


def test_config_get_save_and_inspection_variant(client):
    config = client.get("/api/v1/config").json()["data"]
    assert config["robotId"] == "robot-dev-001"
    assert config["name"] == "巡检机器人"

    saved = client.put("/api/v1/config", json={"name": "新名称", "robotId": "cannot-change"}).json()["data"]
    assert saved["name"] == "新名称"
    assert saved["robotId"] == "robot-dev-001"  # robotId 不可被覆盖

    inspection = client.get("/api/v1/config?configType=inspection").json()["data"]
    assert [item["value"] for item in inspection["environments"]] == ["simulated", "hardware"]
    assert inspection["modelPath"]


def test_map_stations_and_version_conflict(client):
    initial = client.get("/api/v1/map/stations").json()["data"]
    assert initial[0]["stationId"] == "st-1"

    saved = client.put("/api/v1/map/stations", json={
        "mapVersion": 1, "stations": [{"stationId": "st-9", "station": 9}],
    }).json()["data"]
    assert saved["mapVersion"] == 2
    assert saved["stations"][0]["stationId"] == "st-9"

    stale = client.put("/api/v1/map/stations", json={"mapVersion": 1, "stations": []})
    assert stale.status_code == 400
    assert stale.json()["code"] == "VALIDATION_ERROR"


def test_presets_crud(client):
    assert client.get("/api/v1/cam/presets").json()["data"] == [{"presetId": 1, "name": "预置点 1"}]
    saved = client.post("/api/v1/cam/presets", json={"id": 3}).json()["data"]
    assert saved["presetId"] == 3
    assert saved["name"] == "预置点 3"
    preset_ids = [item["presetId"] for item in client.get("/api/v1/cam/presets").json()["data"]]
    assert preset_ids == [1, 3]
    assert client.delete("/api/v1/cam/presets/3").json()["data"] == {"deleted": True}


def test_lift_state_and_command_effect(client):
    default = client.get("/api/v1/lift/state").json()["data"]
    assert default["enabled"] is False
    assert default["speed"] == 4096

    client.post("/api/v1/devices/commands", json={
        "target": "lift", "action": "lift.enable", "issuer": "ui",
        "leaseId": "l1", "idempotencyKey": "ik-lift-enable", "traceId": "tr",
        "payload": {"enabled": True},
    })
    lift = client.get("/api/v1/lift/state").json()["data"]
    assert lift["enabled"] is True
