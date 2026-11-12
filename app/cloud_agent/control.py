"""本地控制面：UDS 上的 JSON-Lines 请求/响应。

为什么不是 HTTP：cloud-agent 不允许监听入站端口（哪怕是 127.0.0.1），而
backend 已经在用 UDS 做进程间通信（`app/ipc/json_socket.py`）。这里保持同一
种形态，本地页面经 backend 转发，链路上不出现任何 TCP 监听。

协议很短：

    {"op": "status"}                      → {"ok": true, "result": {...}}
    {"op": "logs", "since": 12}           → {"ok": true, "result": {"items": [...]}}
    {"op": "set_config", "config": {...}} → {"ok": true, "result": {...}}

失败统一 `{"ok": false, "error": "<code>", "message": "..."}`，不让异常类名
穿过进程边界——backend 要靠 code 决定给页面显示什么。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import ConfigError
from .state import LOG_BUFFER

logger = logging.getLogger("robot-cloud-agent.control")

MAX_LINE_BYTES = 1024 * 1024
MAX_CONNECTIONS = 16
CONNECTION_ADMISSION_TIMEOUT = 5.0
WRITE_TIMEOUT = 10.0


class ControlError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AgentControl:
    """把控制面的 op 映射到 supervisor 的方法。

    supervisor 是唯一的状态持有者；这里只做参数校验与错误翻译，不藏业务逻辑。
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "status": self._status,
            "logs": self._logs,
            "get_config": self._get_config,
            "set_config": self._set_config,
            "reload_config": self._reload_config,
            "list_credentials": self._list_credentials,
            "put_credential": self._put_credential,
            "activate_credential": self._activate_credential,
            "forget_credential": self._forget_credential,
            "clear_credentials": self._clear_credentials,
            "enroll": self._enroll,
            "cancel_enroll": self._cancel_enroll,
            "reconnect": self._reconnect,
            "pause": self._pause,
            "resume": self._resume,
            "rotate": self._rotate,
            "cancel_upload": self._cancel_upload,
            "restart": self._restart,
        }

    @property
    def ops(self) -> list[str]:
        return sorted(self._handlers)

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if not isinstance(op, str) or not op:
            raise ControlError("invalid_request", "缺少 op")
        handler = self._handlers.get(op)
        if handler is None:
            raise ControlError("unknown_op", f"未知操作：{op}")
        return await handler(request)

    # --- 查询 -----------------------------------------------------------

    async def _status(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.agent.status()

    async def _logs(self, request: dict[str, Any]) -> dict[str, Any]:
        since = _int(request.get("since"), default=0, low=0, high=2**31)
        limit = _int(request.get("limit"), default=200, low=1, high=500)
        return {
            "items": LOG_BUFFER.snapshot(since=since, limit=limit),
            "last_seq": LOG_BUFFER.last_seq,
        }

    async def _get_config(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.agent.config_view()

    async def _list_credentials(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.agent.list_credentials()

    # --- 改配置 ---------------------------------------------------------

    async def _set_config(self, request: dict[str, Any]) -> dict[str, Any]:
        payload = request.get("config")
        if not isinstance(payload, dict):
            raise ControlError("invalid_request", "config 必须是对象")
        return await self.agent.set_config(payload)

    async def _reload_config(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.reload_config()

    # --- 凭据 -----------------------------------------------------------

    async def _put_credential(self, request: dict[str, Any]) -> dict[str, Any]:
        server = _string(request, "server")
        token = _string(request, "token")
        return await self.agent.put_credential(
            server=server,
            token=token,
            agent_id=_optional_string(request, "agent_id"),
            root=_optional_string(request, "root"),
            name=_optional_string(request, "name"),
        )

    async def _activate_credential(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.activate_credential(_string(request, "server"))

    async def _forget_credential(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.forget_credential(_string(request, "server"))
    async def _clear_credentials(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.clear_credentials()

    # --- 连接与注册 -----------------------------------------------------

    async def _enroll(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.start_enroll(
            server=_optional_string(request, "server"),
            name=_optional_string(request, "name"),
            root=_optional_string(request, "root"),
        )

    async def _cancel_enroll(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.cancel_enroll()

    async def _reconnect(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.reconnect()

    async def _pause(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.pause()

    async def _resume(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.resume()

    async def _rotate(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.rotate_token()

    # --- 上传与进程 -----------------------------------------------------

    async def _cancel_upload(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.cancel_upload(_string(request, "upload_id"))

    async def _restart(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self.agent.restart_process()


class ControlServer:
    """UDS 服务端：一条连接上循环处理多行请求。"""

    def __init__(
        self,
        path: str | os.PathLike[str],
        control: AgentControl,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self.path = Path(path)
        self.control = control
        self.max_connections = max_connections
        self.server: Any = None
        self._limit: asyncio.Semaphore | None = None
        self._clients: set[asyncio.Task[None]] = set()
        self._identity: tuple[int, int] | None = None

    async def start(self) -> None:
        if self.server is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_socket()
        self._limit = asyncio.Semaphore(self.max_connections)
        self.server = await asyncio.start_unix_server(
            self._handle, path=str(self.path), limit=MAX_LINE_BYTES + 1
        )
        metadata = self.path.stat()
        self._identity = (metadata.st_dev, metadata.st_ino)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - 非 POSIX
            pass
        logger.info("控制面已监听 %s", self.path)

    def _prepare_socket(self) -> None:
        """处理上一次异常退出留下的 socket 文件。

        只删"确认连不上"的 socket；如果另一个实例正在监听，直接报错而不是抢。
        """
        if not self.path.exists() and not self.path.is_symlink():
            return
        if not stat.S_ISSOCK(self.path.lstat().st_mode):
            raise RuntimeError(f"拒绝覆盖非 socket 路径：{self.path}")
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink()
            except OSError as exc:
                raise RuntimeError(f"控制面 socket 不可用：{self.path}：{exc}") from exc
            else:
                raise RuntimeError(f"控制面已在运行：{self.path}")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        assert self._limit is not None
        try:
            await asyncio.wait_for(self._limit.acquire(), CONNECTION_ADMISSION_TIMEOUT)
        except TimeoutError:
            logger.warning("控制面连接数已满，拒绝新连接")
            writer.close()
            return
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                response = await self._respond(raw)
                writer.write(response)
                try:
                    await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
                except TimeoutError:
                    break
        except Exception:
            logger.debug("控制面连接异常", exc_info=True)
        finally:
            self._limit.release()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                logger.debug("关闭控制面连接时出错", exc_info=True)
            if task is not None:
                self._clients.discard(task)

    async def _respond(self, raw: bytes) -> bytes:
        try:
            if len(raw) > MAX_LINE_BYTES:
                raise ControlError("request_too_large", "请求过大")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ControlError("invalid_request", "请求必须是 JSON 对象")
            result = await self.control.handle(value)
            response = {"ok": True, "result": result}
        except ControlError as exc:
            response = {"ok": False, "error": exc.code, "message": exc.message}
        except ConfigError as exc:
            # supervisor 的配置校验失败是调用方问题，给 400 而不是 500
            response = {"ok": False, "error": "invalid_config", "message": str(exc)}
        except Exception as exc:
            logger.exception("控制面处理失败")
            response = {"ok": False, "error": "internal", "message": str(exc)}
        return json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*self._clients, return_exceptions=True)
        self._clients.clear()
        try:
            if self._identity is not None and self.path.exists():
                metadata = self.path.stat()
                if (metadata.st_dev, metadata.st_ino) == self._identity:
                    self.path.unlink()
        except OSError:  # pragma: no cover
            pass
        finally:
            self._identity = None


# --- 参数小工具 ---------------------------------------------------------


def _string(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ControlError("invalid_request", f"{key} 必须是非空字符串")
    return value.strip()


def _optional_string(request: dict[str, Any], key: str) -> str | None:
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControlError("invalid_request", f"{key} 必须是字符串")
    return value.strip() or None


def _int(value: Any, *, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ControlError("invalid_request", "数值参数不合法") from exc
    return max(low, min(high, number))
