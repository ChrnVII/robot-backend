"""UDS JSON Lines IPC：连接复用、陈旧连接重试、稳定错误码、限额保护。

Unix domain socket 仅 POSIX 可用，Windows 下整体跳过（与既有 UDS 用例一致）。
"""

import asyncio
import json
import socket

import pytest

from app.ipc import JsonSocketClient, JsonSocketError, JsonSocketServer

pytestmark = [
    pytest.mark.no_database,
    pytest.mark.skipif(
        not hasattr(socket, "AF_UNIX"), reason="Unix domain socket requires a POSIX platform"
    ),
]


class CountingServer(JsonSocketServer):
    """记录接受过的连接数，用于断言客户端连接复用。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connections = 0

    async def _handle(self, reader, writer):
        self.connections += 1
        await super()._handle(reader, writer)


class LegacyOneShotServer(CountingServer):
    """模拟旧版服务端：响应一次后立即关闭连接（滚动升级兼容场景）。"""

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            raw = await reader.readline()
            if raw:
                writer.write(await self._respond(raw))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def _handler(request):
    operation = request.get("operation")
    if operation == "echo":
        return {"value": request.get("value")}
    if operation == "missing":
        raise KeyError("resource not found")
    if operation == "invalid":
        raise ValueError("bad field")
    if operation == "fail":
        raise RuntimeError("boom")
    raise ValueError("unsupported operation")


def test_reuses_single_connection_across_calls(tmp_path):
    async def scenario():
        server = CountingServer(tmp_path / "s.sock", _handler)
        await server.start()
        try:
            client = JsonSocketClient(tmp_path / "s.sock")
            for index in range(5):
                assert (await client.call({"operation": "echo", "value": index}))["value"] == index
            assert server.connections == 1
            await client.aclose()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_retries_stale_pooled_connection(tmp_path):
    """对端按旧语义响应一次即关闭时，客户端应丢弃陈旧连接并重试成功。"""

    async def scenario():
        server = LegacyOneShotServer(tmp_path / "s.sock", _handler)
        await server.start()
        try:
            client = JsonSocketClient(tmp_path / "s.sock")
            assert (await client.call({"operation": "echo", "value": 1}))["value"] == 1
            # 第二次调用会取到已关闭的池化连接：自动重试新连接。
            assert (await client.call({"operation": "echo", "value": 2}))["value"] == 2
            assert server.connections == 2
            await client.aclose()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_server_handles_many_requests_per_connection(tmp_path):
    async def scenario():
        server = CountingServer(tmp_path / "s.sock", _handler)
        await server.start()
        try:
            client = JsonSocketClient(tmp_path / "s.sock")
            results = await asyncio.gather(
                *(client.call({"operation": "echo", "value": index}) for index in range(20))
            )
            assert sorted(item["value"] for item in results) == list(range(20))
            await client.aclose()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_error_codes_are_stable(tmp_path):
    async def scenario():
        server = JsonSocketServer(tmp_path / "s.sock", _handler)
        await server.start()
        try:
            client = JsonSocketClient(tmp_path / "s.sock")
            for operation, expected in (
                ("missing", "NOT_FOUND"),
                ("invalid", "INVALID_REQUEST"),
                ("fail", "INTERNAL_ERROR"),
            ):
                with pytest.raises(JsonSocketError) as excinfo:
                    await client.call({"operation": operation})
                assert excinfo.value.code == expected
            await client.aclose()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_oversized_request_is_rejected_without_killing_connection(tmp_path):
    async def scenario():
        server = CountingServer(tmp_path / "s.sock", _handler, max_request_bytes=256)
        await server.start()
        try:
            client = JsonSocketClient(tmp_path / "s.sock")
            with pytest.raises(JsonSocketError) as excinfo:
                await client.call({"operation": "echo", "value": "x" * 1024})
            assert excinfo.value.code == "INVALID_REQUEST"
            # 限额保护不应破坏连接：后续请求仍可正常处理。
            assert (await client.call({"operation": "echo", "value": 7}))["value"] == 7
            assert server.connections == 1
            await client.aclose()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_missing_socket_reports_unavailable(tmp_path):
    async def scenario():
        client = JsonSocketClient(tmp_path / "absent.sock", timeout_seconds=0.5)
        with pytest.raises(Exception):
            await client.call({"operation": "echo"})
        await client.aclose()

    asyncio.run(scenario())


def test_server_rejects_non_object_request(tmp_path):
    async def scenario():
        server = JsonSocketServer(tmp_path / "s.sock", _handler)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(tmp_path / "s.sock"))
            writer.write(json.dumps([1, 2, 3]).encode() + b"\n")
            await writer.drain()
            response = json.loads((await reader.readline()).decode())
            assert response["ok"] is False
            assert response["error"] == "INVALID_REQUEST"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()

    asyncio.run(scenario())


def test_large_frame_response_and_request_reuse_connection(tmp_path):
    async def scenario():
        server = CountingServer(tmp_path / "large.sock", _handler)
        await server.start()
        client = JsonSocketClient(tmp_path / "large.sock")
        try:
            value = "x" * (256 * 1024)
            assert await client.call({"operation": "echo", "value": value}) == {"value": value}
            assert await client.call({"operation": "echo", "value": "small"}) == {"value": "small"}
            assert server.connections == 1
        finally:
            await client.aclose()
            await server.close()
    asyncio.run(scenario())


def test_duplicate_server_cannot_remove_active_socket(tmp_path):
    async def scenario():
        path = tmp_path / "owner.sock"
        owner, duplicate = JsonSocketServer(path, _handler), JsonSocketServer(path, _handler)
        client = JsonSocketClient(path)
        await owner.start()
        try:
            with pytest.raises((OSError, RuntimeError)):
                await duplicate.start()
            await duplicate.close()
            assert await client.call({"operation": "echo", "value": 42}) == {"value": 42}
        finally:
            await client.aclose()
            await owner.close()
        assert not path.exists()
        await duplicate.start()
        await duplicate.close()
    asyncio.run(scenario())


def test_stale_socket_can_be_recovered_but_regular_file_is_preserved(tmp_path):
    import socket

    async def scenario():
        path = tmp_path / "stale.sock"
        with socket.socket(socket.AF_UNIX) as abandoned:
            abandoned.bind(str(path))
        server = JsonSocketServer(path, _handler)
        await server.start()
        await server.close()
        path.write_text("keep")
        with pytest.raises(RuntimeError, match="non-socket"):
            await server.start()
        await server.close()
        assert path.read_text() == "keep"
    asyncio.run(scenario())
