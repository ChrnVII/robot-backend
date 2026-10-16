"""WebSocket：四条实时通道的消息契约与非法 ID 处理。"""

import pytest
from starlette.websockets import WebSocketDisconnect


def test_state_topic(client):
    with client.websocket_connect("/ws/state") as websocket:
        message = websocket.receive_json()
    assert message["type"] == "state"
    assert message["data"]["service"] == "robot-core"
    assert str(message["timestamp"]).endswith("Z")


def test_environment_topic(client):
    with client.websocket_connect("/ws/environment") as websocket:
        message = websocket.receive_json()
    assert message["type"] == "environment"
    assert message["data"]["deviceId"] == "environment"
    assert "temperatureC" in message["data"]["values"]


def test_inspection_topic(client):
    client.post("/api/v1/inspections", json={"taskRunId": "ws-insp", "mode": "single"})
    with client.websocket_connect("/ws/inspections/ws-insp") as websocket:
        message = websocket.receive_json()
    assert message["type"] == "inspection"
    assert message["data"]["status"]["state"] == "running"
    assert message["data"]["detections"]["items"] == []
    assert message["data"]["frame"]["available"] is False
    assert "frames" in message["data"]


def test_video_topic(client):
    client.post("/api/v1/video/streams", json={"streamId": "ws-video", "cameraIds": ["cam01"]})
    with client.websocket_connect("/ws/video/ws-video?cameraId=cam01") as websocket:
        message = websocket.receive_json()
    assert message["type"] == "video"
    assert message["data"]["streamId"] == "ws-video"
    assert message["data"]["frame"]["available"] is False


def test_invalid_inspection_id_closes_with_bad_data(client):
    with client.websocket_connect("/ws/inspections/bad$id") as websocket:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            websocket.receive_json()
    assert excinfo.value.code == 1007


def test_invalid_camera_id_closes_with_bad_data(client):
    client.post("/api/v1/video/streams", json={"streamId": "ws-video-bad"})
    with client.websocket_connect("/ws/video/ws-video-bad?cameraId=bad camera") as websocket:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            websocket.receive_json()
    assert excinfo.value.code == 1007


def test_ws_requires_token_when_auth_enabled(auth_client):
    """鉴权开启时，无令牌的 WS 连接以 1008（策略违规）关闭。"""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with auth_client.websocket_connect("/ws/state") as websocket:
            websocket.receive_json()
    assert excinfo.value.code == 1008


def test_ws_rejects_invalid_token(auth_client):
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with auth_client.websocket_connect("/ws/state?accessToken=not-a-token") as websocket:
            websocket.receive_json()
    assert excinfo.value.code == 1008


def test_ws_accepts_valid_access_token(auth_client):
    token = auth_client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["data"]["accessToken"]
    with auth_client.websocket_connect(f"/ws/state?accessToken={token}") as websocket:
        message = websocket.receive_json()
    assert message["type"] == "state"
