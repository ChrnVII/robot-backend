"""媒体域：/media/captures 采集与 device/media 组合校验。"""


def test_capture_camera_image(client):
    data = client.post("/api/v1/media/captures", json={
        "taskRunId": "demo-run-001", "waypointId": "demo-point-001",
        "deviceId": "camera", "mediaType": "image", "parameters": {},
    }).json()["data"]
    assert data["assetId"].startswith("asset_")
    assert data["taskRunId"] == "demo-run-001"
    assert data["waypointId"] == "demo-point-001"
    assert data["deviceId"] == "camera"
    assert data["mediaType"] == "image"
    assert data["mimeType"] == "image/jpeg"
    assert data["mock"] is True


def test_capture_audio(client):
    data = client.post("/api/v1/media/captures", json={
        "taskRunId": "demo-run-001", "waypointId": "p1", "deviceId": "audio",
        "mediaType": "audio", "parameters": {"duration_seconds": 3},
    }).json()["data"]
    assert data["mimeType"] == "audio/wav"


def test_device_media_type_mismatch(client):
    response = client.post("/api/v1/media/captures", json={
        "taskRunId": "r1", "waypointId": "p1", "deviceId": "camera", "mediaType": "audio",
    })
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "不匹配" in body["message"]


def test_capture_validation(client):
    response = client.post("/api/v1/media/captures", json={
        "taskRunId": "r1", "waypointId": "p1", "deviceId": "thermal", "mediaType": "image",
    })
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
