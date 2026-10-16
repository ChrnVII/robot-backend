"""PostgreSQL LISTEN 事件监听：把 robot_core 的 NOTIFY 转成 WS 主题唤醒。

复用平台既有的 PostgreSQL（两侧已共用同一实例），不引入 Redis / 消息队列等
新组件：robot_core 写入关键数据时在事务内 NOTIFY，本监听器在后台线程
LISTEN 并把事件投递回事件循环，由 :class:`~app.api.ws.hub.PushHub` 唤醒
对应主题立即刷新。

设计要点：
- 独立 daemon 线程 + 独立只读连接（autocommit），断线自动重连；
- 数据库不可用时静默降级：WS 仍由 500ms 周期兜底推送，功能不缺失；
- 事件仅作「唤醒信号」，数据仍由 provider 经 IPC 读取，避免双写不一致。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from app.common.dburl import psycopg_dsn
from app.contracts import Channels

logger = logging.getLogger(__name__)

#: 单次 LISTEN 等待超时：用于周期性检查停止标志与连接存活。
_NOTIFY_POLL_TIMEOUT_SECONDS = 5.0
#: 断线重连间隔。
_RECONNECT_DELAY_SECONDS = 2.0
#: 连接与连接建立超时。
_CONNECT_TIMEOUT_SECONDS = 5.0

EventHandler = Callable[[Mapping[str, Any]], None]


class PgEventListener:
    """后台线程 LISTEN 指定通道，把载荷交给 ``on_event``（线程安全由调用方保证）。"""

    def __init__(
        self,
        dsn: str,
        *,
        channel: str = Channels.EVENTS,
        on_event: EventHandler | None = None,
        poll_timeout: float = _NOTIFY_POLL_TIMEOUT_SECONDS,
        reconnect_delay: float = _RECONNECT_DELAY_SECONDS,
    ) -> None:
        self._conninfo = psycopg_dsn(dsn)
        self._channel = channel
        self._on_event = on_event
        self._poll_timeout = poll_timeout
        self._reconnect_delay = reconnect_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pg-event-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._poll_timeout + _RECONNECT_DELAY_SECONDS + 1.0)
        self._thread = None
        self._connected = False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._listen_once()
            except Exception as error:  # noqa: BLE001 - 断线/权限问题：重连并降级到周期推送
                if self._stop.is_set():
                    break
                logger.warning("PostgreSQL 事件监听断开，%.1fs 后重连：%s", self._reconnect_delay, error)
                self._connected = False
                self._stop.wait(self._reconnect_delay)

    def _listen_once(self) -> None:
        import psycopg

        with psycopg.connect(
            self._conninfo, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_SECONDS
        ) as connection:
            connection.execute(f"LISTEN {self._channel}")
            self._connected = True
            logger.info("已订阅 PostgreSQL 通知通道 %s（WS 推送进入事件驱动模式）", self._channel)
            try:
                while not self._stop.is_set():
                    for notify in connection.notifies(timeout=self._poll_timeout):
                        if self._stop.is_set():
                            break
                        self._dispatch(notify.payload)
            finally:
                self._connected = False

    def _dispatch(self, payload: str | None) -> None:
        if self._on_event is None or not payload:
            return
        try:
            value = json.loads(payload)
        except (TypeError, ValueError):
            logger.debug("忽略非法事件载荷：%r", payload)
            return
        if not isinstance(value, dict):
            return
        try:
            self._on_event(value)
        except Exception:  # noqa: BLE001 - 回调异常不能中断监听线程
            logger.debug("事件回调失败", exc_info=True)
