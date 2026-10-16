"""实时推送中枢：主题订阅 + 事件唤醒 + 周期兜底。

v2 改造（对齐旧 @Scheduled(fixedDelay=500) 的兼容升级）：
- 同一主题的全部 WS 客户端共享一个广播循环：provider（IPC 调用）每个
  周期只执行一次后扇出，替代「每客户端各自轮询」——客户端数不再放大
  IPC 连接开销（此前 /ws/inspections 每客户端每 tick 新建 4 条 UDS 连接）；
- 事件驱动：robot_core 经 PostgreSQL NOTIFY 到达的变更事件会唤醒对应主题
  立即刷新（含防抖与最小刷新间隔），500ms 周期仅作兜底；
- 无订阅者时不执行 provider，空闲即零 IPC；
- provider 瞬时失败不再立即断开客户端（旧行为直接以 1012 关闭），
  连续失败超过阈值才关闭。

消息格式与旧契约一致：{"type": ..., "data": ..., "timestamp": ...}。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.common.clock import utc_now_iso

logger = logging.getLogger(__name__)

#: 周期兜底推送间隔（保持旧前端 500ms 契约）。
PUSH_INTERVAL_SECONDS = 0.5
#: 事件到达后的防抖静默期：合并同一批写入产生的事件。
EVENT_DEBOUNCE_SECONDS = 0.15
#: 同一主题两次刷新的最小间隔，防止逐帧事件（如 15fps 推理落库）过密推送。
MIN_REFRESH_INTERVAL_SECONDS = 0.25
#: provider 连续失败次数上限，超过则以 1012 关闭订阅者（对齐旧「服务重启」语义）。
MAX_CONSECUTIVE_FAILURES = 10
SEND_TIMEOUT_SECONDS = 2.0

Provider = Callable[[], Awaitable[dict]]

_SERVICE_RESTARTED = 1012


class Topic:
    """一个推送主题：多客户端订阅、单广播循环、事件唤醒。"""

    def __init__(self, key: str, message_type: str, provider: Provider,
                 interval: float = PUSH_INTERVAL_SECONDS) -> None:
        self.key = key
        self.message_type = message_type
        self._provider = provider
        self._interval = interval
        self._subscribers: set[WebSocket] = set()
        self._queues: dict[WebSocket, asyncio.Queue] = {}
        self._senders: dict[WebSocket, asyncio.Task] = {}
        # 唤醒事件在广播任务内创建：asyncio.Event 会绑定首次使用的事件循环，
        # 若在构造时创建、后被另一循环复用（测试或多循环宿主）会抛 RuntimeError。
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._failures = 0
        self._last_refresh = 0.0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def serve(self, websocket: WebSocket) -> None:
        """订阅并保持连接，直到客户端断开（路由层负责已 accept）。"""
        self._subscribers.add(websocket)
        self._queues[websocket] = asyncio.Queue(maxsize=1)
        self._senders[websocket] = asyncio.create_task(self._send_subscriber(websocket))
        self._ensure_task()
        try:
            # 只消费连接状态：推送由广播任务负责。Starlette 的 receive() 在断开时
            # 返回 websocket.disconnect 消息（再次调用才抛 RuntimeError），
            # 因此两种结束形态都要处理。
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            self._subscribers.discard(websocket)
            self._queues.pop(websocket, None)
            sender = self._senders.pop(websocket, None)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    def wake(self) -> None:
        """事件到达：要求广播循环尽快刷新（无订阅者时为空操作）。"""
        event = self._wake
        if not self._subscribers or event is None:
            return
        try:
            event.set()
        except RuntimeError:  # noqa: BLE001 - 事件绑定到其他循环：下一轮任务会重建
            self._wake = None

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._broadcast_loop())

    async def _broadcast_loop(self) -> None:
        # 绑定当前事件循环（每次任务启动都重建，天然兼容多循环宿主）。
        self._wake = asyncio.Event()
        try:
            while self._subscribers:
                try:
                    data = await self._provider()
                    self._failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 瞬时失败：跳过本轮，避免断开客户端
                    self._failures += 1
                    logger.debug("topic %s provider failed (%d/%d)",
                                 self.key, self._failures, MAX_CONSECUTIVE_FAILURES, exc_info=True)
                    if self._failures >= MAX_CONSECUTIVE_FAILURES:
                        await self.close_all(_SERVICE_RESTARTED)
                        break
                    await self._wait_next_tick()
                    continue
                await self._send_all(data)
                self._last_refresh = time.monotonic()
                await self._wait_next_tick()
        finally:
            self._task = None

    async def _wait_next_tick(self) -> None:
        """等待下一个刷新点：事件唤醒（防抖 + 最小间隔）或周期超时。"""
        event = self._wake
        if event is None:
            event = self._wake = asyncio.Event()
        try:
            await asyncio.wait_for(event.wait(), timeout=self._interval)
        except asyncio.TimeoutError:
            return
        event.clear()
        await asyncio.sleep(EVENT_DEBOUNCE_SECONDS)
        event.clear()
        elapsed = time.monotonic() - self._last_refresh
        if elapsed < MIN_REFRESH_INTERVAL_SECONDS:
            await asyncio.sleep(MIN_REFRESH_INTERVAL_SECONDS - elapsed)

    async def _send_all(self, data: dict) -> None:
        message = {"type": self.message_type, "data": data, "timestamp": utc_now_iso()}
        for websocket in list(self._subscribers):
            queue = self._queues[websocket]
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    async def _send_subscriber(self, websocket: WebSocket) -> None:
        queue = self._queues[websocket]
        try:
            while True:
                message = await queue.get()
                await asyncio.wait_for(websocket.send_json(message), SEND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._subscribers.discard(websocket)
            try:
                await asyncio.wait_for(close_quietly(websocket, _SERVICE_RESTARTED), SEND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def close_all(self, code: int) -> None:
        """关闭全部订阅者并停止推送（服务重启/中枢关闭时使用）。"""
        for websocket in list(self._subscribers):
            self._subscribers.discard(websocket)
            sender = self._senders.get(websocket)
            if sender is not None:
                sender.cancel()
            try:
                await asyncio.wait_for(close_quietly(websocket, code), SEND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """取消广播任务（配合 close_all 在应用关闭时调用）。"""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for sender in self._senders.values():
            sender.cancel()
        await asyncio.gather(*self._senders.values(), return_exceptions=True)
        self._task = None


class PushHub:
    """主题注册表：路由按主题订阅，PG 事件按主题唤醒。"""

    def __init__(self) -> None:
        self._topics: dict[str, Topic] = {}

    def topic(self, key: str, *, message_type: str, provider: Provider,
              interval: float = PUSH_INTERVAL_SECONDS) -> Topic:
        existing = self._topics.get(key)
        if existing is not None and existing.message_type == message_type:
            return existing
        topic = Topic(key, message_type, provider, interval)
        self._topics[key] = topic
        return topic

    def wake(self, topic_key: str) -> None:
        topic = self._topics.get(topic_key)
        if topic is not None:
            topic.wake()

    def wake_payload(self, payload: Mapping[str, Any]) -> None:
        """按 robot_core 事件载荷唤醒相关主题（由 PG 监听线程经事件循环投递）。"""
        topic_type = str(payload.get("topic") or "")
        if topic_type == "state":
            self.wake("state")
            if payload.get("device_id") == "environment":
                self.wake("environment")
        elif topic_type == "environment":
            self.wake("environment")
        elif topic_type == "inspection":
            task_run_id = payload.get("task_run_id")
            if task_run_id:
                # 巡检与视频通道读取同一份任务数据，一并唤醒。
                self.wake(f"inspection:{task_run_id}")
                prefix = f"video:{task_run_id}"
                for key in list(self._topics):
                    if key == prefix or key.startswith(prefix + ":"):
                        self.wake(key)

    def stats(self) -> dict[str, int]:
        return {key: topic.subscriber_count for key, topic in self._topics.items() if topic.subscriber_count}

    async def shutdown(self) -> None:
        for topic in list(self._topics.values()):
            await topic.close_all(1001)
            await topic.stop()
        self._topics.clear()


_hub: PushHub | None = None


def get_push_hub() -> PushHub:
    global _hub
    if _hub is None:
        _hub = PushHub()
    return _hub


def reset_push_hub() -> None:
    """重建中枢（测试用：避免跨用例持有旧事件循环上的订阅）。"""
    global _hub
    _hub = None


async def close_quietly(websocket: WebSocket, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass
