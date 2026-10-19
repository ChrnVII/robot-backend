"""Annex-B 帧结构、NAL 类型与 WebCodecs codec 字符串。

本模块是纯字节/时间工具，不导入 PyAV 与 numpy，便于单元测试与复用。
协议与浏览器侧保持一致：

    文本 JSON  {"t": "config", "codec": ..., "t0": ..., "size": [w, h]}
               {"t": "det", ...}
    二进制帧   <B I I> flags(1) + pts_ms(4) + seq(4) + Annex-B 载荷

解码全部在浏览器 WebCodecs 内完成，服务端只解封装与转发。
"""

from __future__ import annotations

import re
import struct
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# --- WebSocket 二进制帧头 --- #
HEADER = struct.Struct("<BII")
HEADER_LENGTH = HEADER.size

FLAG_KEY = 0x01   # 关键帧（IDR），客户端可在此重建解码器
FLAG_CFG = 0x02   # 配置帧（预留）
FLAG_RST = 0x04   # 服务端丢过帧，客户端必须先 reset 解码器

# --- H.264 NAL 类型 --- #
NAL_SPS = 7
NAL_PPS = 8
NAL_IDR = 5

# --- HEVC NAL 类型 --- #
HEVC_VPS = 32
HEVC_SPS = 33
HEVC_PPS = 34
HEVC_IDR_W_RADL = 19
HEVC_IDR_N_LP = 20
HEVC_CRA = 21

#: 无法从 SPS 推导 HEVC codec 字符串时的兜底值（见 reader 的告警）。
HEVC_CODEC_FALLBACK = "hvc1.1.6.L120.B0"

MAIN_STREAM_CLIP = "main"
SUB_STREAM_CLIP = "sub"


class Timeline:
    """单调毫秒时钟：WS 帧头与检测记录的 pts 共用同一条「到达时钟」。

    用 ``time.monotonic()`` 而不是 ``time.time()``：后者会被 NTP 校时或夏令时
    回拨，一旦回退就会让链路时间戳非单调。``t0_epoch_ms`` 用于前端估算端到端延迟。
    """

    __slots__ = ("_t0_mono", "t0_epoch_ms")

    def __init__(self) -> None:
        self._t0_mono = time.monotonic()
        self.t0_epoch_ms = int(time.time() * 1000)

    def now_ms(self) -> int:
        """当前时刻（毫秒），单调不减，可直接当 PTS 用。"""
        return round((time.monotonic() - self._t0_mono) * 1000.0)

    def monotonic_at(self, pts_ms: int) -> float:
        """Convert a preview arrival timestamp back to the host monotonic clock."""
        return self._t0_mono + pts_ms / 1000.0


def open_options(*, transport: str = "tcp", timeout_us: int = 5_000_000) -> dict[str, str]:
    """低延迟拉流参数（AVFormatContext / RTSP 协议层）。

    FFmpeg 的默认值是按「点播文件」设计的：probesize 5MB、max_delay 0.5s，
    这些缓冲在地面移动机器人上会直接变成一两秒的延迟，必须显式压到最小。
    取值统一转成字符串，避免不同 PyAV 版本对类型校验不一致。
    """
    return {
        # 局域网且愿意容忍偶发花屏时改 udp 能再低一点；tcp 更稳且丢包会重传
        "rtsp_transport": "udp" if str(transport).lower() == "udp" else "tcp",
        "fflags": "nobuffer",          # 关掉 AVIO 输入缓冲
        "flags": "low_delay",          # 关掉解码器内部重排序
        "analyzeduration": "100000",   # 0.1s（默认很大）
        "probesize": "32768",          # 32KB（默认 5MB）
        "max_delay": "0",              # RTP 重排序窗口归零（默认 500000us）
        "reorder_queue_size": "0",
        # FFmpeg 7（PyAV>=14 内置）里 RTSP 的 stimeout 已废弃，统一用 timeout（微秒）
        "timeout": str(max(1, int(timeout_us))),
    }


def pack_frame(flags: int, pts_ms: int, seq: int) -> bytes:
    """打包一帧的 9 字节帧头。"""
    return HEADER.pack(flags & 0xFF, pts_ms & 0xFFFFFFFF, seq & 0xFFFFFFFF)


def unpack_frame_header(raw: bytes) -> tuple[int, int, int]:
    """解出帧头 (flags, pts_ms, seq)；载荷从 ``HEADER_LENGTH`` 开始。"""
    flags, pts_ms, seq = HEADER.unpack_from(raw, 0)
    return flags, pts_ms, seq


def split_annexb(data: bytes) -> list[bytes]:
    """按起始码切分 Annex-B 码流，返回**不含起始码**的 NAL 单元列表。"""
    if not data:
        return []
    starts: list[tuple[int, int]] = []
    i, n = 0, len(data)
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                starts.append((i, 3))
                i += 3
                continue
            if i + 4 <= n and data[i + 2] == 0 and data[i + 3] == 1:
                starts.append((i, 4))
                i += 4
                continue
        i += 1

    out: list[bytes] = []
    for idx, (pos, start_code) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        nal = data[pos + start_code:end]
        if nal:
            out.append(nal)
    return out


