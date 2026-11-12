"""设备注册流程（B 端）。

用 `httpx.MockTransport` 模拟 A 的三种返回：直接放行、待批准、明确拒绝。
把 POLL_INTERVAL 压到毫秒级，这样"等待批准"也能在测试里真实走一遍。
"""

from __future__ import annotations

import httpx
import pytest

from app.cloud_agent import enroll as enroll_module
from app.cloud_agent.config import AgentConfig, ConfigError
from app.cloud_agent.credentials import Credentials, load, save
from app.cloud_agent.enroll import EnrollError, ensure_credentials, rotate

pytestmark = pytest.mark.no_database


def make_config(tmp_path, **kwargs) -> AgentConfig:
    return AgentConfig(
        server="http://127.0.0.1:8000",
        root=str(tmp_path / "share"),
        state_dir=str(tmp_path / "state"),
        **kwargs,
    )


def install_transport(monkeypatch, handler):
    monkeypatch.setattr(
        enroll_module,
        "_client",
        lambda config: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8000"
        ),
    )


def health(instance_id: str | None = "inst-1") -> httpx.Response:
    """A 的 `/api/v1/health`：设备靠 instance_id 认出"换了地址还是同一台"。"""
    return httpx.Response(
        200,
        json={"status": "ok", "version": "test", "protocol": 1, "instance_id": instance_id},
    )


def test_enroll_ok_persists_credentials(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return health("inst-1")
        assert request.url.path == "/internal/enroll"
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "rma_new"})

    install_transport(monkeypatch, handler)

    resolved = await_ensure(config)

    assert resolved.token == "rma_new"
    assert resolved.agent_id == "a1"
    stored = load(config)
    assert stored is not None
    assert stored.token == "rma_new"
    # 顺手记下 A 的身份：下次它换地址时靠这个认人
    assert stored.instance_id == "inst-1"
    # 内存里也要有：否则下次轮换 token 写文件时会把 instance_id 覆盖成 null
    assert resolved.instance_id == "inst-1"


def test_existing_credentials_skip_enrollment(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    save(config, Credentials(server="http://127.0.0.1:8000", token="rma_old", agent_id="a1"))

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("有凭据时不该再请求 A")

    install_transport(monkeypatch, handler)
    fresh = AgentConfig(root=str(tmp_path / "share"), state_dir=str(tmp_path / "state"))

    resolved = await_ensure(fresh)

    assert resolved.token == "rma_old"
    assert resolved.server == "http://127.0.0.1:8000"


def test_adopts_same_server_when_only_the_address_changed(tmp_path, monkeypatch):
    """A 换了域名/IP：认出是同一台，沿用原 token，不重新注册。

    这是这次要堵的洞——以前这里会走注册流程，A 上就凭空多出一台 `-2` 节点。
    """
    save(
        AgentConfig(server="http://old.example.com", state_dir=str(tmp_path / "state")),
        Credentials(
            server="http://old.example.com",
            token="rma_old",
            agent_id="id-old",
            root="/srv/share",
            name="lab-01",
            instance_id="inst-1",
        ),
    )

    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            # 探身份这一步不许带凭据：只是问"你是哪台"，还没确认它是谁
            assert "authorization" not in {key.lower() for key in request.headers}
            return health("inst-1")
        posted.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "agent_id": "id-new", "token": "rma_new"})

    install_transport(monkeypatch, handler)

    resolved = await_ensure(make_config(tmp_path))

    assert posted == [], "同一台 A 换了地址，不该再发注册请求"
    assert resolved.token == "rma_old"
    assert resolved.agent_id == "id-old"
    assert resolved.name == "lab-01"
    assert resolved.instance_id == "inst-1"

    # 凭据已经挪到新地址；旧地址的槽删掉，同一个 token 不留两份
    assert load(make_config(tmp_path)).token == "rma_old"
    old = AgentConfig(server="http://old.example.com", state_dir=str(tmp_path / "state"))
    assert load(old) is None


def test_enrolls_when_instance_id_differs(tmp_path, monkeypatch):
    """真是另一台 A：instance_id 对不上，照常注册，旧凭据原样留着。"""
    save(
        AgentConfig(server="http://old.example.com", state_dir=str(tmp_path / "state")),
        Credentials(
            server="http://old.example.com",
            token="rma_old",
            agent_id="a1",
            instance_id="inst-old",
        ),
    )

    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return health("inst-new")
        posted.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "agent_id": "a2", "token": "rma_new"})

    install_transport(monkeypatch, handler)

    resolved = await_ensure(make_config(tmp_path))

    assert posted == ["/internal/enroll"]
    assert resolved.token == "rma_new"
    old = AgentConfig(server="http://old.example.com", state_dir=str(tmp_path / "state"))
    assert load(old).token == "rma_old"


def test_enrolls_when_server_has_no_instance_id(tmp_path, monkeypatch):
    """旧版 A 的 health 里没有 instance_id：认不出来就退回注册，不能因此卡死。"""
    save(
        AgentConfig(server="http://old.example.com", state_dir=str(tmp_path / "state")),
        Credentials(
            server="http://old.example.com",
            token="rma_old",
            agent_id="a1",
            instance_id="inst-old",
        ),
    )

    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return health(None)
        posted.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "agent_id": "a2", "token": "rma_new"})

    install_transport(monkeypatch, handler)

    resolved = await_ensure(make_config(tmp_path))

    assert posted == ["/internal/enroll"]
    assert resolved.token == "rma_new"


