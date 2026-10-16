"""巡检与视频域：任务生命周期、检测分页、证据图与视频透传。"""


def test_start_status_stop_inspection(client):
    started = client.post("/api/v1/inspections", json={
        "mode": "custom", "taskRunId": "insp-1", "durationSeconds": 300,
        "cameraIds": ["cam02", "cam05", "cam08"], "pipeline": "inference",
    }).json()["data"]
    assert started["taskRunId"] == "insp-1"
    assert started["mode"] == "custom"
    assert started["pipeline"] == "inference"
    assert started["state"] == "running"
    assert started["cameraIds"] == ["cam02", "cam05", "cam08"]

    status = client.get("/api/v1/inspections/insp-1").json()["data"]
    assert status["state"] == "running"

    stopped = client.post("/api/v1/inspections/insp-1/stop").json()["data"]
    assert stopped["state"] == "stopped"


def test_start_defaults(client):
    data = client.post("/api/v1/inspections", json={}).json()["data"]
    assert data["mode"] == "single"
    assert data["pipeline"] == "inference"
    assert data["taskRunId"].startswith("inspection_")


def test_unknown_inspection_returns_not_found(client):
    response = client.get("/api/v1/inspections/missing-task")
    assert response.status_code == 404
    assert response.json()["code"] == "KEY_ERROR"


def test_detections_empty_without_robot_database(client):
    page = client.get("/api/v1/inspections/any-run/detections").json()["data"]
    assert page == {"items": [], "offset": 0, "limit": 50, "total": 0}
    alias = client.get("/api/v1/inspections/any-run/results").json()["data"]
    assert alias["total"] == 0


def test_evidence_image_not_found_without_evidence(client):
    response = client.get("/api/v1/inspections/insp-1/images/cam01:00000001_result.jpg")
    assert response.status_code == 404
    assert response.json()["code"] == "FILE_NOT_FOUND_ERROR"


def test_invalid_task_id_rejected(client):
    response = client.get("/api/v1/inspections/bad$id")
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_video_stream_lifecycle(client):
    started = client.post("/api/v1/video/streams", json={
        "mode": "custom", "streamId": "video-demo-001", "durationSeconds": 0,
        "cameraIds": ["cam01"],
    }).json()["data"]
    assert started["taskRunId"] == "video-demo-001"
    assert started["pipeline"] == "passthrough"

    status = client.get("/api/v1/video/streams/video-demo-001").json()["data"]
    assert status["state"] == "running"

    stopped = client.delete("/api/v1/video/streams/video-demo-001").json()["data"]
    assert stopped["state"] == "stopped"


def test_video_invalid_stream_id_rejected(client):
    response = client.post("/api/v1/video/streams", json={"streamId": "bad id", "mode": "single"})
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
