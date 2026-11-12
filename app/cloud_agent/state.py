"""运行期状态与日志环形缓冲。

页面要看到的东西分两类：

- **状态快照**：连接态、云端地址、agent_id、共享目录、上传队列、最近错误。
  全部活在内存里，由 supervisor 在事件发生时更新，控制面读它。
- **日志**：最近 500 行。不读文件，因为 journald 的权限、轮转、编码都可能让
  "看日志"这个动作失败；进程自己留一份环形缓冲最可靠。

这里不加锁：所有读写都在同一个事件循环里。唯一可能跨线程的是 logging
（某些第三方库会从线程里打日志），所以 LogBuffer 自己带锁。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .config import AgentConfig, resolve_state_dir
from .protocol import PROTOCOL_VERSION

LOG_CAPACITY = 500
MAX_MESSAGE_CHARS = 4000


class LogBuffer(logging.Handler):
    """把日志留在内存里，供控制面按序号增量拉取。"""

    def __init__(self, capacity: int = LOG_CAPACITY) -> None:
        super().__init__()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{self.formatException(record.exc_info)}"
        except Exception:  # noqa: BLE001 - 日志本身不能把进程打挂
            message = str(record.msg)
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + "…"

        with self._lock:
            self._seq += 1
            self._records.append(
                {
                    "seq": self._seq,
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )

    def snapshot(self, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            items = [item for item in self._records if item["seq"] > since]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq


#: 全局唯一。main 里安装到 root logger；控制面直接读。
LOG_BUFFER = LogBuffer()


def install_log_buffer() -> LogBuffer:
    logging.getLogger().addHandler(LOG_BUFFER)
    return LOG_BUFFER


@dataclass
class AgentState:
    """supervisor 与页面共享的一份快照。

    `state` 的取值：
      starting / unregistered / enrolling / pending / connecting / online /
      offline / unauthorized / paused / error / stopped
    """

    started_at: float = field(default_factory=time.time)
    state: str = "starting"
    paused: bool = False
    version: str = __version__
    protocol: int = PROTOCOL_VERSION
    hostname: str = field(default_factory=socket.gethostname)

    server: str = ""
    agent_id: str = ""
    root: str = ""
    name: str = ""

    connected_at: float | None = None
    last_heartbeat_at: float | None = None
    pending: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    last_event_at: float | None = None

    def configure(self, config: AgentConfig) -> None:
        self.server = config.server
        self.root = config.root
        self.name = config.name or ""
        self.agent_id = config.agent_id or ""

    def mark(self, state: str) -> None:
        self.state = state
        self.last_event_at = time.time()
        if state != "error":
            self.last_error = None

    def mark_error(self, code: str, message: str) -> None:
        self.state = "error"
        self.last_error = {"code": code, "message": message, "at": time.time()}
        self.last_event_at = time.time()

    def snapshot(self, config: AgentConfig, uploads: Any | None) -> dict[str, Any]:
        return {
            "state": self.state,
            "paused": self.paused,
            "version": self.version,
            "protocol": self.protocol,
            "hostname": self.hostname,
            "server": self.server,
            "agent_id": self.agent_id,
            "root": self.root,
            "name": self.name,
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_event_at": self.last_event_at,
            "pending": self.pending,
            "last_error": self.last_error,
            "uploads": uploads.snapshot() if uploads is not None else {"active": 0, "jobs": [], "recent": []},
            "paths": {
                "state_dir": str(resolve_state_dir(config)),
                "control_socket": config.control_socket,
            },
        }
