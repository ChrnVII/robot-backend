"""WS 推送中枢：主题扇出、事件唤醒、防抖、空闲零 IPC、失败容错。

这些用例不依赖数据库与真实机器人进程（provider 为进程内协程），
覆盖 hub.py 的推送语义。
"""

import asyncio

import pytest

from app.api.ws.hub import MAX_CONSECUTIVE_FAILURES, PushHub

pytestmark = pytest.mark.no_database


class FakeWebSocket:
    """最小 WebSocket 替身：记录已发送消息、记录关闭码、receive 永久阻塞。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: int | None = None
        self._never = asyncio.Event()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive(self) -> dict:
        await self._never.wait()
        return {"type": "websocket.disconnect"}

    async def close(self, code: int = 1000) -> None:
        self.closed = code


def _provider_counter():
    calls = {"n": 0}

    async def provider() -> dict:
        calls["n"] += 1
        return {"value": calls["n"]}

    return provider, calls


def test_same_topic_subscribers_share_one_provider_call():
    async def scenario() -> None:
        hub = PushHub()
        provider, calls = _provider_counter()
        topic = hub.topic("state", message_type="state", provider=provider, interval=5.0)
        first, second = FakeWebSocket(), FakeWebSocket()
        tasks = [asyncio.create_task(topic.serve(socket)) for socket in (first, second)]
        await asyncio.sleep(0.05)

        # 两个订阅者共享一次 provider（IPC）调用，双方都收到同一份数据。
        assert calls["n"] == 1
        assert len(first.sent) == 1 and len(second.sent) == 1
        assert first.sent[0]["type"] == "state"
        assert str(first.sent[0]["timestamp"]).endswith("Z")

        for task in tasks:
            task.cancel()
        await hub.shutdown()

    asyncio.run(scenario())


def test_slow_subscriber_does_not_delay_healthy_subscriber():
    class SlowWebSocket(FakeWebSocket):
        async def send_json(self, payload):
            await asyncio.Event().wait()

    async def scenario():
        hub = PushHub()
        provider, calls = _provider_counter()
        topic = hub.topic("state", message_type="state", provider=provider, interval=0.01)
        healthy, slow = FakeWebSocket(), SlowWebSocket()
        tasks = [asyncio.create_task(topic.serve(socket)) for socket in (healthy, slow)]
        try:
            await asyncio.sleep(0.15)
            assert len(healthy.sent) >= 3
            assert topic._queues[slow].qsize() == 1
            assert calls["n"] >= 3
        finally:
            for task in tasks:
                task.cancel()
            await hub.shutdown()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(scenario())


def test_event_wakes_topic_immediately():
    async def scenario() -> None:
        hub = PushHub()
        provider, calls = _provider_counter()
        # interval 设得很长：只有事件唤醒才可能触发第二次刷新。
        topic = hub.topic("state", message_type="state", provider=provider, interval=30.0)
        socket = FakeWebSocket()
        task = asyncio.create_task(topic.serve(socket))
        await asyncio.sleep(0.05)
        assert calls["n"] == 1

        hub.wake_payload({"topic": "state", "device_id": "lift"})
        await asyncio.sleep(0.5)
        assert calls["n"] == 2
        assert len(socket.sent) == 2

        task.cancel()
        await hub.shutdown()

    asyncio.run(scenario())


def test_environment_device_wakes_both_topics():
    async def scenario() -> None:
        hub = PushHub()
        provider, calls = _provider_counter()
        hub.topic("state", message_type="state", provider=provider, interval=30.0)
        hub.topic("environment", message_type="environment", provider=provider, interval=30.0)
        state_socket, env_socket = FakeWebSocket(), FakeWebSocket()
        tasks = [
            asyncio.create_task(hub.topic("state", message_type="state", provider=provider).serve(state_socket)),
            asyncio.create_task(hub.topic("environment", message_type="environment", provider=provider).serve(env_socket)),
        ]
        await asyncio.sleep(0.05)
        before = calls["n"]

        hub.wake_payload({"topic": "state", "device_id": "environment"})
        await asyncio.sleep(0.5)
        assert calls["n"] == before + 2

        for task in tasks:
            task.cancel()
        await hub.shutdown()

    asyncio.run(scenario())


def test_inspection_event_wakes_run_and_video_topics():
    async def scenario() -> None:
        hub = PushHub()
        provider, calls = _provider_counter()
        hub.topic("inspection:run-1", message_type="inspection", provider=provider, interval=30.0)
        hub.topic("video:run-1", message_type="video", provider=provider, interval=30.0)
        tasks = [
            asyncio.create_task(hub.topic("inspection:run-1", message_type="inspection", provider=provider).serve(FakeWebSocket())),
            asyncio.create_task(hub.topic("video:run-1", message_type="video", provider=provider).serve(FakeWebSocket())),
        ]
        await asyncio.sleep(0.05)
        before = calls["n"]

        hub.wake_payload({"topic": "inspection", "task_run_id": "run-1"})
        await asyncio.sleep(0.5)
        assert calls["n"] == before + 2

        # 不相关任务不应被唤醒。
        hub.wake_payload({"topic": "inspection", "task_run_id": "run-other"})
        await asyncio.sleep(0.4)
        assert calls["n"] == before + 2

        for task in tasks:
            task.cancel()
        await hub.shutdown()

    asyncio.run(scenario())


def test_event_burst_is_debounced():
    async def scenario() -> None:
        hub = PushHub()
        provider, calls = _provider_counter()
        topic = hub.topic("state", message_type="state", provider=provider, interval=30.0)
        task = asyncio.create_task(topic.serve(FakeWebSocket()))
        await asyncio.sleep(0.05)
        before = calls["n"]

        for _ in range(20):
            hub.wake_payload({"topic": "state", "device_id": "lift"})
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        # 防抖 + 最小刷新间隔：事件风暴收敛为少量刷新，而非 20 次。
        assert calls["n"] - before <= 3

        task.cancel()
        await hub.shutdown()

    asyncio.run(scenario())


def test_idle_topic_does_not_poll():
    async def scenario() -> None:
        hub = PushHub()
        provider, calls = _provider_counter()
        topic = hub.topic("state", message_type="state", provider=provider, interval=0.05)
        socket = FakeWebSocket()
        task = asyncio.create_task(topic.serve(socket))
        await asyncio.sleep(0.15)
        assert calls["n"] >= 2  # 有订阅者时按周期推送

        task.cancel()
        await asyncio.sleep(0.05)
        idle_calls = calls["n"]
        await asyncio.sleep(0.3)
        # 无订阅者后不再有任何 IPC 调用。
        assert calls["n"] == idle_calls

        await hub.shutdown()

    asyncio.run(scenario())


def test_transient_provider_failure_keeps_connection():
    async def scenario() -> None:
        hub = PushHub()
        calls = {"n": 0}

        async def flaky() -> dict:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("ipc temporarily unavailable")
            return {"recovered": True}

        topic = hub.topic("state", message_type="state", provider=flaky, interval=0.05)
        socket = FakeWebSocket()
        task = asyncio.create_task(topic.serve(socket))
        await asyncio.sleep(0.3)
        # 瞬时失败不断开客户端；恢复后继续推送。
        assert socket.closed is None
        assert socket.sent and socket.sent[-1]["data"] == {"recovered": True}

        task.cancel()
        await hub.shutdown()

    asyncio.run(scenario())


def test_persistent_provider_failure_closes_with_service_restarted():
    async def scenario() -> None:
        hub = PushHub()

        async def broken() -> dict:
            raise RuntimeError("ipc permanently unavailable")

        topic = hub.topic("state", message_type="state", provider=broken, interval=0.01)
        socket = FakeWebSocket()
        task = asyncio.create_task(topic.serve(socket))
        await asyncio.sleep(0.05 * MAX_CONSECUTIVE_FAILURES + 0.5)
        assert socket.closed == 1012

        task.cancel()
        await hub.shutdown()

    asyncio.run(scenario())
