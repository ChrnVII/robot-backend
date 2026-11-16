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
from urllib.parse import urlencode, urlsplit

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
    """媒体会话建立失败。code 直接映射到 ErrorCode。

    `retry` 决定这条故障是不是"等一会儿就会好"。这个区分不是洁癖：
    会话已经出过图之后，本机 backend 重启（`backend_unreachable`）必须继续
    重连，否则一次后端滚动重启就把云端画面永久打死；而"本机没开低延迟预览"
    （`stream_disabled`）重连一万次也不会变好。混成一类，两边都会错。
    """

    def __init__(self, code: str, message: str, *, retry: bool = False) -> None:
        self.code = code
        self.message = message
        self.retry = retry
        super().__init__(f"{code}: {message}")


#: 这些故障等一会儿会自己好：本机后端重启、端口暂时没起。
RETRYABLE_CODES = frozenset({"backend_unreachable"})

#: 云端**主动**结束会话时的关闭码。
#:
#: 1000 = 正常关闭（空闲回收 / 管理员终止 / 服务重启），4403 = 这条会话不属于
#: 本机，4404 = 会话已被云端回收。这三种重连一万次也一样，必须收敛——否则每关
#: 一个会话就在机器人上留一个永久重试的僵尸任务，日志里只看得到 4404。
#: 反过来说，1001/1006/1012 这类是链路断了，属于可重连。
TERMINAL_CLOSE_CODES = frozenset({1000, 4403, 4404})


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

    def snapshot(self, session_id: str, profile: str) -> dict[str, Any]:
        """当前统计。

        `profile` 必须由调用方传入：档位是可变的（`ProfileGate.set_profile`），
        而统计对象只负责计数，不持有档位。以前这里读 `self.gate.profile`，
        属性根本不存在，于是每 2s 抛一次 AttributeError——既让 `media_stats`
        一帧也发不出去，又通过 `MediaManager.snapshot()` 把状态链路一起打挂
        （见 state_reporter 的 link/media 组）。
        """
        return {
            "session_id": session_id,
            "bitrate_kbps": self.bitrate_kbps,
            "fps": self.fps,
            "frames": self.frames,
            "keyframes": self.keyframes,
            "dropped": 0,
            "skipped_frames": self.skipped_frames,
            "profile": profile,
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
        #: 上行的写锁。视频帧与统计帧来自两个协程，虽然 websockets 内部也有锁，
        #: 但那属于库的实现细节；这里显式串行，保证顺序可预期（帧不会插进
        #: 一条文本消息的两半里，也不依赖库版本的行为）。
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._cloud: Any = None
        self._ready = asyncio.Event()
        self._failure: MediaError | None = None
        #: 云端明确要求结束（`media_control stop`）：置位后不再重连。
        self._terminal = False

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

    def set_profile(self, profile: str) -> bool:
        """换档。返回**是否真的变了**（非法值/同档返回 False）。

        返回 bool 而不是新档位：调用方只关心"要不要重发 config"。返回档位
        字符串时任何非空值都为真，等于每次都重发，反而分不清是否生效。
        """
        if not self.gate.set_profile(profile):
            return False
        self.profile = self.gate.profile
        logger.info("会话 %s 切档：%s（每 %d 段 GOP 保留 1 段）",
                    self.session_id, self.profile, self.gate.stride)
        return True

    # --- 上行 -----------------------------------------------------------

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _cloud_url(self) -> str:
        return f"{self.config.ws_url}/media"

    def _local_url(self) -> str:
        # 用 urlsplit 取 netloc，而不是字符串切分：backend_url 若被写成
        # `http://host:8100/api` 这类带前缀的形式，切分出来的 host 会把 path
        # 一起带上，拼成 `/api/ws/camera/preview`，永远连不上。
        # 预览通道是根级路径，前缀一律丢弃。
        parts = urlsplit(self.config.backend_url.strip())
        scheme = "wss" if parts.scheme == "https" else "ws"
        netloc = parts.netloc or "127.0.0.1:8100"
        # 空的 cameraId 表示"本地那台球形相机"：本地路由对空串走缺省分支，
        # 传 "default" 反而会因为查不到这个编号而被拒（unknown_camera）。
        query: dict[str, str] = {}
        if self.camera_id:
            query["cameraId"] = self.camera_id
        token = self._local_token()
        if token:
            query["accessToken"] = token
        suffix = f"?{urlencode(query)}" if query else ""
        return f"{scheme}://{netloc}{LOCAL_PREVIEW_PATH}{suffix}"

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
                if not self._ready.is_set() or not exc.retry:
                    # 会话还没起步就失败，或者属于"重试一万次也一样"（未启用预览 /
                    # 会话未知 / 凭据无效）：置 stop 让 `alive` 变假，并把错误交回
                    # `start()`，云端据此给页面一个具体原因而不是干等超时。
                    self._failure = exc
                    self._stop.set()
                    self._ready.set()
                    logger.warning("媒体会话 %s 建立失败：%s", self.session_id, exc.message)
                    return
                # 已经出过图之后的"可恢复"故障（本机 backend 重启之类）：
                # 继续走下面的退避重连，否则一次滚动重启就把画面永久打死。
                self.stats.last_error = f"{exc.code}: {exc.message}"
                logger.info("媒体会话 %s 暂时不可用，稍后重连：%s", self.session_id, exc.message)
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
        """连上云端与本地各一条，然后**互为收尾**地搬运数据。

        这里不能用 `gather`：gather 要两条都结束才返回。本地预览先断（相机
        RTSP 掉了、backend 重启）时，本地那条已经结束而云端那条还在等消息，
        gather 会一直挂着——表现为"设备在线、上游连着、一帧都没有"，页面永远
        停在"画面停滞"，而且机器人再也不会自己恢复。所以用 wait(FIRST_COMPLETED)
        并在任一条结束时取消另一条，再由 `_run_upstream` 决定重连还是收敛。
        """
        cloud = await self._open_cloud()
        try:
            local = await self._open_local()
        except Exception as exc:
            await _close_quietly(cloud)
            url = self._local_url()
            # 一定把实际 URL 打出来：这类故障（端口错、协议错、指到了别的服务）
            # 只有看到"连的是哪个地址"才排得下去。
            logger.warning("本机预览连接失败 url=%s：%s", url, exc)
            raise MediaError(
                "backend_unreachable",
                f"本机预览通道不可用（{url}）：{exc}",
                retry=True,
            ) from exc

        self._cloud = cloud
        pumps = {
            asyncio.create_task(self._pump_local(local, cloud), name="media-local"),
            asyncio.create_task(self._pump_cloud(cloud), name="media-cloud"),
        }
        try:
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            # 关闭码必须在 finally 之前读：我们自己 `close()` 也会把它写成 1000，
            # 那会把"本地预览断了、我主动收掉云端这条"误判成"云端结束了会话"。
            code = getattr(cloud, "close_code", None)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                # 先完成的那条如果抛了，原样上抛给 `_run_upstream` 分类处理。
                task.result()
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            self._cloud = None
            await _close_quietly(local)
            await _close_quietly(cloud)

        if self._terminal or code in TERMINAL_CLOSE_CODES:
            # 云端已经把这条会话结束了：不再重连。重连只会得到 4404，
            # 并且每关一个会话就在机器人上留一个永久重试的任务。
            self._stop.set()
            logger.info("媒体会话 %s 由云端结束（code=%s）", self.session_id, code)
            return
        # 走到这里说明是本机这条腿断了（预览通道关闭 / 相机 RTSP 断），
        # 云端会话还在：重连，别把会话本身判死。
        raise MediaError(
            "backend_unreachable", "本机预览通道已断开，准备重连", retry=True
        )

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

    async def _send(self, cloud: Any, payload: Any) -> None:
        async with self._send_lock:
            await cloud.send(payload)

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
            await self._send(cloud, frame)

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
            await self._send(
                cloud, Message(type=MessageType.MEDIA_CONFIG, payload=payload).to_json()
            )
            logger.info(
                "媒体会话 %s 编码：%s %s", self.session_id, payload["codec"], payload["size"]
            )
            return
        if kind == "error":
            code = str(message.get("code") or "stream_disabled")
            reason = str(message.get("reason") or message.get("message") or "")
            raise MediaError(
                code,
                reason or "本机预览通道不可用",
                # 本机后端重启期间预览通道也是不可达的，这属于"等一会儿就好"；
                # 而 stream_disabled / unknown_camera 是配置问题，重试无意义。
                retry=code in RETRYABLE_CODES,
            )
        # det / record / stop 等本地消息与云端无关。
        # `stop` 表示本地推流服务要重启：不在这里解释，让 socket 自己关，
        # 由 `_connect_once` 的重连路径接住。

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
                    if self.set_profile(str(message.payload.get("profile") or "")):
                        # 档位变了就必须重发 config：页面用 config 里的 profile
                        # 显示"当前档位"，不发就永远显示旧档，自动降级在页面上
                        # 看起来像没生效。
                        await self._send_config(cloud)
                elif action == "stop":
                    # 云端明确结束：置终态，外层据此收敛，不再重连。
                    self._terminal = True
                    logger.info("云端要求结束会话 %s", self.session_id)
                    return
            elif message.type is MessageType.ERROR:
                detail = message.error
                logger.warning("媒体链路上的服务端错误：%s", detail.message if detail else "")

    async def _send_config(self, cloud: Any) -> None:
        """把当前 codec/时间轴/档位重发一遍。"""
        if not self.config_frame:
            return
        payload = {**self.config_frame, "profile": self.profile}
        self.config_frame = payload
        await self._send(
            cloud, Message(type=MessageType.MEDIA_CONFIG, payload=payload).to_json()
        )

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(STATS_INTERVAL)
            cloud = self._cloud
            if cloud is None:
                continue
            self.stats.sample()
            payload = self.stats.snapshot(self.session_id, self.profile)
            with contextlib.suppress(Exception):
                await self._send(
                    cloud, Message(type=MessageType.MEDIA_STATS, payload=payload).to_json()
                )


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
        session = MediaSession(self.config, session_id, camera_id or "", profile or "low")
        self._sessions[session_id] = session
        try:
            return await session.start()
        except Exception:
            self._sessions.pop(session_id, None)
            raise

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
                    "stats": session.stats.snapshot(session.session_id, session.profile),
                }
                for session in self._sessions.values()
            ]
        }