def annexb(*nals: bytes) -> bytes:
    """把裸 NAL 重新拼成 Annex-B（补回 4 字节起始码）。"""
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in nals if nal)


def h264_nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F if nal else 0


def hevc_nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F if nal else 0


class _BitReader:
    """按位读取，只给 HEVC SPS 解析用。"""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte_index = self.pos >> 3
            if byte_index >= len(self.data):
                return value                      # 数据被截断，返回已读到的部分
            bit = (self.data[byte_index] >> (7 - (self.pos & 7))) & 1
            value = (value << 1) | bit
            self.pos += 1
        return value

    def skip(self, count: int) -> None:
        self.pos += count


def h264_codec_string(sps: bytes) -> str | None:
    """由 H.264 SPS 推导 WebCodecs 需要的 ``avc1.PPCCLL``。"""
    if len(sps) < 4 or h264_nal_type(sps) != NAL_SPS:
        return None
    return f"avc1.{sps[1]:02X}{sps[2]:02X}{sps[3]:02X}"


def hevc_codec_string(sps: bytes) -> str | None:
    """由 HEVC SPS 推导 WebCodecs 需要的 ``hvc1.*`` 字符串（尽力而为）。

    Chrome 的 HEVC 支持本身依赖平台且对字符串宽容度有限；解析不出来时调用方
    会回退到 :data:`HEVC_CODEC_FALLBACK` 并提示改用 H.264。
    """
    if len(sps) < 8 or hevc_nal_type(sps) != HEVC_SPS:
        return None
    try:
        reader = _BitReader(sps[2:])              # 跳过 2 字节 NAL header
        reader.u(4)                               # sps_video_parameter_set_id
        max_sub_layers_minus1 = reader.u(3)
        reader.u(1)                               # sps_temporal_id_nesting_flag
        profile_space = reader.u(2)
        tier_flag = reader.u(1)
        profile_idc = reader.u(5)
        compat = reader.u(32)                     # general_profile_compatibility_flags
        reader.u(1)                               # general_progressive_source_flag
        reader.u(1)                               # general_interlaced_source_flag
        reader.u(1)                               # general_non_packed_constraint_flag
        reader.u(1)                               # general_frame_only_constraint_flag
        reader.skip(43 if profile_idc in (4, 5, 6, 7, 8, 9, 10, 11) else 44)
        level_idc = reader.u(8)
    except Exception:  # noqa: BLE001 - 尽力而为的解析，异常即退化为「解析不出来」
        return None
    if max_sub_layers_minus1 > 7:                 # 明显解析歪了
        return None
    space = "ABC"[profile_space] if 0 < profile_space < 3 else ""
    tier = "H" if tier_flag else "L"
    return f"hvc1.{space}{profile_idc}.{compat:X}.{tier}{level_idc}.B0"


def _tb_parts(tb: Any) -> tuple[int, int] | None:
    """取有理数的 (分子, 分母)，兼容 Fraction（旧 PyAV）与 AVRational（新 PyAV）。

    注意新 PyAV 里 time_base 未设置时不再是 None 而是 AVRational(0, 1)，
    所以判空必须用 ``not tb``，不能用 ``is None``。
    """
    if not tb:
        return None
    num = getattr(tb, "numerator", None)
    den = getattr(tb, "denominator", None)
    if num is None or den is None:                # av.AVRational 只有 .num / .den
        num = getattr(tb, "num", 0)
        den = getattr(tb, "den", 0)
    try:
        num, den = int(num), int(den)
    except (TypeError, ValueError):
        return None
    if den == 0 or num == 0:
        return None
    return num, den


def ticks_to_ms(ticks: Any, time_base: Any) -> int | None:
    """把指定时基下的刻度（如摄像头原始 pts）换算成毫秒；时基不可用时返回 None。"""
    parts = _tb_parts(time_base)
    if parts is None or ticks is None:
        return None
    num, den = parts
    try:
        return round(float(ticks) * num * 1000.0 / den)
    except (TypeError, ValueError):
        return None


def mask_credentials(url: str) -> str:
    """隐藏 RTSP/HTTP 地址里的口令，供日志与状态接口使用。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc or "@" not in parts.netloc:
        return url
    credentials, _, host = parts.netloc.rpartition("@")
    user = credentials.split(":", 1)[0]
    masked = f"{user}:***@{host}" if user else f"***@{host}"
    return urlunsplit((parts.scheme, masked, parts.path, parts.query, parts.fragment))


#: 任意文本里内嵌的「scheme://user:password@host」，用于擦掉 FFmpeg 报错里的口令。
_CREDENTIALS_IN_URL = re.compile(r"(?i)\b(rtsps?|https?)://([^/\s:@]+):([^/\s@]*)@")


def redact_credentials(text: Any) -> str:
    """屏蔽自由文本中 URL 内嵌的口令。

    FFmpeg/PyAV 的异常消息经常把完整 RTSP 地址原样带出来，而这些消息会进日志、
    也会经 ``/api/v1/cam/stream/status`` 返回给前端 —— 必须先擦干净再落地。
    """
    if text is None:
        return ""
    return _CREDENTIALS_IN_URL.sub(
        lambda match: f"{match.group(1)}://{match.group(2)}:***@", str(text)
    )
