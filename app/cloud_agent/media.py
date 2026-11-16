"""媒体上行：把本机低延迟预览的一路码流转到云端。

拓扑上只有**一条**上行连接（方案 §3.3 的 C1）。观众从 1 变 5 时，云端扇出，
机器人这边的出口带宽不变——这是"一路 RTSP 一条连接"的延伸，不是新机制。

数据来源是 backend 的 `/ws/camera/preview`：那是已经在跑的本地预览通道
（PyAV 解封装 + Annex-B 透传），格式与云端要的一模一样，所以这里**只做搬运**：

    本地二进制帧  flags(1)+pts(4)+seq(4)+Annex-B   →  原样转发
    本地文本帧    {"t":"config",...}               →  Message(type=media_config)
    本地错误帧    {"t":"error",...}                →  会话失败，如实回错误码

不解释帧内容、不重打包、不转码。`RESET` 位由云端按 `need_keyframe` 打标，
agent 不参与——否则同一套丢帧逻辑要在三个地方各写一遍。

为什么是独立的 WS 而不是复用控制连接：媒体帧会把控制帧堵在后面（TCP 队头），
一次卡顿就是"云台点了没反应"。这也是方案把 C0/C1 分开的直接原因。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed

from .backend import mint_local_access_token
from .config import AgentConfig
from .protocol import (
    MEDIA_FLAG_GOP_START,
    MEDIA_FLAG_KEY,
    MEDIA_HEADER_BYTES,
    Message,
    MessageType,
)

logger = logging.getLogger("robot-cloud-agent")

MEDIA_MAX_BYTES = 8 * 1024 * 1024
CONFIG_TIMEOUT = 6.0
STATS_INTERVAL = 2.0
RECONNECT_MAX = 15.0
LOCAL_PREVIEW_PATH = "/ws/camera/preview"


#: 档位 → 每隔几个 GOP 保留一段。1 表示不抽帧。
#:
#: 为什么按 GOP 抽而不是按帧抽：H.264 的 P 帧依赖参考帧，随机抽帧会让后续帧
#: 引用到"浏览器没收到的那一帧"，表现是花屏直到下一个 IDR。整段 GOP 跳过
#: 才是安全的——跳过的帧浏览器从来没见过，也就不会去引用。
PROFILE_STRIDE = {"high": 1, "low": 2, "tiny": 4}


class ProfileGate:
    """按档位决定"这一段 GOP 发不发"。

    只做两件事：数 GOP、整段保留或整段跳过。跳过之后的第一个保留段会在
    关键帧上打 ``MEDIA_FLAG_GOP_START``，让播放器知道"这里有一段时间的缺口"，
    而不是以为帧序号连续。
    """

    def __init__(self, profile: str = "low") -> None:
        self.profile = profile if profile in PROFILE_STRIDE else "low"
        self._gop = 0
        self._keep = True
        self._skipped = False
        self._jump = False
        self._resume = False

    @property
    def stride(self) -> int:
        return PROFILE_STRIDE[self.profile]

    @property
    def keeping(self) -> bool:
        return self._keep

    def set_profile(self, profile: str) -> bool:
        """换档。返回是否真的变了（变了要从下一个关键帧重新起步）。"""
        if profile not in PROFILE_STRIDE or profile == self.profile:
            return False
        self.profile = profile
        self._resume = True
        return True

    def request_keyframe(self) -> None:
        """下游要求重新出关键帧：下一段 GOP 一定保留。"""
        self._resume = True

    def allow(self, frame: bytes) -> bytes | None:
        """返回要转发的帧（可能被打了 GOP_START 标记），或不发（None）。"""
        if len(frame) <= MEDIA_HEADER_BYTES:
            return None
        if frame[0] & MEDIA_FLAG_KEY:
            self._gop += 1
            # 第一段 GOP 永远保留：新观众需要立刻看到画面。
            self._keep = self._resume or (self._gop - 1) % self.stride == 0
            if self._keep:
                self._jump = self._skipped
                self._skipped = False
            if self._resume:
                self._resume = False
        if not self._keep:
            self._skipped = True
            return None
        if self._jump:
            self._jump = False
            return bytes([frame[0] | MEDIA_FLAG_GOP_START]) + frame[1:]
        return frame


class MediaError(Exception):
    """媒体会话建立失败。code 直接映射到 ErrorCode。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class MediaStats:
    started_at: float = field(default_factory=time.time)
    frames: int = 0
    keyframes: int = 0
    bytes: int = 0
    last_frame_at: float | None = None
    reconnects: int = 0
    skipped_frames: int = 0
    last_error: str = ""
    bitrate_kbps: float = 0.0
    fps: float = 0.0
    _sample_at: float = 0.0
    _sample_bytes: int = 0
    _sample_frames: int = 0

    def sample(self) -> None:
        """按窗口算码率与 fps。只由 2s 定时器调用，`snapshot()` 保持只读。"""
        now = time.time()
        elapsed = max(0.001, now - (self._sample_at or self.started_at))
        self.bitrate_kbps = round(max(0, self.bytes - self._sample_bytes) * 8 / 1000 / elapsed, 1)
        self.fps = round(max(0, self.frames - self._sample_frames) / elapsed, 1)
        self._sample_at = now
        self._sample_bytes = self.bytes
        self._sample_frames = self.frames

    def snapshot(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "bitrate_kbps": self.bitrate_kbps,
            "fps": self.fps,
            "frames": self.frames,
            "keyframes": self.keyframes,
            "dropped": 0,
            "skipped_frames": self.skipped_frames,
            "profile": self.gate.profile,
            "last_frame_at": self.last_frame_at,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


class MediaSession:
    """一个 (camera, session_id) 的上行会话。"""

    def __init__(
        self,
        config: AgentConfig,
        session_id: str,
        camera_id: str,
        profile: str,
    ) -> None:
        self.config = config
        self.session_id = session_id
        self.camera_id = camera_id
        self.profile = profile
        self.gate = ProfileGate(profile)
        self.stats = MediaStats()
        self.config_frame: dict[str, Any] = {}
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._cloud: Any = None
        self._ready = asyncio.Event()
        self._failure: MediaError | None = None

    # --- 生命周期 -------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self._stop.is_set()

    async def start(self) -> dict[str, Any]:
        """建立上行并等到第一条 config（或失败）。返回 {codec, size, profile}。"""
        self._spawn(self._run_upstream(), "media-upstream")
        self._spawn(self._stats_loop(), "media-stats")
        waiter = asyncio.create_task(self._ready.wait())
        done, _ = await asyncio.wait({waiter}, timeout=CONFIG_TIMEOUT)
        waiter.cancel()
        if self._failure is not None:
            await self.stop("handshake_failed")
            raise self._failure
        if not done or not self.config_frame:
            # 连上了但没等到 config：可能相机慢，也可能上游没起。
            # 不立刻失败——帧可能马上就来，云端靠 last_frame_at 判断新鲜度。
            logger.warning("媒体会话 %s 在 %.0fs 内没有收到 config", self.session_id, CONFIG_TIMEOUT)
        return {
            "codec": self.config_frame.get("codec"),
            "size": self.config_frame.get("size") or [0, 0],
            "profile": self.config_frame.get("profile") or self.profile,
        }

    async def stop(self, reason: str = "stopped") -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("媒体会话 %s 结束（%s），共 %d 帧", self.session_id, reason, self.stats.frames)

    def request_keyframe(self) -> None:
        """云端要求重新出关键帧。

        本地预览通道是单向的（server → client），没法命令相机立刻出 IDR，
        所以真正的恢复靠 GOP（1s 内必有下一个关键帧）。这里能做的、也确实
        有用的一件事：**别再跳过下一段 GOP**——如果正好处在抽帧档位，
        请求之后立刻恢复满帧率，否则用户要等 {stride} 个 GOP 才看到画面。
        """
        self.gate.request_keyframe()
        logger.debug("会话 %s 请求关键帧：下一段 GOP 不再跳过", self.session_id)

    def set_profile(self, profile: str) -> str:
        """换档。返回实际生效的档位（非法值回落到当前档）。"""
        if self.gate.set_profile(profile):
            self.profile = self.gate.profile
            logger.info("会话 %s 切档：%s（每 %d 段 GOP 保留 1 段）",
                        self.session_id, self.profile, self.gate.stride)
        return self.profile

    # --- 上行 -----------------------------------------------------------

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _cloud_url(self) -> str:
        return f"{self.config.ws_url}/media"

    def _local_url(self) -> str:
        parts = self.config.backend_url.rstrip("/")
        scheme = "wss" if parts.startswith("https://") else "ws"
        host = parts.split("://", 1)[-1]
        query = {"cameraId": self.camera_id}
        token = self._local_token()
        if token:
            query["accessToken"] = token
        return f"{scheme}://{host}{LOCAL_PREVIEW_PATH}?{urlencode(query)}"

    def _local_token(self) -> str:
        """本机 backend 开了鉴权时需要它。取不到就让后端回 1008 —— 报错要具体。"""
        if self.config.backend_token:
            return self.config.backend_token
        return mint_local_access_token()

    async def _run_upstream(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except MediaError as exc:
                self._failure = exc
                # 放弃重试：这种错误（未启用预览 / 会话未知）重试一万次也一样。
                # 置 stop 让 `alive` 变假，下次 start 会建一个干净会话，而不是
                # 复用一个已经死掉、config 还停留在旧值的壳。
                self._stop.set()
                self._ready.set()
                logger.warning("媒体会话 %s 建立失败：%s", self.session_id, exc.message)
                return
            except ConnectionClosed as exc:
                self.stats.last_error = f"connection closed: {exc}"
                logger.info("媒体会话 %s 断开：%s", self.session_id, exc)
            except Exception as exc:  # noqa: BLE001 - 任何异常都走重连
                self.stats.last_error = str(exc)
                logger.warning("媒体会话 %s 异常：%s", self.session_id, exc)

            if self._stop.is_set():
                return
            if time.monotonic() - started > 10.0:
                backoff = 1.0
            self.stats.reconnects += 1
            delay = min(backoff, RECONNECT_MAX) * (1 + 0.25 * (self.stats.reconnects % 3))
            await self._sleep(delay)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _sleep(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _connect_once(self) -> None:
        cloud = await self._open_cloud()
        try:
            local = await self._open_local()
        except Exception as exc:
            await _close_quietly(cloud)
            raise MediaError("stream_disabled", f"本机预览通道不可用：{exc}") from exc

        self._cloud = cloud
        try:
            await asyncio.gather(
                self._pump_local(local, cloud),
                self._pump_cloud(cloud),
            )
        finally:
            self._cloud = None
            await _close_quietly(local)
            await _close_quietly(cloud)

    async def _open_cloud(self) -> Any:
        token = self.config.token
        if not token:
            raise MediaError("unauthorized", "还没有云端凭据，无法建立媒体上行")
        query = {
            "session_id": self.session_id,
            "camera_id": self.camera_id,
            "profile": self.profile,
        }
        url = f"{self._cloud_url()}?{urlencode(query)}"
        return await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=MEDIA_MAX_BYTES,
            open_timeout=self.config.open_timeout,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        )

    async def _open_local(self) -> Any:
        return await websockets.connect(
            self._local_url(),
            max_size=MEDIA_MAX_BYTES,
            open_timeout=5,
            close_timeout=3,
            ping_interval=None,   # 本地回环，不需要保活
        )

    async def _pump_local(self, local: Any, cloud: Any) -> None:
        async for raw in local:
            if self._stop.is_set():
                return
            if isinstance(raw, str):
                await self._on_local_text(raw, cloud)
                continue
            if len(raw) > MEDIA_HEADER_BYTES and raw[0] & MEDIA_FLAG_KEY:
                self.stats.keyframes += 1
            frame = self.gate.allow(raw)
            if frame is None:
                # 整段 GOP 被跳过：不入统计、不上行。观众看到的是"低帧率"，
                # 而不是"缺了几帧的花屏"。
                self.stats.skipped_frames += 1
                continue
            self.stats.frames += 1
            self.stats.bytes += len(frame)
            self.stats.last_frame_at = time.time()
            await cloud.send(frame)

    async def _on_local_text(self, raw: str, cloud: Any) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = message.get("t")
        if kind == "config":
            # 本地字段名与云端 payload 保持一致（都叫 codec/t0/size），
            # 额外带上 profile，让云端能给页面显示"当前档位"。
            payload = {
                "session_id": self.session_id,
                "codec": message.get("codec"),
                "t0": message.get("t0"),
                "size": message.get("size") or [0, 0],
                "profile": self.profile,
            }
            self.config_frame = payload
            self._ready.set()
            await cloud.send(
                Message(type=MessageType.MEDIA_CONFIG, payload=payload).to_json()
            )
            logger.info(
                "媒体会话 %s 编码：%s %s", self.session_id, payload["codec"], payload["size"]
            )
            return
        if kind == "error":
            code = str(message.get("code") or "stream_disabled")
            reason = str(message.get("reason") or message.get("message") or "")
            raise MediaError(code, reason or "本机预览通道不可用")
        # det / record / stop 等本地消息与云端无关，丢掉。

    async def _pump_cloud(self, cloud: Any) -> None:
        async for raw in cloud:
            if isinstance(raw, bytes):
                continue      # 云端不往回发媒体帧
            try:
                message = Message.parse(raw)
            except Exception:  # noqa: BLE001 - 坏帧不该打断上行
                logger.debug("媒体上行收到无法解析的帧")
                continue
            if message.type is MessageType.MEDIA_CONTROL:
                action = message.payload.get("action")
                if action == "keyframe":
                    self.request_keyframe()
                elif action == "profile":
                    self.set_profile(str(message.payload.get("profile") or ""))
                elif action == "stop":
                    logger.info("云端要求结束会话 %s", self.session_id)
                    return
            elif message.type is MessageType.ERROR:
                detail = message.error
                logger.warning("媒体链路上的服务端错误：%s", detail.message if detail else "")

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(STATS_INTERVAL)
            cloud = self._cloud
            if cloud is None:
                continue
            self.stats.sample()
            payload = self.stats.snapshot(self.session_id)
            with contextlib.suppress(Exception):
                await cloud.send(Message(type=MessageType.MEDIA_STATS, payload=payload).to_json())


async def _close_quietly(ws: Any) -> None:
    with contextlib.suppress(Exception):
        await ws.close()


class MediaManager:
    """会话表。一个 agent 上可以同时有多路（一期只有球形一路）。"""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._sessions: dict[str, MediaSession] = {}

    async def start(self, session_id: str, camera_id: str, profile: str) -> dict[str, Any]:
        existing = self._sessions.get(session_id)
        if existing is not None and existing.alive:
            # 幂等：云端重发 start 不该重建上行（重建会让画面黑一下）
            return {
                "codec": existing.config_frame.get("codec"),
                "size": existing.config_frame.get("size") or [0, 0],
                "profile": existing.profile,
                "reused": True,
            }
        session = MediaSession(self.config, session_id, camera_id or "default", profile or "low")
        self._sessions[session_id] = session
        try:
            return await session.start()
        except Exception:
            self._sessions.pop(session_id, None)
            raise

    def set_profile(self, session_id: str, profile: str) -> str | None:
        """换档。会话不在就返回 None（调用方据此报 stream_disabled）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return session.set_profile(profile)

    def set_profile(self, session_id: str, profile: str) -> str | None:
        """换档。会话不在就返回 None（调用方据此报 stream_disabled）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return session.set_profile(profile)

    async def stop(self, session_id: str, reason: str = "stopped") -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        await session.stop(reason)
        return True

    async def stop_all(self) -> None:
        for session_id in list(self._sessions):
            await self.stop(session_id, "agent_stopped")

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "session_id": session.session_id,
                    "camera_id": session.camera_id,
                    "profile": session.profile,
                    "stride": session.gate.stride,
                    "stats": session.stats.snapshot(session.session_id),
                }
                for session in self._sessions.values()
            ]
        }
