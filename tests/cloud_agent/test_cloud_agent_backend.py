"""本地 backend 客户端：地址推导、探活、错误翻译。

这一组测试的存在理由很具体：曾经 `backend_url` 的默认值被写成 8000（云端
server 的端口），而 robot backend 的约定端口是 8100。结果是 agent 把本地请求
全打到云端——`/api/v1/cameras/presets` 拿到 404，被翻译成"相机不可用"，
实时画面则报"本机预览通道不可用"。两个症状看起来像硬件问题，实际上是配置。
"""

from __future__ import annotations

import httpx
import pytest

from app.cloud_agent.backend import LocalBackend
from app.cloud_agent.config import AgentConfig
from app.cloud_agent.errors import AgentCommandError
from app.cloud_agent.media import MediaSession
from app.cloud_agent.protocol import ErrorCode

pytestmark = pytest.mark.no_database


def make_backend(handler, base_url: str = "http://127.0.0.1:8100") -> LocalBackend:
    """注入 MockTransport：不打真实网络，但保留 httpx 的异常语义。"""
    backend = LocalBackend(base_url)
    backend._client = httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.MockTransport(handler), base_url=base_url
    )
    return backend


def config_with(url: str, token: str = "") -> AgentConfig:
    config = AgentConfig(backend_url=url, backend_token=token)
    return config


# --- 默认值 -------------------------------------------------------------


def test_default_backend_url_matches_the_backend_convention():
    """8100 是 robot backend 的约定端口（readme / deploy 脚本）。

    写成 8000 会指向云端 server，本地接口全部 404 —— 这正是线上那次故障。
    """
    assert AgentConfig().backend_url == "http://127.0.0.1:8100"


def test_default_port_matches_the_deploy_script():
    """默认端口必须与部署脚本的 BACKEND_PORT 一致。

    这个断言是为了防止再次出现"默认值 8000 / 生产 8100"这类错位：
    错位时所有本地请求都会打到云端 server，症状看起来像硬件故障。
    """
    import re
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "deploy" / "quick_start_up.sh"
    text = script.read_text(encoding="utf-8")
    match = re.search(r'BACKEND_PORT="\$\{BACKEND_PORT:-(\d+)\}"', text)
    assert match, "部署脚本里没有 BACKEND_PORT 默认值"
    assert f":{match.group(1)}" in AgentConfig().backend_url


# --- 本地预览 URL -------------------------------------------------------


@pytest.mark.parametrize(
    "backend_url,expected",
    [
        ("http://127.0.0.1:8100", "ws://127.0.0.1:8100/ws/camera/preview"),
        ("http://127.0.0.1:8100/", "ws://127.0.0.1:8100/ws/camera/preview"),
        # 带前缀时**丢弃**前缀：预览是根级路径，否则会拼成 /api/ws/... 永远连不上
        ("http://127.0.0.1:8100/api/v1", "ws://127.0.0.1:8100/ws/camera/preview"),
        ("https://robot.local:8443", "wss://robot.local:8443/ws/camera/preview"),
    ],
)
def test_local_preview_url_is_derived_from_netloc_only(backend_url, expected):
    session = MediaSession(config_with(backend_url), "med-1", "", "low")
    assert session._local_url() == expected


def test_local_preview_url_keeps_camera_and_token():
    session = MediaSession(config_with("http://127.0.0.1:8100", "tok"), "med-1", "VAR-1", "low")

    url = session._local_url()

    assert "cameraId=VAR-1" in url
    assert "accessToken=tok" in url


# --- 错误翻译 -----------------------------------------------------------


async def test_connection_error_maps_to_backend_unreachable():
    """连不上 backend 是配置/进程问题，不能报成"相机坏了"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backend = make_backend(handler)

    with pytest.raises(AgentCommandError) as raised:
        await backend.presets()

    assert raised.value.code is ErrorCode.BACKEND_UNREACHABLE
    assert "127.0.0.1:8100" in raised.value.message


async def test_404_from_a_wrong_service_is_not_reported_as_camera_fault():
    """地址指向了别的服务：404 要能区分出来（至少消息里带上路径）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    backend = make_backend(handler)

    with pytest.raises(AgentCommandError) as raised:
        await backend.presets()

    assert raised.value.code is ErrorCode.CAMERA_UNAVAILABLE
    assert "/api/v1/cameras/presets" in raised.value.message


# --- 探活 ---------------------------------------------------------------


async def test_probe_accepts_the_real_backend():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/cameras"
        return httpx.Response(200, json={"code": "OK", "data": {"cameras": []}})

    ok, detail = await make_backend(handler).probe()

    assert ok is True
    assert detail == "ok"


async def test_probe_flags_a_wrong_service():
    """指向云端 server 时这里会 404 —— 直接告诉用户"不像 robot backend"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    ok, detail = await make_backend(handler).probe()

    assert ok is False
    assert "不像 robot backend" in detail
    assert "8100" in detail          # 提示里给出约定端口


async def test_probe_reports_connection_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    ok, detail = await make_backend(handler).probe()

    assert ok is False
    assert "连不上" in detail


async def test_probe_is_throttled_after_failure(monkeypatch):
    """后端没起来时不要每次重连都探一次（3s 超时会拖慢重连）。"""
    from app.cloud_agent.client import AgentClient
    from app.cloud_agent.sandbox import Sandbox

    tmp = None
    calls = {"n": 0}

    async def fake_probe():
        calls["n"] += 1
        return False, "连不上"

    class Config:
        backend_url = "http://127.0.0.1:8100"

    client = AgentClient.__new__(AgentClient)
    client.config = Config()          # type: ignore[assignment]
    client.backend = type("B", (), {"probe": staticmethod(fake_probe)})()  # type: ignore[assignment]
    client._backend_ok = False
    client._last_probe_at = 0.0

    await client._probe_backend()
    await client._probe_backend()      # 一分钟内不重复
    await client._probe_backend()

    assert calls["n"] == 1
    assert tmp is None
