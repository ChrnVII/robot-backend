"""控制通道客户端。

三条纪律，对应计划书里提炼的结论：

1. **单写者。** 只有 `_send_loop` 能 `ws.send`。指令回包、心跳、失败通知全部
   先进 `outbox`，由同一个协程按序发出。两个协程并发 send 会交错帧，触发条件
   随机，是最难查的一类 bug。

2. **重连必须有退避 + jitter。** A 重启时所有 B 会在同一秒回来，不打退避就是
   自己 DDoS 自己。连接稳定超过 60s 才重置退避，否则"连上 1 秒就断"会以
   1s 的节奏无限重试。

3. **控制连接断了不等于上传断了。** 上传走 HTTP，是独立的。WS 掉线时不去
   cancel 正在进行的上传——它们多半还能传完。重连后 A 会把未完成的 upload
   再报一遍，本地已经在跑的会被 `start()` 幂等忽略。
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

import websockets
from websockets.exceptions import ConnectionClosed

from . import __version__
from .config import AgentConfig
from .enroll import rotate
from .errors import AgentCommandError
from .handlers import HandlerContext, dispatch
from .protocol import (
    AGENT_ALLOWED_COMMANDS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    Message,
    MessageType,
    ProtocolError,
)
from .sandbox import Sandbox, SandboxError
from .uploader import UploadJob, UploadManager

logger = logging.getLogger("robot-cloud-agent")

OUTBOX_SIZE = 128
STABLE_CONNECTION_SECONDS = 60.0
MAX_BACKOFF = 30.0


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
        self.uploads = UploadManager(sandbox, config, self._send_event)
        self.context = HandlerContext(sandbox=sandbox, uploads=self.uploads)
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
        """唯一的写者。"""
        while True:
            frame = await self.outbox.get()
            await ws.send(frame)

    async def _enqueue(self, frame: str, *, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.outbox.put(frame), timeout)

    def _enqueue_nowait(self, frame: str) -> bool:
        try:
            self.outbox.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            return False

    async def _reply(self, message: Message) -> None:
        try:
            await self._enqueue(message.to_json())
        except TimeoutError:
            logger.warning("outbox 已满，回包 %s 被丢弃", message.id)

    async def _send_event(self, payload: dict[str, Any]) -> None:
        frame = Message(type=MessageType.EVENT, payload=payload).to_json()
        if not self._enqueue_nowait(frame):
            logger.debug("outbox 已满，事件被丢弃：%s", payload.get("kind"))

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
        except SandboxError as exc:
            await self._reply(Message.error_frame(message.id, ErrorCode.FORBIDDEN, str(exc)))
        except Exception as exc:
            logger.exception("处理命令 %s 时出错", command.value)
            await self._reply(Message.error_frame(message.id, ErrorCode.INTERNAL, str(exc)))
        else:
            await self._reply(Message.result_frame(message.id, result))

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

    def _hello(self) -> Message:
        return Message.hello_frame(
            agent_id=self.config.name or socket.gethostname(),
            payload={
                "hostname": socket.gethostname(),
                "os": platform.system().lower(),
                "agent_version": __version__,
                "roots": [str(self.sandbox.root)],
                "protocol": PROTOCOL_VERSION,
            },
        )
