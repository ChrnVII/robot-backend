"""控制通道客户端。

三条纪律，对应计划书里提炼的结论：

1. **单写者。** 只有 `_send_loop` 能 `ws.send`。指令回包、心跳、失败通知全部
   先进 `outbox`，由同一个协程按序发出。两个协程并发 send 会交错帧，触发条件
   随机，是最难查的一类 bug。

   状态帧（`state`）**不进 `outbox`**，走单独的“状态槽”：槽里只有最新一帧，
   新的直接覆盖旧的。原因是两者的背压语义完全相反——回包不能丢（丢了就是
   前端一直转圈），状态必须丢（留一堆过期状态毫无意义，还会把回包挤到后面）。
   合成一个队列时，状态洪峰会把命令结果顶掉，这是"点云台没反应"的根源。
   发送优先级固定为：`outbox` → 状态槽。

2. **重连必须有退避 + jitter。** A 重启时所有 B 会在同一秒回来，不打退避就是
   自己 DDoS 自己。连接稳定超过 60s 才重置退避，否则"连上 1 秒就断"会以
   1s 的节奏无限重试。

3. **控制连接断了不等于上传断了。** 上传走 HTTP，是独立的。WS 掉线时不去
   cancel 正在进行的上传——它们多半还能传完。重连后 A 会把未完成的 upload
   再报一遍，本地已经在跑的会被 `start()` 幂等忽略。

能力协商：`hello` 里带 `capabilities` / `media_profiles` / `max_frame_kbps`，
云端据此决定页面上显示哪些按钮。空列表是**明确回答"没有"**，缺字段表示
"老版本，不知道"——两者不同，云端要分开处理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import random
import socket
import time
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from . import __version__
from .backend import LocalBackend
from .config import AgentConfig, resolve_state_dir
from .enroll import rotate
from .errors import AgentCommandError
from .events import EventOutbox, EventUploader
from .handlers import HandlerContext, LeaseKeeper, dispatch
from .media import MediaManager
from .protocol import (
    AGENT_ALLOWED_COMMANDS,
    CommandName,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    Message,
    MessageType,
    ProtocolError,
)
from .sandbox import Sandbox, SandboxError
from .state_reporter import StateReporter
from .uploader import UploadJob, UploadManager

logger = logging.getLogger("robot-cloud-agent")

OUTBOX_SIZE = 128
STABLE_CONNECTION_SECONDS = 60.0
MAX_BACKOFF = 30.0

#: 本构建对外声明具备的能力。
#:
#: 云端据此决定是否显示"高清 / 云台 / 快照"按钮；**声明了却没实现，页面会在
#: 按下去的那一刻才失败**，比不显示按钮更糟。所以这里只列真的做完了的东西：
#:
#:   media.ws.v1     —— /ws/agent/media 上行通道
#:   camera.ptz      —— 云台方向控制（含租约与仲裁）
#:   camera.snapshot —— 快照兜底
#:   state.v1        —— 分组状态上报（P3 接通生产者后才算真具备）
CAPABILITIES: tuple[str, ...] = (
    "media.ws.v1",
    "camera.ptz",
    "camera.snapshot",
    "state.v1",
    "events.batch.v1",
)

#: 需要落本地 Outbox（可靠补传）的事件类型。
#: 心跳之类不进这里：它只对"此刻在线"有意义，补传过去毫无价值。
DURABLE_EVENTS = frozenset({"upload_failed", "upload_completed", "command.result"})

#: 可用的媒体档位。一期只有相机子码流这一档——档位切换（P2）要能与相机
#: 子/主码流、GOP 一起调，声明一堆切不过去的档位只会让页面多出无效按钮。
MEDIA_PROFILES: tuple[str, ...] = ("low",)

#: 上行自述带宽上限（kbps，方案 §8.2）。云端只在挑档位时参考，不是硬约束：
#: 真正的限速由出口链路决定，这里只是把"这台机器大概能推多少"说清楚。
MAX_FRAME_KBPS = 2000


class AgentObserver:
    """连接状态观察者。

    守护进程需要把这些瞬间暴露给本地页面：连接中 / 已连接 / 断开 / 心跳 /
    token 被拒。默认实现全是空操作，client 的单元测试与独立使用都不受影响。
    """

    def on_connecting(self) -> None:
        pass

    def on_connected(self) -> None:
        pass

    def on_disconnected(self, reason: str) -> None:
        pass

    def on_heartbeat(self) -> None:
        pass

    def on_auth_failed(self, message: str) -> None:
        pass


class AgentClient:
    def __init__(
        self, config: AgentConfig, sandbox: Sandbox, observer: AgentObserver | None = None
    ) -> None:
        self.config = config
        self.sandbox = sandbox
        self.observer = observer or AgentObserver()
        self.outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=OUTBOX_SIZE)
        # 状态槽：只保留最新一帧。`_wake` 是发送循环的唤醒信号，两个生产点
        # （outbox 有新内容、状态槽被填）都要 set 一次，否则循环会一直睡着。
        self._state_slot: str | None = None
        self._wake = asyncio.Event()
        #: 状态帧被覆盖掉的次数。用于判断"状态生产得太快"，不是错误。
        self.state_dropped = 0
        self.uploads = UploadManager(sandbox, config, self._send_event)
        # 摄像头这一路：本机 backend 是唯一能碰设备的一方，媒体上行是独立连接。
        # 三者都在这里建一次，随进程生命周期存在。
        self.backend = LocalBackend(config.backend_url, config.backend_token)
        self.media = MediaManager(config)
        self.leases = LeaseKeeper(self.backend)
        # 状态上报与事件 Outbox：一个走 C0 的状态槽，一个走独立 HTTP。
        self.outbox_events = EventOutbox(resolve_state_dir(config) / "events.db")
        self.event_uploader = EventUploader(config, self.outbox_events)
        self._backend_ok = False
        self._last_probe_at = 0.0
        self._backend_ok = False
        self._last_probe_at = 0.0
        self.state_reporter = StateReporter(
            self.backend,
            self.media,
            self.publish_state,
            client_connected=lambda: self._ws is not None,
            outbox=self.outbox_events,
        )
        self.context = HandlerContext(
            sandbox=sandbox,
            uploads=self.uploads,
            backend=self.backend,
            media=self.media,
            leases=self.leases,
            snapshots=self._send_snapshot,
        )
        self._ws: Any = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        # 4401 且轮换失败：说明这条路已经走不通，supervisor 应该停在
        # unauthorized 状态等人工处理，而不是原地无限重连。
        self.auth_failed = False
        # stop() 里那个 close 任务要留住引用：create_task 的返回值只被事件循环弱引用，
        # 不留住可能还没跑完就被 GC 掉，socket 于是留在半关状态。
        self._close_task: asyncio.Task[None] | None = None

    # --- 生命周期 -------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            self._close_task = asyncio.create_task(ws.close())

    async def run(self) -> None:
        backoff = 1.0
        self.auth_failed = False
        try:
            await self.outbox_events.open()
        except Exception:  # noqa: BLE001 - 磁盘满/权限问题不该让控制通道起不来
            # 事件通道降级为"不可用"，控制、状态、媒体照常工作：
            # 一个附属能力失败，不能连带把机器人的远程控制一起停掉。
            logger.exception("事件 Outbox 打不开，本次运行不上报事件")
        self.state_reporter.start()
        self.event_uploader.start()
        await self._probe_backend()
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.observer.on_connecting()
                await self._session()
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                # 4401 = token 无效或已被吊销。先试自助轮换（凭旧 token 换新的），
                # 成功就立刻重连；失败说明这条路走不通了，退出而不是无限重试。
                if exc.code == 4401:
                    if await self._rotate_token():
                        continue
                    self.auth_failed = True
                    self.observer.on_auth_failed("token 被拒绝，自动轮换失败")
                    break
                logger.warning("控制连接断开：%s", exc)
                self.observer.on_disconnected(f"连接断开：{exc}")
            except Exception as exc:  # noqa: BLE001 - 任何连接期异常都走重连
                logger.warning("控制连接断开：%s", exc)
                self.observer.on_disconnected(f"连接断开：{exc}")

            if self._stop.is_set():
                break

            if time.monotonic() - started > STABLE_CONNECTION_SECONDS:
                backoff = 1.0

            delay = min(backoff, MAX_BACKOFF) * (1 + random.random() * 0.25)
            logger.info("%.1fs 后重连", delay)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            backoff = min(backoff * 2, MAX_BACKOFF)

        await self.state_reporter.stop()
        await self.event_uploader.stop()
        await self.media.stop_all()
        await self.leases.release_all()
        await self.backend.close()
        await self.uploads.close()

    async def _rotate_token(self) -> bool:
        """被 4401 之后的自救：换一把新 token，换不到就按原因给一句人话。

        以前这里只有一句"删掉本地凭据"——因为 A 对被拒的三种原因回的是同一个
        401。现在原因分开了，照抄那句建议反而会害人：token 被吊销时重新注册
        = 在 A 上凭空多出一台节点。
        """
        if not self.config.token or not self.config.agent_id:
            logger.error(
                "token 被拒绝，本地也没有可轮换的凭据（缺 token 或 agent_id）。\n"
                "删掉本地凭据后重新启动，让这台机器重新注册。"
            )
            return False

        logger.warning("token 被拒绝，尝试轮换…")
        outcome = await rotate(self.config)
        if outcome.ok:
            self.config.token = outcome.token or ""
            logger.info("token 已轮换，重新连接")
            await asyncio.sleep(1.0)
            return True

        if outcome.code == "revoked":
            logger.error(
                "这台 A 已吊销本机的 token：%s\n"
                "到 A 的「节点」页给这台机器重新签发，别直接重新注册——那会多出一台节点。",
                outcome.message,
            )
        elif outcome.code == "token_mismatch":
            logger.error(
                "token 与 A 的记录对不上：%s\n"
                "用 `robot-monitor-server issue-token` 拿到的新 token 启动。",
                outcome.message,
            )
        elif outcome.code == "unknown_agent":
            logger.error(
                "这台 A 不认识本机的 agent_id：%s\n"
                "如果是换了一台 A，用 --server 指向原来那台；确实要换，就删掉本地凭据重新注册。",
                outcome.message,
            )
        else:
            logger.error(
                "轮换没有成功（%s）：%s\n稍后重启再试，或到 A 上 `robot-monitor-server pending` 看看。",
                outcome.code or "unknown",
                outcome.message or "没有更多信息",
            )
        return False

    async def _session(self) -> None:
        headers = {"Authorization": f"Bearer {self.config.token}"}
        async with websockets.connect(
            self.config.ws_url,
            additional_headers=headers,
            max_size=MAX_FRAME_BYTES * 4,
            open_timeout=self.config.open_timeout,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("已连接 %s", self.config.ws_url)
            self.observer.on_connected()

            sender = asyncio.create_task(self._send_loop(ws), name="agent-send")
            heartbeat = asyncio.create_task(self._heartbeat(), name="agent-heartbeat")

            try:
                await self._enqueue(self._hello().to_json())
                async for raw in ws:
                    await self._on_frame(raw)
            finally:
                for task in (sender, heartbeat):
                    task.cancel()
                await asyncio.gather(sender, heartbeat, return_exceptions=True)
                self._ws = None
                for task in list(self._handlers):
                    task.cancel()
                if self._handlers:
                    await asyncio.gather(*self._handlers, return_exceptions=True)
                self._handlers.clear()

    # --- 发送 -----------------------------------------------------------

    async def _send_loop(self, ws: Any) -> None:
        """唯一的写者。

        协作式唤醒而不是 `await outbox.get()`：状态帧与命令帧在两个地方，
        用两个队列各自阻塞就会有两个写者（或者引入额外的转发协程）。
        这里让生产者只负责"放好 + set 一下信号"，由发送循环按固定优先级取。
        """
        while True:
            frame = self._drain()
            if frame is None:
                self._wake.clear()
                # clear 之后再取一次：生产者可能正好在 clear 之前 set 并放好了帧，
                # 那一拍 wait() 会永远等不到第二次唤醒。
                frame = self._drain()
                if frame is None:
                    await self._wake.wait()
                    continue
            await ws.send(frame)

    def _drain(self) -> str | None:
        """同步取一帧：`outbox` 优先，其次状态槽。

        优先级不能反：状态是"现在是什么"，晚一拍就过期了，而命令回包晚一拍
        用户就一直看着转圈。两者冲突时永远是丢弃状态。
        """
        while not self.outbox.empty():
            try:
                return self.outbox.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - empty() 与 get_nowait 之间的竞态
                break
        if self._state_slot is not None:
            frame, self._state_slot = self._state_slot, None
            return frame
        return None

    async def _enqueue(self, frame: str, *, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.outbox.put(frame), timeout)
        self._wake.set()

    def _enqueue_nowait(self, frame: str) -> bool:
        try:
            self.outbox.put_nowait(frame)
        except asyncio.QueueFull:
            return False
        self._wake.set()
        return True

    def publish_state(self, payload: dict[str, Any]) -> None:
        """投递一帧状态快照（覆盖写，永不阻塞）。

        payload 必须自带 `state_version`（单调递增）与 `observed_at`（采集时刻）：
        云端靠前者判新旧，页面靠后者显示"数据延迟几秒"。这一层不代填——
        只有采集方知道状态是哪一刻的。

        调用方可以是任何协程；槽满即覆盖，因此这个方法是 O(1) 且不会失败。
        上下文里没有连接时（未连上/正在重连）也照常入槽：要么下一次连上后
        发出最新一帧，要么根本没机会发——两种情况都不需要调用方关心。
        """
        if not payload:
            return
        if self._state_slot is not None:
            self.state_dropped += 1
        self._state_slot = Message.state_frame(payload).to_json()
        self._wake.set()

    async def _reply(self, message: Message) -> None:
        try:
            await self._enqueue(message.to_json())
        except TimeoutError:
            logger.warning("outbox 已满，回包 %s 被丢弃", message.id)

    async def _send_event(self, payload: dict[str, Any]) -> None:
        frame = Message(type=MessageType.EVENT, payload=payload).to_json()
        if not self._enqueue_nowait(frame):
            logger.debug("outbox 已满，事件被丢弃：%s", payload.get("kind"))
        # 值得追溯的事件额外落本地 Outbox：C0 上的 EVENT 帧是尽力而为，
        # 断线期间发不出去就没了；Outbox 会在恢复后补传并被云端去重。
        kind = str(payload.get("kind") or "")
        if kind in DURABLE_EVENTS:
            await self._record(kind, payload)

    async def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        seq = await self.outbox_events.append(event_type, payload)
        if seq is not None:
            self.event_uploader.notify()

    async def _record_command(
        self,
        command: CommandName | None,
        message: Message,
        state: str,
        code: str,
        error: str,
    ) -> None:
        """命令终态进事件表：页面能对账，排障时也能还原"点了几次、结果如何"。

        `command_id` 优先取信封里的那个（与云端帧 `id`、`command_log` 同值）。
        取帧 id 作为兜底是为了兼容"旧云端生成两个不同 id"的部署——那种情况下
        事件里的 command_id 至少还能与那条命令的帧对应上。
        """
        await self._record(
            "command.result",
            {
                "command": command.value if command is not None else (message.command or ""),
                "command_id": str(message.payload.get("command_id") or message.id),
                "state": state,
                "code": code,
                "error": error[:200],
                "trace_id": str(message.payload.get("trace_id") or ""),
            },
        )

    async def _send_snapshot(
        self, request_id: str, image: bytes, meta: dict[str, Any]
    ) -> None:
        """把 JPEG 送到云端。走独立的 HTTP 通道，不进控制帧。

        这条路径刻意不走 C0：一张 20KB 的图 base64 之后 27KB，加上帧头就要挤占
        命令队列；而控制通道单帧上限 256KB，弱网下（分片丢包 → 重传）会直接
        把用户的"停止"命令堵在后面。
        """
        if not self.config.token:
            raise AgentCommandError(ErrorCode.UNAUTHORIZED, "还没有云端凭据，无法回传快照")
        url = f"{self.config.http_base}/internal/snapshot/{request_id}"
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "image/jpeg",
            "X-Captured-At": str(meta.get("captured_at") or ""),
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, content=image, headers=headers)
        except httpx.HTTPError as exc:
            raise AgentCommandError(
                ErrorCode.TIMEOUT, f"快照回传失败：{exc}"
            ) from exc

        if response.status_code == 404:
            # 云端已经等超时并丢弃了这个请求：图片没人要了。如实报错，
            # 否则页面会一直转圈等一张永远不会出现的图。
            raise AgentCommandError(ErrorCode.EXPIRED, "快照请求已在云端超时，图片未被接收")
        if response.status_code >= 400:
            raise AgentCommandError(
                ErrorCode.INTERNAL, f"快照回传失败：HTTP {response.status_code}"
            )

    # --- 接收 -----------------------------------------------------------

    async def _on_frame(self, raw: str | bytes) -> None:
        try:
            message = Message.parse(raw)
        except ProtocolError as exc:
            # 帧都解析不了，说明两端协议已经不兼容了。继续收只会一直错。
            logger.error("收到无法解析的帧：%s", exc)
            raise

        if message.type is MessageType.COMMAND:
            self._spawn(self._handle_command(message))
        elif message.type is MessageType.ACK:
            await self._on_welcome(message.payload)
        elif message.type is MessageType.EVENT:
            logger.debug("服务端事件：%s", message.payload)
        elif message.type is MessageType.ERROR:
            detail = message.error
            logger.warning("服务端报错：%s", detail.message if detail else "?")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _on_welcome(self, payload: dict[str, Any]) -> None:
        interval = payload.get("heartbeat_interval")
        if isinstance(interval, (int, float)) and interval > 0:
            self.config.heartbeat_interval = float(interval)

        resumes = payload.get("resume_uploads") or []
        if resumes:
            logger.info("服务端要求续传 %d 个未完成的上传", len(resumes))
        for item in resumes:
            try:
                job = UploadJob.from_payload(dict(item))
            except (AgentCommandError, TypeError, ValueError) as exc:
                logger.warning("跳过无法解析的续传任务：%s", exc)
                continue
            await self.uploads.start(job)

    async def _handle_command(self, message: Message) -> None:
        command = message.command_name
        if command is None or command not in AGENT_ALLOWED_COMMANDS:
            logger.warning("拒绝未知命令：%r", message.command)
            await self._reply(
                Message.error_frame(
                    message.id,
                    ErrorCode.UNKNOWN_COMMAND,
                    f"不支持的命令：{message.command!r}",
                )
            )
            return

        try:
            result = await dispatch(command, message.payload, self.context)
        except AgentCommandError as exc:
            await self._reply(Message.error_frame(message.id, exc.code, exc.message))
            await self._record_command(command, message, "rejected", exc.code.value, exc.message)
        except SandboxError as exc:
            await self._reply(Message.error_frame(message.id, ErrorCode.FORBIDDEN, str(exc)))
            await self._record_command(command, message, "rejected", ErrorCode.FORBIDDEN.value, str(exc))
        except Exception as exc:
            logger.exception("处理命令 %s 时出错", command.value)
            await self._reply(Message.error_frame(message.id, ErrorCode.INTERNAL, str(exc)))
            await self._record_command(command, message, "failed", ErrorCode.INTERNAL.value, str(exc))
        else:
            await self._reply(Message.result_frame(message.id, result))
            await self._record_command(command, message, "succeeded", "", "")

    # --- 心跳 -----------------------------------------------------------

    async def _heartbeat(self) -> None:
        """应用层心跳。

        WS 自带的 ping/pong 在协议层，A 的业务代码看不到；A 判断"这台机器还活着"
        必须靠应用层的心跳帧。两套都留：传输层的管连接死活，应用层的管业务在线。
        """
        while True:
            await asyncio.sleep(self.config.heartbeat_interval)
            frame = Message(
                type=MessageType.EVENT,
                payload={
                    "kind": "heartbeat",
                    "ts": time.time(),
                    "active_uploads": self.uploads.active,
                },
            ).to_json()
            if not self._enqueue_nowait(frame):
                logger.warning("outbox 已满，跳过一拍心跳")
            self.observer.on_heartbeat()

    async def _probe_backend(self) -> None:
        """启动时确认 backend_url 指向的确实是 robot backend。

        不阻塞、不致命：本地控制面出问题不该让云端接入起不来。但一定要留一条
        醒目的日志——否则用户只会看到"页面出不了图"，而真正的原因（端口填成了
        云端 server 的 8000）藏在几百行 debug 里。

        节流：成功之后不再探；失败时最多每分钟一次，避免每次重连都等 3s 超时。
        """
        if self._backend_ok:
            return
        now = time.time()
        if now - self._last_probe_at < 60.0:
            return
        self._last_probe_at = now

        ok, detail = await self.backend.probe()
        self._backend_ok = ok
        if ok:
            logger.info("本地 backend 就绪：%s", self.config.backend_url)
            return
        logger.error(
            "本地 backend 不可用（%s）：%s；云台/快照/实时画面将不可用，"
            "文件传输与云端连接不受影响",
            self.config.backend_url,
            detail,
        )

    def _hello(self) -> Message:
        return Message.hello_frame(
            agent_id=self.config.name or socket.gethostname(),
            payload={
                "hostname": socket.gethostname(),
                "os": platform.system().lower(),
                "agent_version": __version__,
                "roots": [str(self.sandbox.root)],
                "protocol": PROTOCOL_VERSION,
                # 空列表 = 明确回答"这个构建没有这些能力"；字段缺失 = 老版本。
                # 云端要分开显示：前者不该出现按钮，后者显示"不支持远程画面"而不是转圈。
                "capabilities": list(CAPABILITIES),
                "media_profiles": list(MEDIA_PROFILES),
                "max_frame_kbps": MAX_FRAME_KBPS,
            },
        )
