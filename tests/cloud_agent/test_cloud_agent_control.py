"""控制面与状态机测试。

三组关注点：

1. `AgentControl` 的 op 路由与参数校验——页面传错东西时必须得到稳定错误码，
   而不是 500；
2. `ControlServer` 的 UDS 往返——backend 就是按这个形状说话的；
3. `CloudAgent` 的凭据/配置操作——这些是"原来只能 SSH 做的事"，现在由页面触发，
   写错一个字段就会让云端多出一台重名节点。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from app.cloud_agent.agent import CloudAgent
from app.cloud_agent.config import AgentConfig, load_config, save_config
from app.cloud_agent.control import AgentControl, ControlError, ControlServer
from app.cloud_agent.credentials import Credentials, load, save
from app.cloud_agent.state import LOG_BUFFER

pytestmark = pytest.mark.no_database


def make_config(tmp_path) -> AgentConfig:
    return AgentConfig(
        server="http://127.0.0.1:8000",
        root=str(tmp_path / "share"),
        state_dir=str(tmp_path / "state"),
        config_path=str(tmp_path / "cloud-agent.toml"),
        control_socket=str(tmp_path / "run" / "cloud-agent.sock"),
    )


@pytest.fixture()
def share(tmp_path):
    directory = tmp_path / "share"
    directory.mkdir()
    return directory


# --- 配置落盘 -----------------------------------------------------------


def test_save_config_roundtrip_excludes_token(tmp_path, share):
    config = make_config(tmp_path)
    config.token = "rma_secret_token"
    config.agent_id = "a1"
    config.name = "lab-01"

    path = save_config(config)
    text = path.read_text(encoding="utf-8")

    assert "rma_secret_token" not in text
    assert "agent_id" not in text
    assert 'name = "lab-01"' in text

    reloaded = load_config(path)
    assert reloaded.server == config.server
    assert reloaded.root == config.root
    assert reloaded.name == "lab-01"
    assert reloaded.token == "", "token 只存在于凭据槽，不能从 TOML 读出来"


def test_saved_config_is_owner_only(tmp_path, share):
    if os.name != "posix":
        pytest.skip("POSIX 权限位")
    config = make_config(tmp_path)
    path = save_config(config)
    assert path.stat().st_mode & 0o777 == 0o600


# --- 控制面 op ----------------------------------------------------------


class StubAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def status(self):
        return {"state": "online"}

    def config_view(self):
        return {"server": "https://x"}

    def list_credentials(self):
        return {"slots": []}

    async def set_config(self, payload):
        self.calls.append(("set_config", payload))
        return {"state": "online"}

    async def reload_config(self):
        return {"state": "online"}

    async def put_credential(self, **kwargs):
        self.calls.append(("put_credential", kwargs))
        return {"state": "online"}

    async def activate_credential(self, server):
        self.calls.append(("activate_credential", {"server": server}))
        return {"state": "online"}

    async def forget_credential(self, server):
        self.calls.append(("forget_credential", {"server": server}))
        return {"state": "unregistered"}

    async def clear_credentials(self):
        self.calls.append(("clear_credentials", {}))
        return {"state": "unregistered"}

    async def start_enroll(self, **kwargs):
        self.calls.append(("enroll", kwargs))
        return {"state": "enrolling"}

    async def cancel_enroll(self):
        return {"state": "unregistered"}

    async def reconnect(self):
        return {"state": "connecting"}

    async def pause(self):
        return {"state": "paused"}

    async def resume(self):
        return {"state": "offline"}

    async def rotate_token(self):
        return {"ok": True}

    async def cancel_upload(self, upload_id):
        self.calls.append(("cancel_upload", {"upload_id": upload_id}))
        return {"state": "online"}

    async def restart_process(self):
        return {"restarting": True}


async def test_control_routes_every_op():
    stub = StubAgent()
    control = AgentControl(stub)

    assert (await control.handle({"op": "status"}))["state"] == "online"
    assert (await control.handle({"op": "pause"}))["state"] == "paused"
    assert (await control.handle({"op": "get_config"}))["server"] == "https://x"

    await control.handle({"op": "set_config", "config": {"root": "/srv/share"}})
    assert stub.calls[-1] == ("set_config", {"root": "/srv/share"})

    await control.handle(
        {"op": "put_credential", "server": "https://a", "token": "t", "agent_id": "a1"}
    )
    assert stub.calls[-1][1]["agent_id"] == "a1"

    await control.handle({"op": "cancel_upload", "upload_id": "u1"})
    assert stub.calls[-1] == ("cancel_upload", {"upload_id": "u1"})

    await control.handle({"op": "activate_credential", "server": "https://b"})
    assert stub.calls[-1] == ("activate_credential", {"server": "https://b"})


async def test_control_rejects_unknown_op_and_bad_arguments():
    control = AgentControl(StubAgent())

    with pytest.raises(ControlError) as unknown:
        await control.handle({"op": "drop_database"})
    assert unknown.value.code == "unknown_op"

    with pytest.raises(ControlError) as missing:
        await control.handle({"op": "put_credential", "server": "https://a"})
    assert missing.value.code == "invalid_request"

    with pytest.raises(ControlError) as empty:
        await control.handle({})
    assert empty.value.code == "invalid_request"


async def test_logs_op_returns_incremental_records():
    LOG_BUFFER.clear()
    LOG_BUFFER.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None))

    control = AgentControl(StubAgent())
    first = await control.handle({"op": "logs", "since": 0})
    assert first["items"][-1]["message"] == "hello world"

    seq = first["last_seq"]
    assert (await control.handle({"op": "logs", "since": seq}))["items"] == []


# --- UDS 往返 -----------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="Windows 上的 UDS 支持不完整，生产目标是 Linux")
async def test_uds_roundtrip(tmp_path):
    control = AgentControl(StubAgent())
    server = ControlServer(tmp_path / "run" / "control.sock", control)
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        writer.write(b'{"op": "status"}\n')
        await writer.drain()
        response = json.loads(await reader.readline())

        assert response["ok"] is True
        assert response["result"]["state"] == "online"

        writer.write(b'{"op": "nope"}\n')
        await writer.drain()
        failed = json.loads(await reader.readline())
        assert failed["ok"] is False
        assert failed["error"] == "unknown_op"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()


# --- supervisor 的凭据/配置操作 ----------------------------------------


async def test_prepare_without_server_waits_as_unregistered(tmp_path):
    agent = CloudAgent(make_config(tmp_path))
    agent.config.server = ""

    assert await agent._prepare() is False
    assert agent.state.state == "unregistered"


async def test_set_config_persists_and_clears_token_when_server_changes(tmp_path, share):
    config = make_config(tmp_path)
    config.token = "old-token"
    config.agent_id = "old-id"
    save(config, Credentials(server=config.server, token="old-token", agent_id="old-id"))

    agent = CloudAgent(config)
    await agent.set_config({"name": "lab-02"})

    assert agent.config.name == "lab-02"
    assert load_config(config.config_path).name == "lab-02"

    await agent.set_config({"server": "https://new.example.com"})

    assert agent.config.server == "https://new.example.com"
    assert agent.config.token == "", "换云端必须清掉旧 token"
    assert agent.config.agent_id == ""


async def test_put_credential_writes_slot_and_config(tmp_path, share):
    agent = CloudAgent(make_config(tmp_path))

    await agent.put_credential(
        server="https://a.example.com", token="rma_x", agent_id="a1", root=str(share)
    )

    stored = load(
        AgentConfig(server="https://a.example.com", state_dir=str(tmp_path / "state"))
    )
    assert stored is not None
    assert stored.token == "rma_x"
    assert stored.agent_id == "a1"

    slots = agent.list_credentials()["slots"]
    assert len(slots) == 1
    assert slots[0]["active"] is True
    assert "token" not in slots[0]


async def test_set_config_cancels_pending_enroll(tmp_path, share):
    """换云端时，还挂在旧云端上的待批准注册必须先停。

    否则它几秒后拿到旧云端的 token 落盘，新配置就带了一把错的 token。
    """
    agent = CloudAgent(make_config(tmp_path))
    task = asyncio.create_task(asyncio.sleep(60))
    agent._enroll_task = task
    agent.state.pending = {"id": "p" * 8}

    await agent.set_config({"server": "https://new.example.com"})

    assert agent._enroll_task is None
    assert agent.state.pending is None
    assert task.cancelled()


async def test_put_credential_cancels_pending_enroll(tmp_path, share):
    agent = CloudAgent(make_config(tmp_path))
    task = asyncio.create_task(asyncio.sleep(60))
    agent._enroll_task = task
    agent.state.pending = {"id": "p" * 8}

    await agent.put_credential(
        server="https://a.example.com", token="rma_x", agent_id="a1", root=str(share)
    )

    assert agent._enroll_task is None
    assert agent.state.pending is None
    assert task.cancelled()


async def test_activate_credential_switches_identity(tmp_path, share):
    agent = CloudAgent(make_config(tmp_path))
    save(agent.config, Credentials(server="https://a", token="ta", agent_id="a1"))
    save(agent.config, Credentials(server="https://b", token="tb", agent_id="b1"))
    agent.config.server = "https://a"
    agent.config.token = "ta"
    agent.config.agent_id = "a1"

    await agent.activate_credential("https://b")

    assert agent.config.server == "https://b"
    assert agent.config.token == "tb"
    assert agent.config.agent_id == "b1"
    assert load_config(agent.config.config_path).server == "https://b"


async def test_activate_unknown_credential_is_rejected(tmp_path, share):
    from app.cloud_agent.config import ConfigError

    agent = CloudAgent(make_config(tmp_path))
    with pytest.raises(ConfigError, match="没有这个云端"):
        await agent.activate_credential("https://nope")


async def test_forget_credential_only_affects_the_named_server(tmp_path, share):
    agent = CloudAgent(make_config(tmp_path))
    save(agent.config, Credentials(server="https://a", token="ta", agent_id="a1"))
    save(agent.config, Credentials(server="https://b", token="tb", agent_id="b1"))

    await agent.forget_credential("https://b")

    assert load(AgentConfig(server="https://b", state_dir=str(tmp_path / "state"))) is None
    assert load(AgentConfig(server="https://a", state_dir=str(tmp_path / "state"))) is not None


async def test_clear_credentials_removes_every_slot(tmp_path, share):
    agent = CloudAgent(make_config(tmp_path))
    save(agent.config, Credentials(server="https://a", token="ta", agent_id="a1"))
    save(agent.config, Credentials(server="https://b", token="tb", agent_id="b1"))

    result = await agent.clear_credentials()

    assert result["removed"] == 2
    assert agent.list_credentials()["slots"] == []
    assert agent.config.token == ""


async def test_pause_and_resume_toggle_state(tmp_path):
    agent = CloudAgent(make_config(tmp_path))

    await agent.pause()
    assert agent.state.state == "paused"
    assert agent.state.paused is True

    await agent.resume()
    assert agent.state.state == "offline"
    assert agent.state.paused is False


async def test_cancel_upload_reports_missing_manager(tmp_path):
    from app.cloud_agent.config import ConfigError

    agent = CloudAgent(make_config(tmp_path))
    with pytest.raises(ConfigError):
        await agent.cancel_upload("u1")


def test_repo_layout_matches_expected_paths():
    """防止测试自己在错误目录下跑：contract 与源码必须同时存在。"""
    root = Path(__file__).resolve().parents[2]
    assert (root / "tests" / "contracts" / "protocol-frames.json").is_file()
    assert (root / "app" / "cloud_agent" / "protocol.py").is_file()
