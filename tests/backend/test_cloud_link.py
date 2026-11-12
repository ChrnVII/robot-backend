"""云端接入 API：页面契约与网关转发。

这些用例不触库：端点只做参数映射与封套，网关只做 UDS 转发，正好可以用假对象
把它们钉住。真正的跨进程联调在 `tests/cloud_agent/` 的 UDS 用例里。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api.endpoints import cloud_link
from app.gateways.cloud_agent import CloudAgentGateway
from app.schemas.cloud_link import CloudConfigUpdate, CloudCredentialRequest

pytestmark = pytest.mark.no_database


def run(coro):
    return asyncio.run(coro)


def fake_request():
    return SimpleNamespace(state=SimpleNamespace(trace_id="trace-1"))


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, op: str, **params):
        self.calls.append((op, params))
        if op == "status":
            return {"state": "online", "connected_at": 1.0, "uploads": {"active": 0}}
        if op == "logs":
            return {"items": [{"seq": 1, "message": "hi"}], "last_seq": 1}
        if op == "list_credentials":
            return {"slots": [{"server": "https://a", "agent_id": "a1"}]}
        return {"state": "online", "op": op}


def test_status_is_camelized_and_wrapped():
    gateway = FakeGateway()
    response = run(cloud_link.cloud_status(fake_request(), gateway))

    assert response["code"] == "OK"
    assert response["traceId"] == "trace-1"
    assert response["data"]["state"] == "online"
    assert response["data"]["connectedAt"] == 1.0, "页面读 camelCase，网关出口统一转换"
    assert gateway.calls == [("status", {})]


def test_config_update_only_sends_given_fields():
    gateway = FakeGateway()
    body = CloudConfigUpdate(root="/srv/share", maxConcurrentUploads=5, verifyTls=False)

    run(cloud_link.update_cloud_config(fake_request(), body, gateway))

    op, params = gateway.calls[-1]
    assert op == "set_config"
    assert params["config"] == {
        "root": "/srv/share",
        "max_concurrent_uploads": 5,
        "verify_tls": False,
    }


def test_empty_config_update_does_not_touch_agent():
    gateway = FakeGateway()
    run(cloud_link.update_cloud_config(fake_request(), CloudConfigUpdate(), gateway))

    assert gateway.calls[-1][0] == "get_config"


def test_credential_fields_are_mapped_to_snake_case():
    gateway = FakeGateway()
    body = CloudCredentialRequest(
        server="https://a.example.com", token="t", agentId="a1", root="/srv/share"
    )

    run(cloud_link.put_cloud_credential(fake_request(), body, gateway))

    op, params = gateway.calls[-1]
    assert op == "put_credential"
    assert params["agent_id"] == "a1"
    assert params["server"] == "https://a.example.com"


def test_delete_without_server_clears_everything():
    gateway = FakeGateway()

    run(cloud_link.delete_cloud_credential(fake_request(), None, gateway))
    assert gateway.calls[-1] == ("clear_credentials", {})

    run(cloud_link.delete_cloud_credential(fake_request(), "https://a.example.com", gateway))
    assert gateway.calls[-1] == ("forget_credential", {"server": "https://a.example.com"})


def test_logs_pass_paging_parameters():
    gateway = FakeGateway()
    run(cloud_link.cloud_logs(fake_request(), since=7, limit=50, gateway=gateway))

    assert gateway.calls[-1] == ("logs", {"since": 7, "limit": 50})


def test_gateway_forwards_op_and_drops_none():
    gateway = CloudAgentGateway(SimpleNamespace(cloud_agent_socket="x", cloud_agent_timeout=1.0))
    seen: list[dict] = []

    class FakeUnix:
        async def call(self, request):
            seen.append(dict(request))
            return {"ok": True}

    gateway._unix = FakeUnix()  # type: ignore[assignment]
    run(gateway.call("put_credential", server="https://a", token=None, agent_id="a1"))

    assert seen == [{"op": "put_credential", "server": "https://a", "agent_id": "a1"}]
