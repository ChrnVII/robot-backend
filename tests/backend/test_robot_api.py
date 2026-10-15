"""机器人域读接口：/robot/state、/environment、/devices/{deviceId}/state。"""


def test_robot_state(client):
    body = client.get("/api/v1/robot/state").json()
    assert body["code"] == "OK"
    data = body["data"]
    assert data["service"] == "robot-core"
    assert data["state"] == "idle"
    assert "environment" in data["devices"]
    assert data["devices"]["environment"]["health"] == "ok"


def test_environment_is_camelized(client):
    data = client.get("/api/v1/environment").json()["data"]
    assert data["deviceId"] == "environment"
    assert data["quality"] == "valid"
    assert data["source"] == "python-mock"
    assert "sampledAt" in data
    values = data["values"]
    assert values["temperatureC"] == 24.8
    assert values["humidityPercent"] == 68.5


def test_device_state_lift(client):
    data = client.get("/api/v1/devices/lift/state").json()["data"]
    assert data["deviceId"] == "lift"
    assert data["values"]["motion"] == "idle"
    assert data["values"]["enabled"] is False


def test_invalid_device_id_rejected(client):
    response = client.get("/api/v1/devices/bad$id/state")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "INVALID_DEVICE_ID"
    assert "unsupported characters" in body["message"]