def test_multiple_known_servers_requires_explicit_server(tmp_path, monkeypatch):
    """记着两台 A 又没指定地址时，报错要把两台都列出来，而不是随便挑一台。"""
    state = str(tmp_path / "state")
    save(
        AgentConfig(server="http://a.example.com", state_dir=state),
        Credentials(server="http://a.example.com", token="rma_a", agent_id="a1"),
    )
    save(
        AgentConfig(server="http://b.example.com", state_dir=state),
        Credentials(server="http://b.example.com", token="rma_b", agent_id="b1"),
    )
    from app.cloud_agent.credentials import current_path

    current_path(AgentConfig(state_dir=state)).unlink()

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("没指定哪台 A 时不该发请求")

    install_transport(monkeypatch, handler)

    with pytest.raises(ConfigError) as excinfo:
        await_ensure(AgentConfig(root=str(tmp_path), state_dir=state))

    assert "a.example.com" in str(excinfo.value)
    assert "b.example.com" in str(excinfo.value)


def test_pending_then_approved(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    state = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return health("inst-1")
        if request.method == "POST":
            return httpx.Response(202, json={"pending_id": "p" * 8, "hint": "approve p"})
        state["get"] += 1
        if state["get"] == 1:
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json={"status": "ok", "agent_id": "a2", "token": "rma_after"})

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(enroll_module, "POLL_INTERVAL", 0.01)

    resolved = await_ensure(config)

    assert resolved.token == "rma_after"
    assert state["get"] == 2


def test_rejected_enrollment_is_reported(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"pending_id": "p" * 8})
        return httpx.Response(403, json={"code": "forbidden", "message": "管理员已拒绝"})

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(enroll_module, "POLL_INTERVAL", 0.01)

    with pytest.raises(EnrollError, match="拒绝"):
        await_ensure(config)


def test_server_error_is_reported(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "internal", "message": "boom"})

    install_transport(monkeypatch, handler)

    with pytest.raises(EnrollError, match="500"):
        await_ensure(config)


def test_missing_server_is_reported(tmp_path):
    config = AgentConfig(root=str(tmp_path), state_dir=str(tmp_path / "state"))

    with pytest.raises(ConfigError, match="server"):
        await_ensure(config)


def test_missing_root_is_reported(tmp_path, monkeypatch):
    config = AgentConfig(server="http://127.0.0.1:8000", state_dir=str(tmp_path / "state"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "rma_x"})

    install_transport(monkeypatch, handler)

    with pytest.raises(ConfigError, match="root"):
        await_ensure(config)


def test_rotate_updates_local_credentials(tmp_path, monkeypatch):
    config = make_config(tmp_path, token="rma_old", agent_id="a1", instance_id="inst-1")
    save(
        config,
        Credentials(server=config.server, token="rma_old", agent_id="a1", instance_id="inst-1"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "rma_rotated"})

    install_transport(monkeypatch, handler)

    outcome = await_rotate(config)

    assert outcome.ok is True
    assert outcome.token == "rma_rotated"
    stored = load(config)
    assert stored is not None and stored.token == "rma_rotated"
    # 换 token 不该把 A 的身份弄丢，否则下次它换地址就认不出来了
    assert stored.instance_id == "inst-1"


def test_rotate_after_enroll_keeps_instance_id(tmp_path, monkeypatch):
    """注册时拿到的 instance_id，不能被第一次轮换写丢。

    这是真踩到的：ensure_credentials 只把 token/agent_id 回填进 config，轮换时
    save() 拿着 config.instance_id=None 覆盖文件，这台 A 从此认不出来。
    """
    config = make_config(tmp_path)

    def enroll_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/health":
            return health("inst-1")
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "rma_enrolled"})

    install_transport(monkeypatch, enroll_handler)
    resolved = await_ensure(config)

    def rotate_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "rma_rotated"})

    install_transport(monkeypatch, rotate_handler)
    outcome = await_rotate(resolved)

    assert outcome.token == "rma_rotated"
    stored = load(config)
    assert stored is not None
    assert stored.token == "rma_rotated"
    assert stored.instance_id == "inst-1"


def test_rotate_reports_why_it_was_rejected(tmp_path, monkeypatch):
    """被拒也要带原因：三种原因的处置完全不同，压成一句"无效"就只能删文件。"""
    config = make_config(tmp_path, token="rma_old", agent_id="a1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "detail": {
                    "code": "revoked",
                    "message": "这台设备已被 A 吊销：请到「节点」页重新签发 token",
                }
            },
        )

    install_transport(monkeypatch, handler)

    outcome = await_rotate(config)

    assert outcome.ok is False
    assert outcome.code == "revoked"
    assert "重新签发" in (outcome.message or "")


def test_rotate_without_credentials_does_nothing(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("没有凭据时不该发请求")

    install_transport(monkeypatch, handler)

    outcome = await_rotate(config)
    assert outcome.ok is False
    assert outcome.code == "no_credentials"


def await_ensure(config: AgentConfig) -> AgentConfig:
    import asyncio

    return asyncio.run(ensure_credentials(config))


def await_rotate(config: AgentConfig):
    import asyncio

    return asyncio.run(rotate(config))
