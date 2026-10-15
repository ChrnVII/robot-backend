"""鉴权域：登录 / 刷新 / 登出 / me 与全局 Bearer 校验（auth 开启模式）。"""


def _login(client, username="admin", password="admin123"):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_login_success_and_me(auth_client):
    body = _login(auth_client).json()
    assert body["code"] == "OK"
    data = body["data"]
    assert data["tokenType"] == "Bearer"
    assert data["accessToken"] and data["refreshToken"]
    assert data["user"] == {"username": "admin", "role": "admin"}

    anonymous = auth_client.get("/api/v1/users/me")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "UNAUTHORIZED"

    me = auth_client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {data['accessToken']}"})
    assert me.json()["data"] == {"username": "admin", "role": "admin", "authEnabled": True}


def test_login_rejects_bad_password(auth_client):
    response = _login(auth_client, password="wrong")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_protected_routes_require_token_when_enabled(auth_client):
    assert auth_client.get("/api/v1/robot/state").status_code == 401
    token = _login(auth_client).json()["data"]["accessToken"]
    allowed = auth_client.get("/api/v1/robot/state", headers={"Authorization": f"Bearer {token}"})
    assert allowed.status_code == 200
    # 根路径探针保持公开
    assert auth_client.get("/healthz").status_code == 200


def test_refresh_and_logout(auth_client):
    data = _login(auth_client).json()["data"]
    refreshed = auth_client.post("/api/v1/auth/refresh", json={"refreshToken": data["refreshToken"]}).json()
    assert refreshed["code"] == "OK"
    assert refreshed["data"]["accessToken"]

    revoked = auth_client.post("/api/v1/auth/logout", json={"refreshToken": data["refreshToken"]}).json()
    assert revoked["data"] == {"revoked": True}

    after_logout = auth_client.post("/api/v1/auth/refresh", json={"refreshToken": data["refreshToken"]})
    assert after_logout.status_code == 401


def test_open_mode_me_returns_anonymous(client):
    data = client.get("/api/v1/users/me").json()["data"]
    assert data == {"username": "anonymous", "role": "viewer", "authEnabled": False}
