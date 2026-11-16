"""控制通道发送侧：状态槽与优先级。

P0 的验收条件之一是"状态队列满时丢状态、命令不受影响"。这条性质没法靠读代码
确认——它取决于两个生产点（`outbox` 与状态槽）和唯一写者的调度顺序，所以用
假 socket + 状态洪峰直接压。

另一条被钉住的性质是"状态永远不排队"：槽里只有最新一帧，投递是 O(1)、不会
失败、不会阻塞调用方。状态生产者（P3 的分组上报）会每秒调它好几次，
一旦这里会阻塞，整条链路就会被它拖住。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.cloud_agent.client import AgentClient
from app.cloud_agent.config import AgentConfig
from app.cloud_agent.protocol import Message
from app.cloud_agent.sandbox import Sandbox

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
def client(tmp_path) -> AgentClient:
    share = tmp_path / "share"
    share.mkdir()
    return AgentClient(make_config(tmp_path), Sandbox(share))


class FakeSocket:
    """只记帧、可选变慢，用来把"发送"这一步从网络里独立出来。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.frames: list[str] = []
        self.delay = delay

    async def send(self, frame: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.frames.append(frame)

    def types(self) -> list[str]:
        return [json.loads(frame)["type"] for frame in self.frames]


def state_payload(version: int) -> dict:
    return {
        "state_version": version,
        "observed_at": time.time(),
        "robot": {"mode": "idle", "battery": 76},
    }


# --- 状态槽 -------------------------------------------------------------


def test_state_slot_keeps_only_the_latest_frame(client: AgentClient):
    client.publish_state(state_payload(1))
    client.publish_state(state_payload(2))

    assert client.state_dropped == 1

    frame = client._drain()
    assert frame is not None
    body = json.loads(frame)
    assert body["type"] == "state"
    assert body["payload"]["state_version"] == 2

    # 取走即清空：状态不会被重复发送
    assert client._drain() is None


def test_publish_state_never_touches_the_command_outbox(client: AgentClient):
    for version in range(100):
        client.publish_state(state_payload(version))

    assert client.outbox.empty()
    assert client.outbox.qsize() == 0


# --- 优先级 -------------------------------------------------------------


def test_commands_are_drained_before_state(client: AgentClient):
    """两个队列都有内容时，必须先发命令回包。"""
    client._enqueue_nowait(Message.result_frame("r-1", {"ok": True}).to_json())
    client.publish_state(state_payload(1))

    first = json.loads(client._drain())
    second = json.loads(client._drain())

    assert first["type"] == "result"
    assert first["id"] == "r-1"
    assert second["type"] == "state"


async def test_state_flood_never_starves_command_results(client: AgentClient):
    """状态洪峰下，20 条命令结果必须一条不少、顺序不变地发出。"""
    socket = FakeSocket(delay=0.001)
    sender = asyncio.create_task(client._send_loop(socket))

    flowing = asyncio.Event()
    flowing.set()

    async def flood() -> None:
        version = 0
        while flowing.is_set():
            version += 1
            client.publish_state(state_payload(version))
            await asyncio.sleep(0)          # 每帧都让出，模拟最坏情况

    flooder = asyncio.create_task(flood())
    try:
        for index in range(20):
            await client._enqueue(Message.result_frame(f"r-{index}", {}).to_json())

        for _ in range(200):                # 最多等 2s
            if len([t for t in socket.types() if t == "result"]) == 20:
                break
            await asyncio.sleep(0.01)
    finally:
        flowing.clear()
        flooder.cancel()
        sender.cancel()
        await asyncio.gather(flooder, sender, return_exceptions=True)

    results = [json.loads(f)["id"] for f in socket.frames if json.loads(f)["type"] == "result"]
    assert results == [f"r-{index}" for index in range(20)]

    # 期间产生的状态远多于发出的状态：其余都被覆盖掉了，这才符合契约。
    assert client.state_dropped > 0


async def test_hello_declares_capabilities_explicitly(client: AgentClient):
    """能力清单必须与真实实现一致，且总是显式发出（空列表也是一种回答）。"""
    from app.cloud_agent.client import CAPABILITIES, MEDIA_PROFILES

    body = json.loads(client._hello().to_json())
    payload = body["payload"]

    assert payload["capabilities"] == list(CAPABILITIES)
    assert payload["media_profiles"] == list(MEDIA_PROFILES)
    # 每一项都必须有对应的生产者/消费者，否则页面会等一个永远不来的东西。
    assert {"media.ws.v1", "camera.ptz", "camera.snapshot"} <= set(CAPABILITIES)
    assert {"state.v1", "events.batch.v1"} <= set(CAPABILITIES)


# --- 向前兼容（P0 验收项）-----------------------------------------------


async def test_unimplemented_camera_command_still_gets_unknown_command(
    client: AgentClient,
):
    """`camera.state` 在协议词汇表里，但这个构建还不会做——必须回 unknown_command。

    这是"解析宽松、执行严格"的关键一条：如果枚举里有命令、白名单里没有就
    抛异常或 KeyError，整条连接会被关掉，云端看到的是"设备反复掉线"，
    而不是"这台设备查不了云台位置"。两种症状的排查方向完全不同。
    """
    frame = Message.parse(
        json.dumps(
            {
                "v": 1,
                "type": "command",
                "id": "c9f1a2b3d4e5f607",
                "command": "camera.state",
                "payload": {"session_id": "ctl-1"},
            }
        )
    )

    await client._handle_command(frame)

    reply = json.loads(client.outbox.get_nowait())
    assert reply["type"] == "result"
    assert reply["id"] == "c9f1a2b3d4e5f607"
    assert reply["ok"] is False
    assert reply["error"]["code"] == "unknown_command"


async def test_camera_command_fails_cleanly_when_local_backend_is_down(
    client: AgentClient,
):
    """本机 backend 没起来时，云台命令要回"相机不可用"，而不是 500 或挂住。"""
    frame = Message.parse(
        json.dumps(
            {
                "v": 1,
                "type": "command",
                "id": "aa11bb22cc33dd44",
                "command": "camera.ptz",
                "payload": {
                    "command_id": "aa11bb22cc33dd44",
                    "session_id": "ctl-1",
                    "sequence": 1,
                    "action": "start",
                    "direct": "left",
                },
            }
        )
    )

    await client._handle_command(frame)

    reply = json.loads(client.outbox.get_nowait())
    assert reply["ok"] is False
    assert reply["error"]["code"] in {"camera_unavailable", "internal"}
