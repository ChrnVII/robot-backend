from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Mapping, Union


Handler = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class JsonSocketError(RuntimeError):
    """对端以稳定错误码拒绝请求（对齐旧 Java PeerRejectedException）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code or "peer_rejected"


class JsonSocketServer:
    """One-request-per-connection JSON IPC over a Unix domain socket."""

    def __init__(self, path: Union[str, Path], handler: Handler, max_request_bytes: int = 1024 * 1024):
        self.path = Path(path)
        self.handler = handler
        self.max_request_bytes = max_request_bytes
        self.server: Any = None

    async def start(self) -> None:
        if os.name == "nt":
            raise RuntimeError("Unix domain socket services require the target Linux IPC")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o660)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if len(raw) > self.max_request_bytes:
                raise ValueError("request too large")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request must be an object")
            result = dict(await self.handler(value))
            response = {"ok": True, "result": result}
        except Exception as error:
            response = {"ok": False, "error": type(error).__name__, "message": str(error)}
        writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def serve_forever(self) -> None:
        if self.server is None:
            await self.start()
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.path.unlink(missing_ok=True)


class JsonSocketClient:
    def __init__(self, path: Union[str, Path], timeout_seconds: float = 5.0):
        self.path = str(path)
        self.timeout_seconds = timeout_seconds

    async def call(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.path), self.timeout_seconds)
        writer.write(json.dumps(dict(request), ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), self.timeout_seconds)
        writer.close()
        await writer.wait_closed()
        response = json.loads(raw.decode("utf-8"))
        if not response.get("ok"):
            raise JsonSocketError(
                str(response.get("error") or "peer_rejected"),
                str(response.get("message") or "IPC call failed"),
            )
        return dict(response["result"])
