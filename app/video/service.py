"""球形摄像头低延迟预览服务：生命周期与状态。

一个进程内只跑**一条** RTSP 连接（子码流），所有浏览器共享它：
服务端不解码转码，只把 Annex-B 码流经 WebSocket 透传，解码在浏览器 WebCodecs 完成。

启用条件（两者都满足才会真正拉流）::

    ROBOT_CAMERA_STREAM_URL=rtsp://<user>:<pass>@<host>:554/Streaming/Channels/102

未配置地址、未安装 PyAV、或显式关闭时，服务静默降级——WS 接口会先发一条
``{"t": "error", "code": "stream_disabled"}`` 再关闭，前端据此回退到 JPEG 轮询。

云台 / 预置点 / 重启**不在本服务范围内**，它们仍走 robot-core 的 ISAPI 驱动。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app.common.config import Settings
from app.video.codec import Timeline, mask_credentials
from app.video.detector import DetectionThread
from app.video.pipeline import CameraBroadcaster, FrameSlot
from app.video.reader import RtspStreamReader

logger = logging.getLogger(__name__)

#: WS 关闭码：服务端主动停止（对齐 api.ws.hub 的「服务重启」语义）。
SERVICE_RESTARTED = 1012


class CameraStreamService:
    """拥有拉流线程、检测线程与广播器；由应用 lifespan 启停。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._stop = threading.Event()
        self._broadcaster = CameraBroadcaster(max_queue=settings.camera_stream_client_queue)
        self._slot: FrameSlot | None = None
        self._reader: RtspStreamReader | None = None
        self._detector: DetectionThread | None = None
        self._started = False
        self._skip_reason = ""

    # ---------- 属性 ---------- #

    @property
    def url(self) -> str:
        return str(self._settings.camera_stream_url or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.camera_stream_enabled and self.url)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def skip_reason(self) -> str:
        return self._skip_reason

    @property
    def broadcaster(self) -> CameraBroadcaster | None:
        """未启用时返回 None——WS 路由据此拒绝连接并提示原因。"""
        return self._broadcaster if self._started else None

    @property
    def timeline(self) -> Timeline | None:
        return self._reader.timeline if self._reader is not None else None

    # ---------- 生命周期 ---------- #

    def start(self, loop: asyncio.AbstractEventLoop) -> bool:
        """启动拉流（幂等）。返回是否真的开始了拉流。"""
        if self._started:
            return True
        if not self._settings.camera_stream_enabled:
            self._skip_reason = "disabled_by_config"
            logger.info("球形摄像头低延迟预览未启用（ROBOT_CAMERA_STREAM_ENABLED=false）")
            return False
        if not self.url:
            self._skip_reason = "url_not_configured"
            logger.info(
                "球形摄像头低延迟预览未启用：未配置 ROBOT_CAMERA_STREAM_URL，"
                "画面接口将回退到既有 JPEG 抓图轮询"
            )
            return False
        if not str(self.url).lower().startswith(("rtsp://", "rtsps://")):
            self._skip_reason = "unsupported_scheme"
            logger.error("ROBOT_CAMERA_STREAM_URL 必须是 rtsp:// 地址，当前：%s",
                         mask_credentials(self.url))
            return False

        self._stop.clear()
        self._broadcaster.attach_loop(loop)

        detect_enabled = bool(self._settings.camera_stream_detect_enabled)
        self._slot = FrameSlot() if detect_enabled else None

        self._reader = RtspStreamReader(
            self.url,
            self._broadcaster,
            stop_event=self._stop,
            slot=self._slot,
            transport=str(self._settings.camera_stream_transport),
            timeout_us=int(self._settings.camera_stream_timeout_us),
            reconnect_delay=float(self._settings.camera_stream_reconnect_delay),
        )
        self._broadcaster.bind_timeline(self._reader.timeline.t0_epoch_ms)

        threads: list[threading.Thread] = [self._reader]
        if detect_enabled and self._slot is not None:
            self._detector = DetectionThread(
                self._slot,
                self._broadcaster,
                stop_event=self._stop,
                model_path=str(self._settings.camera_stream_model or ""),
                imgsz=int(self._settings.camera_stream_imgsz),
                conf=float(self._settings.camera_stream_conf),
                iou=float(self._settings.camera_stream_iou),
                every=int(self._settings.camera_stream_detect_every),
                timeline=self._reader.timeline,
            )
            threads.append(self._detector)

        for thread in threads:
            thread.start()
        self._started = True
        self._skip_reason = ""
        logger.info(
            "球形摄像头低延迟预览已启动：%s（检测=%s）",
            mask_credentials(self.url), "开" if self._slot is not None else "关",
        )
        return True

    def stop(self) -> None:
        if not self._started:
            return
        logger.info("正在停止球形摄像头低延迟预览 …")
        self._stop.set()
        self._broadcaster.close_clients(SERVICE_RESTARTED)
        for thread in (self._detector, self._reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
        self._detector = None
        self._reader = None
        self._slot = None
        self._started = False
        self._broadcaster.detach_loop()
        logger.info("球形摄像头低延迟预览已停止")

    # ---------- 诊断 ---------- #

    def status(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled": bool(self._settings.camera_stream_enabled),
            "started": self._started,
            "reason": self._skip_reason,
            "url": mask_credentials(self.url),
            "transport": str(self._settings.camera_stream_transport),
            "codec": self._broadcaster.codec,
            "size": list(self._broadcaster.size),
            "clients": self._broadcaster.client_count,
            "detectEnabled": self._slot is not None,
        }
        if self._reader is not None:
            payload.update(self._reader.stats())
            payload["framesSkipped"] = self._slot.dropped if self._slot is not None else 0
        if self._detector is not None:
            payload["detector"] = self._detector.stats()
        return payload


_service: CameraStreamService | None = None


def get_camera_stream_service() -> CameraStreamService:
    """进程内单例。配置由环境变量决定，故允许进程内缓存。"""
    global _service
    if _service is None:
        from app.common.config import get_settings  # noqa: PLC0415 - 避免循环导入

        _service = CameraStreamService(get_settings())
    return _service


def reset_camera_stream_service() -> None:
    """丢弃单例（测试用：避免跨用例复用旧配置与线程）。"""
    global _service
    if _service is not None:
        _service.stop()
    _service = None
