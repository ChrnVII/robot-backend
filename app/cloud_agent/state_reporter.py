"""状态上报：按分组采集，整帧覆盖，版本单调。

状态链路的契约是「允许丢旧值，只保留最新版本，靠 REST 全量对账兜底」。
落到实现上就三件事：

1. **版本单调。** 每次成功采集 +1，云端只接受更大的版本。乱序/重发到了云端
   会被丢弃并计数，而不是让界面刷回旧值。
2. **只发最新一帧。** 走 `AgentClient.publish_state()` 的状态槽：槽里只有一帧，
   新的直接覆盖旧的。状态永远不值得排队——排队只会让页面看到更旧的东西。
3. **采集时间与发送时间分开。** `observed_at` 是采集那一刻，云端另有
   `received_at`。页面用两者之差显示"数据延迟"，弱网下这两个数字差很多。

关于分组频率：方案里写的是 link 5s / robot 1s / camera 10s / media 2s。
这里按 1s 的主循环采集全部组，**发送侧仍是 1s 一帧**。原因不是偷懒——
采集成本几乎为零（一次本机回环 HTTP），而四个定时器各自发一帧会让
"同一时刻的状态"被拆成四份、在云端拼起来还可能有版本空洞。整帧发送、
分组采集，语义上更简单，流量上更省（一帧 ~200 字节）。

`link` 组只报我们真的测得到的量。**不报 rtt 与丢包率**：云端没有回显、
agent 也没有往返采样，编一个 0 上去比留空更糟——页面会以为网络很好。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from .backend import LocalBackend
from .media import MediaManager

logger = logging.getLogger("robot-cloud-agent")

#: 主循环间隔。也是 robot 组的频率。
TICK_SECONDS = 1.0
#: link 组的采集间隔（连接质量变化慢，没必要每秒问一次）。
LINK_EVERY = 5.0
#: camera 组的采集间隔。
CAMERA_EVERY = 10.0


class StateReporter:
    """把"本机现在是什么样"变成一帧 `state`。"""

    def __init__(
        self,
        backend: LocalBackend,
        media: MediaManager,
        publish: Any,
        *,
        client_connected: Any = None,
        outbox: Any = None,
    ) -> None:
        self.backend = backend
        self.media = media
        self._publish = publish
        #: 事件 Outbox。用它算"还有多少条没补传上去"——只有机器人自己知道。
        self.outbox = outbox
        self._connected = client_connected or (lambda: True)
        self.version = 0
        self.last_sent_at: float | None = None
        self.last_error: str = ""
        self.sent = 0
        self.failures = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.started_at = time.time()
        #: 上一次采集到的值，用于判断"变化时才发"（camera 组）。
        self._last_camera: dict[str, Any] = {}
        self._last_camera_at = 0.0
        self._last_link: dict[str, Any] = {}
        self._last_link_at = 0.0

    # --- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="state-reporter")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def status(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "sent": self.sent,
            "failures": self.failures,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
        }

    # --- 主循环 ---------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 上报失败不能拖垮连接
                self.failures += 1
                self.last_error = str(exc)
                logger.debug("状态采集失败：%s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)

    async def _tick(self) -> None:
        if not self._connected():
            # 没连上就只更新本地缓存，不攒版本：连上后第一帧应该是"现在"，
            # 而不是一串补发的历史。
            return
        payload = await self.build()
        self._publish(payload)
        self.sent += 1
        self.last_sent_at = time.time()

    # --- 采集 -----------------------------------------------------------

    async def build(self) -> dict[str, Any]:
        """采一帧。任何一个组拿不到就不带这个键，绝不填假值。"""
        now = time.time()
        payload: dict[str, Any] = {}

        try:
            health = await self.backend.robot_state()
        except Exception as exc:  # noqa: BLE001 - 后端暂时不可用是常态
            self.last_error = str(exc)
            health = {}

        robot = _robot_group(health)
        if robot:
            payload["robot"] = robot

        camera = self._camera_group(health, now)
        if camera:
            payload["camera"] = camera

        lag = None
        if self.outbox is not None:
            try:
                lag = int((await self.outbox.stats()).get("lag") or 0)
            except Exception as exc:  # noqa: BLE001 - 事件库临时不可读就不报这个数
                logger.debug("读取事件积压失败：%s", exc)
        link = self._link_group(now, lag)
        if link:
            payload["link"] = link

        media = self._media_group()
        if media:
            payload["media"] = media

        # 什么都没有（后端挂了、也没媒体会话）：不推进版本，
        # 否则"版本在涨、内容全空"会让页面以为数据很新。
        if not payload:
            raise RuntimeError("没有任何可上报的状态组")

        self.version += 1
        return {"state_version": self.version, "observed_at": now, **payload}

    def _camera_group(self, health: dict[str, Any], now: float) -> dict[str, Any] | None:
        """相机在线与否。变化时才重采（10s 兜底），减少对 robot-core 的打扰。"""
        if now - self._last_camera_at < CAMERA_EVERY and self._last_camera:
            return self._last_camera
        devices = health.get("devices") if isinstance(health, dict) else None
        camera_state = devices.get("camera") if isinstance(devices, dict) else None
        online: bool | None = None
        if isinstance(camera_state, dict):
            health_value = str(camera_state.get("health") or "").lower()
            # robot-core 的 HealthState：ok / degraded / unavailable
            online = health_value in ("ok", "degraded")
        if online is None:
            # 拿不到就不带 camera 组：页面显示"—"，而不是一个乐观的 true。
            return self._last_camera or None
        group = {"online": online}
        self._last_camera = group
        self._last_camera_at = now
        return group

    def _link_group(self, now: float, lag: int | None = None) -> dict[str, Any] | None:
        if lag is not None:
            # 补传落后量变化比 5s 快得多，必须立刻带上，不能被 link 的缓存挡住。
            self._last_link = {**self._last_link, "event_lag_seq": lag}
            self._last_link_at = 0.0
        if now - self._last_link_at < LINK_EVERY and self._last_link:
            return self._last_link
        sessions = self.media.snapshot().get("sessions") or []
        uplink = sum(int(item["stats"].get("bitrate_kbps") or 0) for item in sessions)
        reconnects = sum(int(item["stats"].get("reconnects") or 0) for item in sessions)
        group = {
            "uplink_kbps": uplink,
            "media_reconnects": reconnects,
            "control_uptime_s": round(now - self.started_at, 1),
        }
        if lag is not None:
            group["event_lag_seq"] = lag
        if lag is not None:
            group["event_lag_seq"] = lag
        self._last_link = group
        self._last_link_at = now
        return group

    def _media_group(self) -> dict[str, Any] | None:
        """只有真的在推流时才报：没有会话时这个组毫无意义。"""
        sessions = self.media.snapshot().get("sessions") or []
        if not sessions:
            return None
        stats = sessions[0]["stats"]
        return {
            "preview": "streaming" if stats.get("frames") else "starting",
            "bitrate_kbps": stats.get("bitrate_kbps"),
            "fps": stats.get("fps"),
            "sessions": len(sessions),
        }


def _robot_group(health: dict[str, Any]) -> dict[str, Any] | None:
    """从 robot-core 的 health 里挑出页面要的两个数。

    `mode` 用 robot-core 的顶层 state；`battery` 从 battery 设备状态里取，
    取不到就不带——旧机型没有电池设备，填 0 会让页面显示"电量耗尽"。
    """
    if not isinstance(health, dict) or not health:
        return None
    group: dict[str, Any] = {}
    state = health.get("state")
    if isinstance(state, str) and state:
        group["mode"] = state
    devices = health.get("devices")
    battery = _find_battery(devices)
    if battery is not None:
        group["battery"] = battery
    return group or None


def _find_battery(devices: Any) -> int | None:
    if not isinstance(devices, dict):
        return None
    entry = devices.get("battery")
    if not isinstance(entry, dict):
        return None
    values = entry.get("values") if isinstance(entry.get("values"), dict) else entry
    for key in ("percent", "battery", "level", "soc"):
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(round(float(value)))
    return None
