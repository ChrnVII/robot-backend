"""球形摄像头低延迟预览：纯单元用例（不触库）。

覆盖三类容易回归的地方：

1. **帧协议**：Annex-B 切分/重组、NAL 类型、帧头打包、codec 字符串 —— 这些一旦
   写错，浏览器侧就是全黑，且只有真实摄像头才能复现，所以必须在单测里钉住；
2. **背压策略**：订阅者队列溢出后必须整体清空并只等关键帧重同步（FLAG_RST），
   这是「延迟不随时间增长」的核心保证；
3. **降级路径**：未配置 RTSP 地址时服务不启动、WS 先发 error 再关闭，
   前端据此回退到 JPEG 轮询 —— 保证没接摄像头时页面依然可用。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from fractions import Fraction

import pytest

from app.api.ws import routes as ws_routes
from app.common.config import Settings, get_settings
from app.video.codec import (
    FLAG_KEY,
    FLAG_RST,
    HEADER_LENGTH,
    annexb,
    h264_codec_string,
    h264_nal_type,
    hevc_codec_string,
    hevc_nal_type,
    mask_credentials,
    open_options,
    pack_frame,
    redact_credentials,
    split_annexb,
    ticks_to_ms,
    unpack_frame_header,
)
from app.video.pipeline import CameraBroadcaster, FrameSlot, ViewerClient
from app.video.service import CameraStreamService

pytestmark = pytest.mark.no_database


@pytest.fixture(autouse=True)
def _auth_disabled(monkeypatch):
    """WS 路由鉴权依赖全局 Settings；显式关掉，避免本机 robot.env 影响用例。"""
    monkeypatch.setenv("ROBOT_AUTH_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 帧协议
# --------------------------------------------------------------------------- #

def test_split_annexb_handles_both_start_codes():
    # 4 字节起始码 + 3 字节起始码混用是 RTSP 的常态
    raw = b"\x00\x00\x00\x01\x67\x64\x00\x1f" + b"\x00\x00\x01\x68\xee"
    nals = split_annexb(raw)
    assert nals == [b"\x67\x64\x00\x1f", b"\x68\xee"]
    assert split_annexb(b"") == []


def test_annexb_round_trip_restores_start_codes():
    nals = [b"\x67\x64\x00\x1f", b"\x68\xee"]
    rebuilt = annexb(*nals)
    assert rebuilt.startswith(b"\x00\x00\x00\x01")
    assert split_annexb(rebuilt) == nals


def test_nal_types_and_h264_codec_string():
    sps = b"\x67\x64\x00\x1f"
    assert h264_nal_type(sps) == 7
    assert h264_codec_string(sps) == "avc1.64001F"
    # 非 SPS 不推导
    assert h264_codec_string(b"\x68\xee\x00\x00") is None
    # HEVC：NAL 头右移一位取类型，(33 << 1) 即 SPS
    assert hevc_nal_type(bytes([33 << 1]) + b"\x00" * 8) == 33
    assert hevc_nal_type(b"") == 0


def test_hevc_codec_string_never_raises_on_garbage():
    # 尽力而为的解析：解析不出来只能返回 None，绝不能抛异常打断拉流线程
    assert hevc_codec_string(b"\x42\x01\x01") is None          # 太短
    for sample in (b"\x42\x01" + b"\xff" * 32, bytes([33 << 1]) + b"\x01" * 16):
        result = hevc_codec_string(sample)
        assert result is None or result.startswith("hvc1.")


def test_frame_header_round_trip():
    payload = b"\x00\x00\x00\x01\x65payload"
    packet = pack_frame(FLAG_KEY, 123456, 7) + payload
    assert len(packet) == HEADER_LENGTH + len(payload)
    flags, pts, seq = unpack_frame_header(packet)
    assert (flags, pts, seq) == (FLAG_KEY, 123456, 7)
    assert packet[HEADER_LENGTH:] == payload


def test_open_options_are_low_latency():
    # 这几项正是「默认值面向点播文件」的元凶，必须显式压掉
    options = open_options()
    assert options["fflags"] == "nobuffer"
    assert options["max_delay"] == "0"
    assert options["reorder_queue_size"] == "0"
    assert options["probesize"] == "32768"
    assert options["rtsp_transport"] == "tcp"
    assert open_options(transport="udp")["rtsp_transport"] == "udp"
    assert open_options(transport="UDP")["rtsp_transport"] == "udp"
    assert open_options(timeout_us=1234)["timeout"] == "1234"


def test_ticks_to_ms_supports_fraction_and_avrational():
    class _AVRational:  # 新 PyAV 只有 .num/.den
        num, den = 1, 90000

    assert ticks_to_ms(90000, Fraction(1, 90000)) == 1000
    assert ticks_to_ms(90000, _AVRational()) == 1000
    assert ticks_to_ms(None, Fraction(1, 90000)) is None
    assert ticks_to_ms(1, None) is None
    assert ticks_to_ms(1, Fraction(0, 1)) is None


def test_mask_credentials_hides_password():
    masked = mask_credentials("rtsp://admin:Secret1!@192.168.137.64:554/Streaming/Channels/102")
    assert "Secret1!" not in masked
    assert masked == "rtsp://admin:***@192.168.137.64:554/Streaming/Channels/102"
    # 没有凭据的地址原样返回
    assert mask_credentials("rtsp://192.168.137.64:554/s") == "rtsp://192.168.137.64:554/s"
    assert mask_credentials("") == ""


def test_redact_credentials_scrubs_free_text():
    """FFmpeg 的报错会把完整 RTSP 地址带出来，落日志/进状态接口前必须擦掉。"""
    raw = (
        "[Errno 138] Error number -138 occurred: "
        "'rtsp://admin:Secret1!@192.168.137.64:554/Streaming/Channels/102'"
    )
    cleaned = redact_credentials(raw)
    assert "Secret1!" not in cleaned
    assert cleaned == (
        "[Errno 138] Error number -138 occurred: "
        "'rtsp://admin:***@192.168.137.64:554/Streaming/Channels/102'"
    )
    # 同样处理 http(s)，并保留非 URL 文本
    assert redact_credentials("failed https://u:p@h/x") == "failed https://u:***@h/x"
    assert redact_credentials("camera unavailable: /dev/video0") == "camera unavailable: /dev/video0"
    assert redact_credentials(None) == ""


# --------------------------------------------------------------------------- #
# 背压：最新帧槽位与订阅者队列
# --------------------------------------------------------------------------- #

def test_frame_slot_keeps_only_latest_and_reports_drops():
    slot = FrameSlot()
    assert slot.take(timeout=0.01) is None
    slot.put("frame-1", 10)
    slot.put("frame-2", 20)          # frame-1 还没被取走就被顶掉
    assert slot.dropped == 1
    assert slot.take(timeout=0.01) == ("frame-2", 20, None)
    assert slot.take(timeout=0.01) is None


def test_viewer_client_overflow_waits_for_keyframe_and_flags_reset():
    client = ViewerClient(max_queue=2)
    client.offer(pack_frame(0, 1, 1))
    client.offer(pack_frame(0, 2, 2))
    assert client.queue.qsize() == 2

    client.offer(pack_frame(0, 3, 3))          # 溢出：整体清空 + 标记 need_reset
    assert client.dropped == 1
    assert client.need_reset is True

    client.offer(pack_frame(0, 4, 4))          # 丢帧后非关键帧直接丢弃
    assert client.queue.qsize() == 0

    client.offer(pack_frame(FLAG_KEY, 5, 5))   # 关键帧重新起步，并补 FLAG_RST
    frame = client.queue.get_nowait()
    flags, pts, seq = unpack_frame_header(frame)
    assert flags & FLAG_KEY
    assert flags & FLAG_RST
    assert (pts, seq) == (5, 5)
    assert client.need_reset is False


def test_viewer_client_never_blocks_on_json_backlog():
    client = ViewerClient(max_queue=1)
    client.offer("first")
    client.offer("second")                     # JSON 溢出：静默丢弃，不清队列
    assert client.queue.qsize() == 1
    assert client.queue.get_nowait() == "first"


def test_broadcaster_builds_config_message_only_with_codec():
    broadcaster = CameraBroadcaster(max_queue=2)
    broadcaster.set_size((1280, 720))
    assert broadcaster.config_message is None            # 还不知道编码就不发配置
    broadcaster.bind_timeline(1700000000000)
    broadcaster.set_codec("avc1.64001F")
    assert json.loads(broadcaster.config_message) == {
        "t": "config", "codec": "avc1.64001F", "t0": 1700000000000, "size": [1280, 720],
    }
    assert broadcaster.codec == "avc1.64001F"
    assert broadcaster.size == (1280, 720)


def test_broadcast_without_viewers_or_loop_is_a_noop():
    broadcaster = CameraBroadcaster()
    broadcaster.broadcast(b"\x00")          # 无订阅者：不应抛错
    broadcaster.attach_loop(asyncio.new_event_loop())
    broadcaster.broadcast(b"\x00")          # 有循环但无订阅者：同样空操作
    assert broadcaster.client_count == 0


# --------------------------------------------------------------------------- #
# 降级路径
# --------------------------------------------------------------------------- #

def _settings(**overrides) -> Settings:
    # 不读 robot.env：避免本机真实配置影响断言（init 参数优先级高于环境变量）
    return Settings(_env_file=None, **overrides)


def test_service_is_disabled_without_rtsp_url():
    service = CameraStreamService(_settings(camera_stream_url=""))
    assert service.enabled is False
    assert service.start(asyncio.new_event_loop()) is False
    assert service.broadcaster is None
    status = service.status()
    assert status["started"] is False
    assert status["reason"] == "url_not_configured"
    assert status["detectEnabled"] is False
    service.stop()   # 未启动时应当是安全的空操作


def test_service_rejects_non_rtsp_scheme():
    service = CameraStreamService(_settings(camera_stream_url="http://192.168.137.64/snapshot"))
    assert service.enabled is True                      # 有地址即视为「启用」，但方案不支持
    assert service.start(asyncio.new_event_loop()) is False
    assert service.skip_reason == "unsupported_scheme"


def test_service_respects_explicit_disable_switch():
    service = CameraStreamService(_settings(
        camera_stream_enabled=False, camera_stream_url="rtsp://h/s",
    ))
    assert service.enabled is False
    assert service.start(asyncio.new_event_loop()) is False
    assert service.skip_reason == "disabled_by_config"


def test_status_masks_rtsp_credentials():
    service = CameraStreamService(_settings(camera_stream_url="rtsp://admin:pw@10.0.0.9:554/s"))
    assert "pw" not in service.status()["url"]
    assert service.status()["url"] == "rtsp://admin:***@10.0.0.9:554/s"


class _FakeWebSocket:
    """只记录调用，不承载真实连接。"""

    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[tuple[str, object]] = []
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: object) -> None:
        self.sent.append(("json", payload))

    async def send_text(self, payload: str) -> None:
        self.sent.append(("text", payload))

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(("bytes", payload))

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class _StubService:
    def __init__(self, broadcaster: CameraBroadcaster | None, reason: str = "") -> None:
        self.broadcaster = broadcaster
        self.skip_reason = reason


def test_preview_route_reports_disabled_then_closes(monkeypatch):
    """未启用时必须先发 error JSON 再关闭 —— 前端据此回退到 JPEG 轮询。"""
    monkeypatch.setattr(
        ws_routes, "get_camera_stream_service",
        lambda: _StubService(None, reason="url_not_configured"),
    )
    socket = _FakeWebSocket()
    asyncio.run(ws_routes.camera_preview_socket(socket, accessToken=None))

    assert socket.accepted is True
    assert socket.sent, "未启用时应当先给前端一条可读的错误说明"
    kind, payload = socket.sent[0]
    assert kind == "json"
    assert payload["t"] == "error"
    assert payload["code"] == "stream_disabled"
    assert payload["reason"] == "url_not_configured"
    assert socket.closed_code == 1012


def test_preview_route_streams_config_then_frames(monkeypatch):
    """启用后：先发 config 文本，随后把队列里的二进制帧原样转发。"""
    broadcaster = CameraBroadcaster(max_queue=4)
    broadcaster.set_size((640, 360))
    broadcaster.bind_timeline(1700000000000)
    broadcaster.set_codec("avc1.64001F")
    monkeypatch.setattr(ws_routes, "get_camera_stream_service", lambda: _StubService(broadcaster))

    socket = _FakeWebSocket()

    async def scenario() -> None:
        task = asyncio.create_task(ws_routes.camera_preview_socket(socket, accessToken=None))
        for _ in range(100):                     # 等路由完成订阅
            if broadcaster.client_count:
                break
            await asyncio.sleep(0.01)
        assert broadcaster.client_count == 1

        broadcaster.viewers[0].offer(pack_frame(FLAG_KEY, 42, 1) + b"\x00\x00\x00\x01\x65payload")
        for _ in range(100):
            if len(socket.sent) >= 2:
                break
            await asyncio.sleep(0.01)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert socket.sent[0][0] == "text"
    assert json.loads(socket.sent[0][1])["t"] == "config"
    assert socket.sent[1][0] == "bytes"
    flags, pts, _ = unpack_frame_header(socket.sent[1][1])
    assert flags & FLAG_KEY and pts == 42
    # 断开后订阅者必须被摘掉，否则广播会一直往死连接里塞
    assert broadcaster.client_count == 0


def test_frame_slot_is_thread_safe_across_threads():
    slot = FrameSlot()
    stop = threading.Event()

    def producer() -> None:
        for index in range(500):
            slot.put(index, index)
        stop.set()

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    received = 0
    while time.monotonic() < deadline:
        item = slot.take(timeout=0.05)
        if item is not None:
            received += 1
        elif stop.is_set():
            break
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert received > 0
    # 槽位只保留最新一帧：产出与丢弃之和不应超过总产出
    assert received + slot.dropped <= 500
