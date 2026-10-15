"""系统域接口：探针、平台信息、统一封套与 TraceId 行为。"""


def test_healthz_envelope_and_trace(client):
    response = client.get("/healthz", headers={"X-Trace-Id": "trace-test-001"})
    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "trace-test-001"
    body = response.json()
    assert body["code"] == "OK"
    assert body["message"] == "success"
    assert body["traceId"] == "trace-test-001"
    assert str(body["timestamp"]).endswith("Z")
    data = body["data"]
    assert data["status"] == "ok"
    assert data["coreStatus"] == "ok"
    assert data["service"] == "inspection-robot-backend"
    assert data["robotId"] == "robot-dev-001"


def test_trace_id_is_generated_when_header_invalid(client):
    response = client.get("/healthz", headers={"X-Trace-Id": "bad trace!"})
    assert response.status_code == 200
    generated = response.headers["X-Trace-Id"]
    assert generated and generated != "bad trace!"
    assert response.json()["traceId"] == generated


def test_readyz_and_version(client):
    ready = client.get("/readyz").json()
    assert ready["code"] == "OK" and ready["data"] == {"ready": True, "status": "ok"}
    version = client.get("/version").json()
    assert version["data"]["apiVersion"] == "v1"
    assert version["data"]["version"]


def test_platform(client):
    body = client.get("/api/v1/platform").json()
    assert body["code"] == "OK"
    assert body["data"]["robotId"] == "robot-dev-001"
    assert body["data"]["mode"] == "simulated"
    assert body["data"]["apiVersion"] == "v1"


def test_unknown_route_returns_error_envelope(client):
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "HTTP_ERROR"
    assert "traceId" in body
