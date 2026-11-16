"""媒体会话的收尾：什么时候该重连，什么时候该收敛。

这条链路上最容易写错、也最难在本地复现的是"会话收尾"：

- 本地预览先断（相机 RTSP 掉、backend 重启）时，如果两条数据泵用 `gather`，
  远端那条会把整个协程挂住——设备在线、上游连着、一帧都没有，页面永远停在
  "画面停滞"，而且机器人再也不会自己恢复。
- 云端已经结束会话（1000 / 4403 / 4404）时如果还重连，每关一个会话就在机器人
  上留一个永久重试的僵尸任务，日志里只剩刷屏的 4404。

所以这里用假 socket 精确验证三种收尾：本地先断 → 可重连；云端要求停 → 收敛；
云端已回收会话 → 收敛。
"""

from __future__ import annotations

import asyncio

import pytest

from app.cloud_agent.media import MediaError, MediaSession
from app.cloud_agent.protocol import Message, MessageType

pytestmark = pytest.mark.no_database


class Config:
    token = "agent-token"
    backend_url = "http://127.0.0.1:8100"
    backend_token = ""
    open_timeout = 5


class ScriptedSocket:
    """按脚本吐帧，吐完就结束（等价于对端正常关闭）。"""

    def __init__(self, items: list[str] | None = None, *, close_code: int | None = None) -> None:
        self._items = list(items or [])
        self.close_code = close_code
        self.closed = False

    def __aiter__(self) -> ScriptedSocket:
        return self

    async def __anext__(self) -> str:
        if self._items:
            return self._items.pop(0)
        raise StopAsyncIteration

    async def close(self, *args, **kwargs) -> None:
        self.closed = True


class HangingSocket(ScriptedSocket):
    """永不出帧、永不结束：模拟"连接还在，但没有数据"。"""

    async def __anext__(self) -> str:
        await asyncio.Event().wait()      # 只有被 cancel 才会醒
        raise StopAsyncIteration


def make_session(cloud, local) -> MediaSession:
    session = MediaSession(Config(), "med-1", "", "low")  # type: ignore[arg-type]
    session._open_cloud = _return(cloud)   # type: ignore[assignment]
    session._open_local = _return(local)   # type: ignore[assignment]
    return session


def _return(value):
    async def opener(*args, **kwargs):
        return value

    return opener


async def test_local_leg_ending_is_retryable_not_terminal():
    """本地预览断了但云端会话还在：必须重连，不能把会话判死。"""
    cloud = HangingSocket()
    session = make_session(cloud, ScriptedSocket())

    with pytest.raises(MediaError) as raised:
        await session._connect_once()

    assert raised.value.retry is True
    assert raised.value.code == "backend_unreachable"
    assert session._stop.is_set() is False       # 还可以重连
    assert cloud.closed is True                  # 但这条上游已经收干净了


async def test_cloud_stop_request_converges_the_session():
    """云端发 `media_control stop`：置终态，不再重连。"""
    stop = Message(
        type=MessageType.MEDIA_CONTROL, payload={"action": "stop", "session_id": "med-1"}
    ).to_json()
    session = make_session(ScriptedSocket([stop]), HangingSocket())

    await session._connect_once()

    assert session._terminal is True
    assert session._stop.is_set() is True


@pytest.mark.parametrize("code", [4403, 4404])
async def test_cloud_reclaiming_the_session_converges_it(code):
    """4403/4404 说明这条会话在云端已经不存在了：重连只会得到同样的关闭码。"""
    session = make_session(ScriptedSocket(close_code=code), HangingSocket())

    await session._connect_once()

    assert session._stop.is_set() is True


async def test_transport_failure_does_not_converge():
    """1006 这类"链路断了"必须继续重连——拔网线后要能自己恢复出图。"""
    session = make_session(ScriptedSocket(close_code=1006), HangingSocket())

    with pytest.raises(MediaError) as raised:
        await session._connect_once()

    assert raised.value.retry is True
    assert session._stop.is_set() is False


async def test_local_backend_down_before_first_frame_fails_fast():
    """会话还没起步就起不来：立刻报错给云端，而不是让 start() 干等 6s 超时。

    云端据此给页面一个具体原因（backend_unreachable），并把它当可重连故障，
    而不是"这台设备不支持画面"。
    """
    session = MediaSession(Config(), "med-1", "", "low")  # type: ignore[arg-type]
    session._open_cloud = _return(ScriptedSocket())       # type: ignore[assignment]

    async def refusing(*args, **kwargs):
        raise OSError("connection refused")

    session._open_local = refusing                        # type: ignore[assignment]

    with pytest.raises(MediaError) as raised:
        await session._connect_once()

    assert raised.value.code == "backend_unreachable"
    assert raised.value.retry is True
