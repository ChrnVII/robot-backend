"""上传器测试：断点续传、重试、失败码。

用 `httpx.MockTransport` 假装 A：能精确控制"哪些块已经收到了"，从而确定性地
验证"只传缺失的块"这条最关键的续传逻辑。真起一个服务器做不到这一点——
本地传输太快，根本杀不到中间状态。
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.cloud_agent import uploader as uploader_module
from app.cloud_agent.config import AgentConfig
from app.cloud_agent.errors import UploadCancelled, UploadFailed
from app.cloud_agent.protocol import ErrorCode
from app.cloud_agent.sandbox import Sandbox
from app.cloud_agent.uploader import UploadJob, UploadManager

pytestmark = pytest.mark.no_database

CHUNK = 1024
PAYLOAD = bytes(range(256)) * 16


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "f.bin").write_bytes(PAYLOAD)

    config = AgentConfig(
        server="http://127.0.0.1:9",
        token="test-token",
        root=str(root),
        max_concurrent_uploads=3,
    )
    notes: list[dict] = []

    async def notify(payload: dict) -> None:
        notes.append(payload)

    return root, config, notes


def make_manager(config, transport: httpx.MockTransport) -> UploadManager:
    async def notify(payload: dict) -> None:
        return None

    manager = UploadManager(Sandbox(config.root), config, notify)
    manager._client = httpx.AsyncClient(transport=transport, base_url="http://fake")
    return manager


def job_for(path: str = "f.bin", total: int = len(PAYLOAD)) -> UploadJob:
    return UploadJob.from_payload(
        {
            "upload_id": "u" * 8,
            "rel": path,
            "filename": path,
            "total_size": total,
            "chunk_size": CHUNK,
            "total_chunks": total // CHUNK,
        }
    )


class FakeServer:
    """记录收到的块，并按配置返回状态码。"""

    def __init__(self, received: list[int] | None = None, put_failures: int = 0) -> None:
        self.received = received or []
        self.put_failures = put_failures
        self.puts: list[int] = []
        self.completed: str | None = None
        self.status_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/status"):
            self.status_calls += 1
            if self.status_calls > 1:
                return httpx.Response(404, json={"code": "file_not_found"})
            return httpx.Response(
                200,
                json={
                    "upload_id": "u" * 8,
                    "status": "uploading",
                    "chunk_size": CHUNK,
                    "total_chunks": len(PAYLOAD) // CHUNK,
                    "total_size": len(PAYLOAD),
                    "received": self.received,
                },
            )

        if request.method == "PUT":
            index = int(path.rsplit("/", 1)[-1])
            if self.put_failures > 0:
                self.put_failures -= 1
                return httpx.Response(500, json={"code": "internal", "message": "boom"})
            self.puts.append(index)
            assert request.headers["content-length"] == str(len(request.content))
            return httpx.Response(200, json={"received": len(self.puts)})

        if request.method == "POST" and path.endswith("/complete"):
            body = json.loads(request.content)
            self.completed = body["sha256"]
            return httpx.Response(200, json={"status": "done"})

        return httpx.Response(404, json={"code": "file_not_found", "message": path})


async def test_resume_only_uploads_missing_chunks(env):
    _root, config, _notes = env
    server = FakeServer(received=[0, 2])
    manager = make_manager(config, httpx.MockTransport(server.handler))

    try:
        await manager._upload(job_for())
    finally:
        await manager._client.aclose()

    assert server.puts == [1, 3]
    assert server.completed == hashlib.sha256(PAYLOAD).hexdigest()


async def test_all_chunks_present_still_produces_digest(env):
    _root, config, _notes = env
    server = FakeServer(received=[0, 1, 2, 3])
    manager = make_manager(config, httpx.MockTransport(server.handler))

    try:
        await manager._upload(job_for())
    finally:
        await manager._client.aclose()

    assert server.puts == []
    assert server.completed == hashlib.sha256(PAYLOAD).hexdigest()


async def test_chunk_retries_then_succeeds(env, monkeypatch):
    _root, config, _notes = env
    monkeypatch.setattr(uploader_module, "RETRY_BASE_DELAY", 0.001)

    server = FakeServer(received=[0, 1, 2, 3], put_failures=2)
    manager = make_manager(config, httpx.MockTransport(server.handler))

    server.received = [0, 1, 2]
    try:
        await manager._upload(job_for())
    finally:
        await manager._client.aclose()

    assert server.puts == [3]
    assert server.completed is not None


async def test_status_404_means_cancelled(env):
    _root, config, _notes = env

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "file_not_found"})

    manager = make_manager(config, httpx.MockTransport(handler))
    try:
        with pytest.raises(UploadCancelled):
            await manager._upload(job_for())
    finally:
        await manager._client.aclose()


async def test_size_mismatch_is_rejected_before_uploading(env):
    _root, config, _notes = env
    server = FakeServer()
    manager = make_manager(config, httpx.MockTransport(server.handler))

    try:
        with pytest.raises(UploadFailed) as excinfo:
            await manager._upload(job_for(total=len(PAYLOAD) - CHUNK))
    finally:
        await manager._client.aclose()

    assert excinfo.value.code is ErrorCode.SIZE_CHANGED
    assert server.puts == []


async def test_checksum_mismatch_from_server_is_propagated(env):
    _root, config, _notes = env

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "received": [0, 1, 2, 3],
                    "chunk_size": CHUNK,
                    "total_chunks": 4,
                    "total_size": len(PAYLOAD),
                },
            )
        return httpx.Response(
            409, json={"code": "checksum_mismatch", "message": "sha256 mismatch"}
        )

    manager = make_manager(config, httpx.MockTransport(handler))
    try:
        with pytest.raises(UploadFailed) as excinfo:
            await manager._upload(job_for())
    finally:
        await manager._client.aclose()

    assert excinfo.value.code is ErrorCode.CHECKSUM_MISMATCH


async def test_empty_file_completes_without_chunks(env, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "empty.txt").write_bytes(b"")

    config = AgentConfig(server="http://127.0.0.1:9", token="t", root=str(root))
    server = FakeServer()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"received": [], "chunk_size": CHUNK, "total_chunks": 0, "total_size": 0},
            )
        return server.handler(request)

    manager = make_manager(config, httpx.MockTransport(handler))
    job = UploadJob.from_payload(
        {
            "upload_id": "e" * 8,
            "rel": "empty.txt",
            "total_size": 0,
            "chunk_size": CHUNK,
            "total_chunks": 0,
        }
    )

    try:
        await manager._upload(job)
    finally:
        await manager._client.aclose()

    assert server.puts == []
    assert server.completed == hashlib.sha256(b"").hexdigest()


def test_complete_timeout_grows_with_file_size(env):
    """complete 要等 A 重算整个文件的 sha256，超时必须随大小增长。

    固定 60s 会让 20GB 在 150MB/s 的盘上（约 137s）"A 已经成功、B 却报失败"，
    而日志和事实相反是最费时间的排障场景。
    """
    root, config, _notes = env

    async def noop(payload: dict) -> None:
        pass

    manager = UploadManager(Sandbox(str(root)), config, noop)

    # 小文件不能因为公式反而变短
    assert manager._complete_timeout(0) == config.http_timeout

    twenty_gb = 20 * 1024**3
    timeout = manager._complete_timeout(twenty_gb)
    assert timeout > 300
    # 必须留在 deploy/nginx 里 /internal/ 的 proxy_read_timeout（600s）以内，
    # 否则 B 等到的是代理断开，又回到"分不清服务器还在干活还是连接死了"。
    assert timeout < 600


async def test_failure_is_reported_back(env):
    root, config, notes = env

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "file_not_found"})

    async def notify(payload: dict) -> None:
        notes.append(payload)

    manager = UploadManager(Sandbox(str(root)), config, notify)
    manager._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake"
    )

    try:
        await manager.start(job_for())
        for _ in range(100):
            if notes:
                break
            import asyncio

            await asyncio.sleep(0.01)
    finally:
        await manager.close()

    # upload 被 A 回收时不上报失败（A 已经知道了），所以这里应该是空的
    assert notes == []
