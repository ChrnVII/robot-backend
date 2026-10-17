"""RTSP 拉流线程：一条连接、全程不解码转码，只解封装 +（可选）解码喂检测。

数据流（单读者线程，同一份码流零拷贝扇出）::

    rtsp://.../102  ──PyAV 低延迟 demux──┬─► Annex-B 透传 ──► WebSocket ──► 浏览器 WebCodecs
                                        └─► decode ──► 最新帧槽位 ──► 检测线程

录像按需将同一份原始码流转封装为 MKV，独立写盘队列不阻塞预览。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.video.codec import (
    FLAG_KEY,
    FLAG_RST,
    HEVC_CODEC_FALLBACK,
    HEVC_CRA,
    HEVC_IDR_N_LP,
    HEVC_IDR_W_RADL,
    HEVC_PPS,
    HEVC_SPS,
    HEVC_VPS,
    NAL_IDR,
    NAL_PPS,
    NAL_SPS,
    Timeline,
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
)
from app.video.pipeline import CameraBroadcaster, FrameSlot

logger = logging.getLogger(__name__)


def _set_low_delay(av_module: Any, stream: Any) -> None:
    """给解码器打开 low_delay（B 帧重排序缓冲区归零）。

    PyAV 各版本对这个枚举成员的命名/路径都不一致（实测 15.1.0 里是
    ``av.codec.context.Flags.low_delay``，而更早的版本写作 ``LOW_DELAY``），
    因此多路径 + 两种大小写都试一遍，找不到就安静跳过 —— 这不是致命项，
    FFmpeg 在无 B 帧的流上本来也不会重排序。
    """
    low = None
    for path in ("codec.context.Flags", "codec.Flags", "codec.context.CodecFlags"):
        obj: Any = av_module
        for part in path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if obj is None:
            continue
        low = getattr(obj, "LOW_DELAY", None) or getattr(obj, "low_delay", None)
        if low is not None:
            break
    if low is None:
        logger.debug("当前 PyAV 未暴露 low_delay 标志，跳过解码器低延迟设置")
        return
    try:
        context = stream.codec_context
        context.flags = context.flags | low
    except Exception:  # noqa: BLE001 - 个别精简构建没有该属性
        logger.debug("设置解码器 low_delay 失败", exc_info=True)


class RtspStreamReader(threading.Thread):
    """唯一的一条 RTSP 连接；断线自动重连，线程顶层兜住所有异常。"""

    def __init__(
        self,
        url: str,
        broadcaster: CameraBroadcaster,
        *,
        stop_event: threading.Event,
        slot: FrameSlot | None = None,
        recorder=None,
        transport: str = "tcp",
        timeout_us: int = 5_000_000,
        reconnect_delay: float = 2.0,
    ) -> None:
        super().__init__(name="camera-stream-reader", daemon=True)
        self.url = url
        self.broadcaster = broadcaster
        self.slot = slot
        self.recorder = recorder
        self._stop_event = stop_event
        self._options = open_options(transport=transport, timeout_us=timeout_us)
        self._io_timeout = max(0.1, timeout_us / 1_000_000)
        self.reconnect_delay = max(0.2, float(reconnect_delay))

        self.timeline = Timeline()
        self.fatal: str = ""
        self.last_error: str = ""
        self.connected = False
        self.sessions = 0
        self.packets_rx = 0
        self.keyframes_rx = 0
        self.decode_errors = 0

        self._av: Any = None
        self._error_cls: type[BaseException] = Exception
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._vps: bytes | None = None
        self._codec: str | None = None
        self._is_hevc = False
        self._seq = 0

    # ---------- PyAV 惰性加载 ---------- #

    def _load_av(self) -> Any:
        """惰性导入 PyAV：未安装时视频链路整体降级，但不影响后端其余功能。"""
        try:
            import av  # noqa: PLC0415 - 惰性导入，避免把 av 变成硬依赖
        except ImportError:
            self.fatal = "pyav_missing"
            logger.error("未安装 PyAV（pip install av），球形摄像头低延迟预览已禁用")
            return None
        self._av = av
        # PyAV >= 14 删除了 av.AVError，改名 av.error.FFmpegError
        self._error_cls = getattr(getattr(av, "error", None), "FFmpegError", None) or getattr(
            av, "AVError", Exception
        )
        return av

    # ---------- 参数集 ---------- #

    def _absorb(self, nals: list[bytes]) -> None:
        """截获并缓存 VPS/SPS/PPS，同时推导前端要用的 codec 字符串。"""
        if self._is_hevc:
            for nal in nals:
                kind = hevc_nal_type(nal)
                if kind == HEVC_VPS:
                    self._vps = nal
                elif kind == HEVC_SPS and nal != self._sps:
                    self._sps = nal
                    codec = hevc_codec_string(nal)
                    if codec:
                        self._codec = codec
                        self.broadcaster.set_codec(codec)
                elif kind == HEVC_PPS:
                    self._pps = nal
            return
        for nal in nals:
            kind = h264_nal_type(nal)
            if kind == NAL_PPS:
                self._pps = nal
            elif kind == NAL_SPS and nal != self._sps:
                self._sps = nal
                codec = h264_codec_string(nal)
                if codec:
                    self._codec = codec
                    self.broadcaster.set_codec(codec)

    def _params(self) -> bytes:
        if self._is_hevc:
            return annexb(*(nal for nal in (self._vps, self._sps, self._pps) if nal))
        return annexb(*(nal for nal in (self._sps, self._pps) if nal))

    def _is_key_nal(self, nal: bytes) -> bool:
        if self._is_hevc:
            return hevc_nal_type(nal) in (HEVC_IDR_W_RADL, HEVC_IDR_N_LP, HEVC_CRA)
        return h264_nal_type(nal) == NAL_IDR

    def _is_param_nal(self, nal: bytes) -> bool:
        if self._is_hevc:
            return hevc_nal_type(nal) in (HEVC_VPS, HEVC_SPS, HEVC_PPS)
        return h264_nal_type(nal) in (NAL_SPS, NAL_PPS)

    # ---------- 主循环 ---------- #

    def run(self) -> None:
        av = self._load_av()
        if av is None:
            return
        logger.info("摄像头预览拉流线程启动：%s", mask_credentials(self.url))
        while not self._stop_event.is_set():
            try:
                self._session(av)
            except self._error_cls as exc:  # noqa: PERF203 - 按类型分流日志级别
                self.connected = False
                if self._stop_event.is_set():
                    break
                # FFmpeg 的异常文本会带上完整 RTSP 地址，必须先擦掉口令再落地
                self.last_error = redact_credentials(exc)
                logger.warning("RTSP 断开（%s），%.1fs 后重连", self.last_error, self.reconnect_delay)
                self._stop_event.wait(self.reconnect_delay)
            except Exception as exc:  # noqa: BLE001 - 线程顶层必须兜住
                self.connected = False
                if self._stop_event.is_set():
                    break
                # 漏出去会让线程静默死掉，之后再也不重连
                message = redact_credentials(exc)
                self.last_error = message
                if message != str(exc):
                    # 消息里带口令：只打擦干净的文本，不能带原始 traceback
                    logger.error("拉流线程异常（%s），%.1fs 后重连", message, self.reconnect_delay)
                else:
                    logger.exception("拉流线程异常，%.1fs 后重连", self.reconnect_delay)
                self._stop_event.wait(self.reconnect_delay)
        logger.info("摄像头预览拉流线程退出")

    def _session(self, av: Any) -> None:
        # PyAV 的 options 值统一给字符串，避免不同版本对类型校验不一致
        self._sps = self._pps = self._vps = self._codec = None
        with av.open(self.url, options=self._options, timeout=(self._io_timeout, self._io_timeout)) as container:
            if not container.streams.video:
                raise RuntimeError("该 RTSP 地址里没有视频流")
            stream = container.streams.video[0]

            # FRAME 级多线程解码会缓冲 thread_count 帧，是隐藏的延迟来源；
            # SLICE 级并行没有这个代价，画面越大收益越明显。
            try:
                stream.thread_type = "SLICE"
            except Exception:  # noqa: BLE001 - 个别编码器不支持
                pass
            _set_low_delay(av, stream)

            codec_name = str(getattr(stream.codec_context, "name", "") or "").lower()
            self._is_hevc = codec_name in ("hevc", "h265", "h.265")
            if not self._is_hevc and codec_name and codec_name not in ("h264", "h.264"):
                logger.warning(
                    "非 H.264/H.265 编码（%s），浏览器 WebCodecs 很可能解不出来", codec_name
                )

            width = int(stream.codec_context.width or 0)
            height = int(stream.codec_context.height or 0)
            self.broadcaster.set_size((width, height))
            self.broadcaster.bind_timeline(self.timeline.t0_epoch_ms)

            # RTSP 的 extradata 来自 SDP，开流即可拿到参数集，不必等首个关键帧
            extradata = stream.codec_context.extradata
            if extradata:
                self._absorb(split_annexb(extradata))
            if self._codec:
                self.broadcaster.set_codec(self._codec)
            elif self._is_hevc:
                # 解析失败时的兜底，提示见日志
                self.broadcaster.set_codec(HEVC_CODEC_FALLBACK)
                logger.warning(
                    "无法从 SPS 推导 HEVC codec 字符串，已用兜底值；"
                    "最稳的做法是把子码流编码改成 H.264"
                )

            in_tb = stream.time_base
            self.sessions += 1
            self.last_error = ""
            self.connected = True
            logger.info(
                "摄像头预览就绪：编码=%s codec=%s 分辨率=%dx%d 时基=%s",
                codec_name, self._codec, width, height, in_tb,
            )

            preview_ready = False
            packet_times = {}
            try:
                for packet in container.demux(stream):
                    if self._stop_event.is_set():
                        break
                    # demux 末尾的 flushing 包没有载荷。不能只看 dts is None，
                    # 因为部分 RTSP 设备给的 dts 本来就是空的。
                    if not packet.size:
                        continue
                    try:
                        raw = bytes(packet)
                    except Exception:  # noqa: BLE001 - 个别损坏包会读取失败
                        logger.debug("读取 packet 载荷失败，跳过该包", exc_info=True)
                        continue
                    if not raw:
                        continue

                    if self.recorder is not None:
                        self.recorder.submit(packet, stream)

                    # 实时链路的 pts：这一帧的到达时刻（WebSocket 帧头与此同值）
                    pts_ms = self.timeline.now_ms()
                    self.packets_rx += 1
                    if packet.pts is not None:
                        packet_times[packet.pts] = pts_ms
                        if len(packet_times) > 120:
                            packet_times.pop(next(iter(packet_times)))

                    nals = split_annexb(raw)
                    self._absorb(nals)
                    is_key = bool(packet.is_keyframe) or any(self._is_key_nal(nal) for nal in nals)
                    if is_key:
                        self.keyframes_rx += 1

                    # 关键帧重建成 VPS/SPS/PPS + IDR，保证浏览器任意时刻接入都能解码，
                    # 不必等设备重发参数集。
                    if is_key:
                        body = annexb(*(nal for nal in nals if not self._is_param_nal(nal)))
                        payload = self._params() + body if body else raw
                    else:
                        payload = raw

                    self._seq = (self._seq + 1) & 0xFFFFFFFF
                    flags = FLAG_KEY if is_key else 0
                    if is_key and not preview_ready:
                        flags |= FLAG_RST
                        preview_ready = True
                    if preview_ready:
                        self.broadcaster.broadcast(pack_frame(flags, pts_ms, self._seq) + payload)

                    # 解码交给检测线程用；子码流很便宜，放这里可以不额外占一条 RTSP 连接。
                    if self.slot is not None:
                        try:
                            frames = packet.decode()
                        except self._error_cls:
                            self.decode_errors += 1
                            if self.decode_errors <= 3 or self.decode_errors % 500 == 0:
                                logger.warning(
                                    "解码出错（累计 %d 次），等下一个关键帧恢复", self.decode_errors
                                )
                            frames = []
                        if frames:
                            last = frames[-1]
                            source_pts = last.pts if last.pts is not None else packet.pts
                            self.slot.put(
                                last,
                                packet_times.pop(source_pts, pts_ms),
                                ticks_to_ms(source_pts, last.time_base or in_tb) if source_pts is not None else None,
                            )
            finally:
                if self.recorder is not None:
                    self.recorder.finish()

        self.connected = False
        logger.info(
            "会话结束（收包 %d，关键帧 %d，解码错误 %d）",
            self.packets_rx, self.keyframes_rx, self.decode_errors,
        )

    # ---------- 诊断 ---------- #

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "sessions": self.sessions,
            "packets": self.packets_rx,
            "keyframes": self.keyframes_rx,
            "decodeErrors": self.decode_errors,
            "lastError": self.last_error,
            "fatal": self.fatal,
        }
