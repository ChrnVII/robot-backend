"""低延迟管线原语：最新帧槽位、订阅者队列、跨线程广播。

三个组件的职责边界：

- :class:`FrameSlot` —— 解码线程与推理线程之间的解耦点，只保留**最新一帧**；
- :class:`ViewerClient` —— 单个浏览器的定长队列，溢出即丢并标记需要重同步；
- :class:`CameraBroadcaster` —— 订阅者集合，从拉流线程安全地广播到事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from app.video.codec import FLAG_KEY, FLAG_RST

logger = logging.getLogger(__name__)


class FrameSlot:
    """只保留**最新一帧**。写者永不阻塞，读者拿走即清空。

    这是「推理慢不会拖垮视频」的关键：推理线程偶尔卡一下，中间帧直接丢弃，
    下一轮拿到的还是当前最新帧，永远不会出现越来越长的积压。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._frame: Any = None
        self._pts_ms = 0
        self._src_pts_ms: int | None = None
        self._fresh = False
        self.dropped = 0

    def put(self, frame: Any, pts_ms: int, src_pts_ms: int | None = None) -> None:
        with self._cv:
            if self._fresh:
                self.dropped += 1        # 上一帧还没被取走就被顶掉了
            self._frame = frame
            self._pts_ms = pts_ms
            self._src_pts_ms = src_pts_ms
            self._fresh = True
            self._cv.notify()

    def take(self, timeout: float = 0.5) -> tuple[Any, int, int | None] | None:
        """返回 ``(frame, pts_ms, src_pts_ms)``，超时返回 None。"""
        with self._cv:
            if not self._fresh:
                self._cv.wait(timeout)
            if not self._fresh:
                return None
            item = (self._frame, self._pts_ms, self._src_pts_ms)
            self._frame = None
            self._fresh = False
            return item


class ViewerClient:
    """一个 WebSocket 订阅者。队列定长，溢出即丢，绝不允许无界积压。"""

    __slots__ = ("dropped", "need_reset", "queue")

    def __init__(self, max_queue: int = 8) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(max_queue)))
        self.need_reset = False
        self.dropped = 0

    def offer(self, item: Any) -> None:
        """只允许在事件循环线程里调用。"""
        if isinstance(item, str):        # 配置/检测等 JSON，体量小，尽量送达
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:
                pass
            return

        if self.queue.full():
            self.dropped += 1
            self.need_reset = True
            while True:                  # 整个清空，只保留「最新的」
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        if self.need_reset:
            if not (item[0] & FLAG_KEY):
                return                   # 丢过帧之后只等下一个关键帧重新起步
            item = bytes([item[0] | FLAG_RST]) + item[1:]
            self.need_reset = False

        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


class CameraBroadcaster:
    """浏览器订阅者集合 + 广播。

    拉流线程调用 :meth:`broadcast`（线程安全，经 ``call_soon_threadsafe`` 投递），
    事件循环线程负责增删订阅者与消费队列。
    """

    def __init__(self, *, max_queue: int = 8) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[ViewerClient] = set()
        self._codec: str | None = None
        self._size: tuple[int, int] = (0, 0)
        self._config_message: str | None = None
        self._t0_epoch_ms = 0
        self.max_queue = max(1, int(max_queue))

    # ---------- 生命周期 ---------- #

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def detach_loop(self) -> None:
        self._loop = None

    # ---------- 订阅者 ---------- #

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def viewers(self) -> tuple[ViewerClient, ...]:
        return tuple(self._clients)

    @property
    def config_message(self) -> str | None:
        return self._config_message

    @property
    def codec(self) -> str | None:
        return self._codec

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def add_client(self) -> ViewerClient:
        client = ViewerClient(self.max_queue)
        client.need_reset = True
        self._clients.add(client)
        return client

    def remove_client(self, client: ViewerClient) -> None:
        self._clients.discard(client)

    def close_clients(self, code: int = 1001) -> None:
        """要求全部订阅者断开（服务停止时调用，由路由侧收尾）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._drop_all, code)
        except RuntimeError:             # 事件循环已关闭
            pass

    def _drop_all(self, code: int) -> None:  # pragma: no cover - 需真实 WS 连接
        for client in list(self._clients):
            try:
                client.queue.put_nowait(json.dumps({"t": "stop", "code": code}))
            except asyncio.QueueFull:
                while not client.queue.empty():
                    client.queue.get_nowait()
                client.queue.put_nowait(json.dumps({"t": "stop", "code": code}))

    # ---------- 广播 ---------- #

    def broadcast(self, item: Any) -> None:
        """线程安全广播；无订阅者或循环不可用时为空操作。"""
        if not self._clients:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, item)
        except RuntimeError:             # 事件循环已关闭
            pass

    def _fanout(self, item: Any) -> None:
        for client in list(self._clients):
            client.offer(item)

    def publish(self, payload: dict[str, Any]) -> None:
        """广播一条 JSON 消息（检测结果等）。"""
        self.broadcast(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    # ---------- 编码信息 ---------- #

    def build_config_message(self) -> str:
        return json.dumps(
            {
                "t": "config",
                "codec": self._codec,
                "t0": self._t0_epoch_ms,
                "size": list(self._size),
            },
            separators=(",", ":"),
        )

    def set_size(self, size: tuple[int, int]) -> None:
        if not (size and size[0] and size[1]):
            return
        self._size = (int(size[0]), int(size[1]))
        if self._codec:
            self._config_message = self.build_config_message()

    def set_codec(self, codec: str | None) -> None:
        if not codec or codec == self._codec:
            return
        self._codec = codec
        self._config_message = self.build_config_message()
        logger.info("摄像头预览编码：codec=%s 分辨率=%sx%s", codec, self._size[0], self._size[1])
        self.broadcast(self._config_message)

    # ---------- 到达时钟 ---------- #

    def bind_timeline(self, t0_epoch_ms: int) -> None:
        self._t0_epoch_ms = int(t0_epoch_ms)
