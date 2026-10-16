"""UDS JSON Lines IPC 协议层。

v2 改进（保持线协议向后兼容）：
- 客户端连接池：复用 UDS 连接，替代「每次调用新建连接」；遇到陈旧连接
  （对端已按旧语义单请求关闭）自动丢弃并重试一次；
- 写侧超时：连接、写（drain）、读全部有超时兜底；
- 服务端单连接多请求循环：配合客户端池化，消除高频路径上的连接风暴；
- 有界并发：服务端限制同时处理的连接数，超出限额快速关闭；
- 响应体大小上限：防止异常大响应拖垮对端；
- 稳定错误码：异常经 app.contracts.ipc_error_code 归一化回传，
  不再透传 Python 异常类名（旧类名由消费侧兼容映射）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Mapping, Union

from app.contracts.errors import ipc_error_code

logger = logging.getLogger(__name__)

Handler = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]

#: 等待连接名额的最长时间；超时说明对端已经过载，快速关闭让客户端感知失败。
_CONNECTION_ADMISSION_TIMEOUT_SECONDS = 5.0
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class JsonSocketError(RuntimeError):
    """对端以稳定错误码拒绝请求（对齐旧 Java PeerRejectedException）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code or "peer_rejected"


class JsonSocketServer:
    """JSON Lines IPC 服务端：单连接内循环处理多个请求（一行一请求）。"""

    def __init__(
        self,
        path: Union[str, Path],
        handler: Handler,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_connections: int = 64,
    ):
        self.path = Path(path)
        self.handler = handler
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.max_connections = max_connections
        self._limit: asyncio.Semaphore | None = None
        self.server: Any = None
        self._path_identity: tuple[int, int] | None = None
        self._lock_file = None
        self._clients: set[asyncio.Task] = set()

    async def start(self, *, start_serving: bool = True) -> None:
        if self.server is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A persistent sidecar lock serializes bind/recovery, including two new
        # instances racing to remove the same stale socket. Never unlink locks.
        import fcntl

        lock_file = self.path.with_suffix(self.path.suffix + ".lock").open("a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                if not stat.S_ISSOCK(self.path.lstat().st_mode):
                    raise RuntimeError(f"refusing to replace non-socket path: {self.path}")
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(str(self.path))
                    except ConnectionRefusedError:
                        self.path.unlink()
                    else:
                        raise RuntimeError(f"IPC socket is already in use: {self.path}")
            self._limit = asyncio.Semaphore(self.max_connections)
            self.server = await asyncio.start_unix_server(
                self._handle, path=str(self.path), limit=max(65536, self.max_request_bytes + 1),
                start_serving=start_serving,
            )
            metadata = self.path.stat()
            self._path_identity = (metadata.st_dev, metadata.st_ino)
            os.chmod(self.path, 0o660)
            self._lock_file = lock_file
        except BaseException:
            lock_file.close()
            raise

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            await self._handle_connection(reader, writer)
        finally:
            self._clients.discard(task)
            writer.close()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert self._limit is not None
        try:
            acquired = await asyncio.wait_for(
                self._limit.acquire(), _CONNECTION_ADMISSION_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("ipc server overloaded, rejecting connection (limit=%d)", self.max_connections)
            writer.close()
            await writer.wait_closed()
            return
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break  # 客户端已关闭连接
                response = await self._respond(raw)
                writer.write(response)
                try:
                    await asyncio.wait_for(writer.drain(), 10.0)
                except asyncio.TimeoutError:
                    break  # 客户端不读响应：放弃该连接
        except Exception:  # noqa: BLE001 - 传输层错误：记录后关闭该连接
            logger.debug("ipc connection error", exc_info=True)
        finally:
            self._limit.release()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 对端可能已先行关闭
                pass

    async def _respond(self, raw: bytes) -> bytes:
        try:
            if len(raw) > self.max_request_bytes:
                raise ValueError("request too large")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request must be an object")
            result = dict(await self.handler(value))
            response = {"ok": True, "result": result}
        except Exception as error:  # noqa: BLE001 - 业务与解析错误统一映射稳定错误码
            response = {"ok": False, "error": ipc_error_code(error), "message": str(error)}
        payload = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(payload) > self.max_response_bytes:
            fallback = json.dumps(
                {"ok": False, "error": ipc_error_code(ValueError("response too large")),
                 "message": "response too large"},
                ensure_ascii=False,
            ).encode("utf-8") + b"\n"
            return fallback
        return payload

    async def serve_forever(self) -> None:
        if self.server is None:
            await self.start()
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        try:
            if self._path_identity is not None and self.path.exists():
                metadata = self.path.stat()
                if (metadata.st_dev, metadata.st_ino) == self._path_identity:
                    self.path.unlink()
        finally:
            self._path_identity = None
            if self._lock_file is not None:
                self._lock_file.close()
                self._lock_file = None


class JsonSocketClient:
    """JSON Lines IPC 客户端：连接池复用 + 全链路超时 + 陈旧连接重试一次。"""

    def __init__(
        self,
        path: Union[str, Path],
        timeout_seconds: float = 5.0,
        *,
        pool_size: int = 8,
        max_attempts: int = 2,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.path = str(path)
        self.timeout_seconds = timeout_seconds
        self.pool_size = max(1, pool_size)
        self.max_attempts = max(1, max_attempts)
        self.max_response_bytes = max_response_bytes
        self._pool: Deque[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = deque()

    async def call(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(dict(request), ensure_ascii=False).encode("utf-8") + b"\n"
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        use_fresh = False  # 重试时强制新建连接：避免再次撞上池内的陈旧连接
        for attempt in range(self.max_attempts):
            pooled = None if use_fresh else self._pop_pooled()
            fresh = pooled is None
            try:
                if pooled is None:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_unix_connection(self.path, limit=self.max_response_bytes + 1),
                        self.timeout_seconds,
                    )
                else:
                    reader, writer = pooled
                writer.write(payload)
                await asyncio.wait_for(writer.drain(), self.timeout_seconds)
                raw = await asyncio.wait_for(reader.readline(), self.timeout_seconds)
                if not raw:
                    raise ConnectionError("peer closed connection before responding")
                if len(raw) > self.max_response_bytes:
                    raise ValueError("response too large")
                response = json.loads(raw.decode("utf-8"))
            except (asyncio.TimeoutError, ValueError):
                # 超时/坏响应：请求可能已被对端处理，重放有重复执行风险，直接失败。
                self._discard(reader, writer)
                raise
            except Exception as exc:
                self._discard(reader, writer)
                reader = writer = None
                if fresh or attempt >= self.max_attempts - 1:
                    raise
                # 仅陈旧（池化）连接的传输失败才重试：对端未曾收到本次请求。
                use_fresh = True
                continue
            self._push_pooled(reader, writer)
            if not response.get("ok"):
                raise JsonSocketError(
                    str(response.get("error") or "peer_rejected"),
                    str(response.get("message") or "IPC call failed"),
                )
            return dict(response["result"])
        raise ConnectionError(f"ipc call failed after {self.max_attempts} attempts: {self.path}")

    def _pop_pooled(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        while self._pool:
            candidate = self._pool.pop()
            writer = candidate[1]
            if writer.is_closing():
                self._close_quietly(candidate[0], candidate[1])
                continue
            return candidate
        return None

    def _push_pooled(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._pool) < self.pool_size and not writer.is_closing():
            self._pool.append((reader, writer))
        else:
            self._close_quietly(reader, writer)

    def _discard(
        self, reader: asyncio.StreamReader | None, writer: asyncio.StreamWriter | None
    ) -> None:
        if reader is not None or writer is not None:
            self._close_quietly(reader, writer)

    @staticmethod
    def _close_quietly(
        reader: asyncio.StreamReader | None, writer: asyncio.StreamWriter | None
    ) -> None:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def aclose(self) -> None:
        """释放池内全部连接（应用关闭时由网关层调用）。"""
        while self._pool:
            reader, writer = self._pool.pop()
            self._close_quietly(reader, writer)
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
