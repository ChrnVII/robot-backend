"""守护进程主体：配置解析、注册状态机、连接生命周期、控制面操作。

与 monitor 的 agent 最大的不同：**启动不再是一次性脚本**。没有 server、没有
凭据、等待审批、token 被吊销，都不再是"打一行日志然后退出"，而是进程停在某个
明确状态里，等页面或人工把它推进一步。这样本地页面才有东西可展示、有按钮可按。

状态迁移（简化）：

    starting ──▶ unregistered ──(enroll)──▶ enrolling ──▶ pending ──▶ connecting
                     ▲                                        │            │
                     │                                        ▼            ▼
                     └────────(删除凭据)───────────── unauthorized ◀── online/offline
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from typing import Any

from . import __version__
from .client import AgentClient, AgentObserver
from .config import AgentConfig, ConfigError, load_config, save_config
from .credentials import (
    Credentials,
    clear_all,
    forget_slot,
    load,
    same_server,
    save,
    store_dir,
    stored_slots,
)
from .enroll import (
    POLL_INTERVAL,
    POLL_TIMEOUT,
    EnrollError,
    poll_enrollment,
    rotate,
    start_enrollment,
)
from .sandbox import Sandbox, SandboxError
from .state import AgentState

logger = logging.getLogger("robot-cloud-agent")

# 页面可改的配置项。control_socket 不在其中：换 socket 路径要重新绑定监听，
# 属于"改完重启"的范畴，不给热更新。
EDITABLE_FIELDS = (
    "server",
    "root",
    "name",
    "heartbeat_interval",
    "max_concurrent_uploads",
    "verify_tls",
    "log_level",
)


class CloudAgent(AgentObserver):
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.state = AgentState()
        self.state.configure(config)
        self.sandbox: Sandbox | None = None
        self.client: AgentClient | None = None
        self.uploads: Any | None = None

        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._paused = False
        self._reload = False
        self._enroll_task: asyncio.Task[None] | None = None
        self._supervise_task: asyncio.Task[None] | None = None
        self._auto_enroll_attempted = False

    # --- 生命周期 -------------------------------------------------------

    async def run(self) -> None:
        self.state.mark("starting")
        self._supervise_task = asyncio.create_task(self._supervise(), name="cloud-agent-supervise")
        try:
            await self._stop.wait()
        finally:
            await self._shutdown()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._cancel_enroll_task()
        if self.client is not None:
            self.client.stop()

    async def _shutdown(self) -> None:
        await self._stop_enroll()
        if self.client is not None:
            self.client.stop()
        if self._supervise_task is not None:
            self._supervise_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervise_task
        self.state.mark("stopped")

    async def _supervise(self) -> None:
        while not self._stop.is_set():
            if self._paused:
                self.state.paused = True
                self.state.mark("paused")
                await self._wait_wake()
                continue

            try:
                ready = await self._prepare()
            except (ConfigError, SandboxError) as exc:
                self.state.mark_error("configuration", str(exc))
                await self._sleep_or_wake(5)
                continue
            except EnrollError as exc:
                self.state.mark_error("enroll_failed", str(exc))
                await self._sleep_or_wake(5)
                continue

            if not ready:
                await self._wait_wake()
                continue

            self._reload = False

            try:
                sandbox = Sandbox(self.config.root)
            except SandboxError as exc:
                self.state.mark_error("invalid_root", str(exc))
                await self._sleep_or_wake(5)
                continue

            self.sandbox = sandbox
            client = AgentClient(self.config, sandbox, observer=self)
            self.client = client
            self.uploads = client.uploads
            self.state.mark("connecting")
            try:
                await client.run()
            finally:
                self.client = None

            if self._stop.is_set():
                break
            if self._paused:
                continue
            if client.auth_failed:
                self.state.mark("unauthorized")
                await self._wait_wake()
            elif self._reload:
                self._reload = False
            else:
                self.state.mark("offline")
                await self._sleep_or_wake(5)

    async def _prepare(self) -> bool:
        """补齐 server / 凭据 / root。返回 True 表示可以连了。"""
        stored = load(self.config)
        if stored is None and self.config.server.strip():
            stored = await self._adopt_same_server()
        if stored is not None:
            self._apply_credentials(stored)

        if not self.config.server.strip():
            self.state.mark("unregistered")
            return False

        if not self.config.root.strip():
            raise ConfigError("还没有配置共享目录（root），请在页面里填写一个目录")

        if self.config.token.strip():
            return True

        # 没有 token：启动时自动注册一次。失败后不自动重试——反复提交会在云端
        # 堆出一串待批准记录，等页面上的「立即注册」或「手工填凭据」更干净。
        if not self._auto_enroll_attempted and (
            self._enroll_task is None or self._enroll_task.done()
        ):
            self._start_enroll()
        return False

    async def _adopt_same_server(self) -> Credentials | None:
        """地址变了但还是同一台云端：沿用旧 token，不重复注册。"""
        from .enroll import adopt_same_server

        try:
            return await adopt_same_server(self.config)
        except Exception as exc:  # noqa: BLE001 - 认不出来就走正常注册
            logger.debug("探测云端身份失败：%s", exc)
            return None

    def _apply_credentials(self, credentials: Credentials) -> None:
        self.config.server = self.config.server or credentials.server
        self.config.token = self.config.token or credentials.token
        self.config.agent_id = self.config.agent_id or credentials.agent_id
        self.config.instance_id = self.config.instance_id or credentials.instance_id
        if not self.config.root:
            self.config.root = credentials.root or ""
        if not self.config.name:
            self.config.name = credentials.name
        self.state.configure(self.config)

    # --- 注册 -----------------------------------------------------------

    def _start_enroll(self) -> None:
        if self._enroll_task is not None and not self._enroll_task.done():
            return
        self._auto_enroll_attempted = True
        self.state.pending = None
        self._enroll_task = asyncio.create_task(self._enroll_until_approved(), name="enroll")

    async def _enroll_until_approved(self) -> None:
        deadline = time.monotonic() + POLL_TIMEOUT
        try:
            self.state.mark("enrolling")
            start = await start_enrollment(self.config)
            if start.credentials is not None:
                self._apply_credentials(start.credentials)
                save_config(self.config)
                self.state.pending = None
                self.state.mark("offline")
                self._wake.set()
                return

            assert start.pending_id is not None
            self.state.pending = {
                "id": start.pending_id,
                "hint": start.hint,
                "since": time.time(),
            }
            self.state.mark("pending")

            while time.monotonic() < deadline and not self._stop.is_set():
                await asyncio.sleep(POLL_INTERVAL)
                credentials = await poll_enrollment(self.config, start.pending_id)
                if credentials is not None:
                    self._apply_credentials(credentials)
                    save_config(self.config)
                    self.state.pending = None
                    self.state.mark("offline")
                    self._wake.set()
                    return

            raise EnrollError("等待批准超时，请重新发起注册")
        except asyncio.CancelledError:
            self.state.pending = None
            if not self._paused and not self._stop.is_set():
                self.state.mark("unregistered")
            raise
        except EnrollError as exc:
            self.state.pending = None
            self.state.mark_error("enroll_failed", str(exc))
        except Exception as exc:  # noqa: BLE001 - 注册期任何异常都不该打挂进程
            self.state.pending = None
            self.state.mark_error("enroll_failed", str(exc))

    async def start_enroll(
        self,
        server: str | None = None,
        name: str | None = None,
        root: str | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if server is not None:
            changes["server"] = server
        if name is not None:
            changes["name"] = name
        if root is not None:
            changes["root"] = root
        if changes:
            await self._stop_enroll()
            self._update_config(changes)

        if not self.config.server.strip():
            raise ConfigError("缺少云端地址（server）")
        if not self.config.root.strip():
            raise ConfigError("缺少共享目录（root）")

        if self._enroll_task is not None and not self._enroll_task.done():
            await self._stop_enroll()
        self._auto_enroll_attempted = False
        self._start_enroll()
        return self.status()

    async def cancel_enroll(self) -> dict[str, Any]:
        await self._stop_enroll()
        self._auto_enroll_attempted = False
        self.state.mark("unregistered")
        return self.status()

    # --- 配置 -----------------------------------------------------------

    def config_view(self) -> dict[str, Any]:
        data = self.config.public_dict()
        data["agent_id"] = self.config.agent_id or ""
        data["enrolling"] = bool(self._enroll_task and not self._enroll_task.done())
        return data

    def _cancel_enroll_task(self) -> asyncio.Task[None] | None:
        """停掉正在等待批准的注册任务，返回给调用方决定是否 await。

        换云端、填新凭据、删凭据时必须先停：否则一个还在轮询旧云端的注册任务
        可能在几秒后把旧 token 落盘，覆盖掉刚设置的新身份。
        """
        task = self._enroll_task
        self._enroll_task = None
        self.state.pending = None
        if task is not None and not task.done():
            task.cancel()
            return task
        return None

    async def _stop_enroll(self) -> None:
        task = self._cancel_enroll_task()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _update_config(self, changes: dict[str, Any]) -> None:
        # 只接受白名单字段；布尔/数值在这里转一次，页面传 "3" 也能用。
        clean: dict[str, Any] = {}
        for key in EDITABLE_FIELDS:
            if key not in changes:
                continue
            value = changes[key]
            try:
                if key == "heartbeat_interval":
                    value = float(value)
                elif key == "max_concurrent_uploads":
                    value = int(value)
                elif key == "verify_tls":
                    value = bool(value)
                else:
                    value = str(value).strip()
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{key} 的取值不合法：{value!r}") from exc
            clean[key] = value

        previous_server = self.config.server
        updated = self.config.replace(**clean)

        # 换云端地址必须换身份：旧 token 是旧云端签的，带过去只会换来 4401。
        if "server" in clean and not same_server(previous_server, updated.server):
            updated.token = ""
            updated.agent_id = ""
            updated.instance_id = ""
            self._cancel_enroll_task()
        elif "root" in clean and updated.root != self.config.root:
            # 注册申请里带了 root_path，改了就要按新目录重新提交。
            self._cancel_enroll_task()

        self.config = updated
        self.state.configure(self.config)
        save_config(self.config)
        self._apply_log_level()

    def _apply_log_level(self) -> None:
        level = getattr(logging, self.config.log_level.upper(), None)
        if isinstance(level, int):
            logging.getLogger().setLevel(level)

    async def set_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        previous_root = self.config.root
        if {"server", "root"} & set(payload):
            await self._stop_enroll()
        self._update_config(payload)
        self._auto_enroll_attempted = False

        if previous_root != self.config.root:
            await self._cancel_all_uploads()

        await self._restart_session()
        return self.status()

    async def reload_config(self) -> dict[str, Any]:
        """手工改过 TOML 之后，让进程重新读一遍。"""
        try:
            fresh = load_config(self.config.config_path)
        except ConfigError as exc:
            raise ConfigError(str(exc)) from exc
        # 凭据不来自文件，必须保留内存里的这一份
        fresh.token = self.config.token
        fresh.agent_id = self.config.agent_id
        fresh.instance_id = self.config.instance_id
        fresh.config_path = self.config.config_path
        self.config = fresh
        self.state.configure(self.config)
        self._apply_log_level()
        self._auto_enroll_attempted = False
        await self._restart_session()
        return self.status()

    # --- 凭据 -----------------------------------------------------------

    def list_credentials(self) -> dict[str, Any]:
        slots = []
        for item in stored_slots(self.config):
            slots.append(
                {
                    "server": item.server,
                    "agent_id": item.agent_id,
                    "root": item.root or "",
                    "name": item.name or "",
                    "instance_id": item.instance_id or "",
                    "active": same_server(item.server, self.config.server),
                }
            )
        return {
            "state_dir": str(store_dir(self.config)),
            "active_server": self.config.server,
            "slots": slots,
        }

    async def put_credential(
        self,
        server: str,
        token: str,
        agent_id: str | None = None,
        root: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """手工填凭据：对应 `issue-token` 之后把 token 贴进来的场景。"""
        await self._stop_enroll()
        credentials = Credentials(
            server=server,
            token=token,
            agent_id=agent_id or "",
            root=root or self.config.root or None,
            name=name or self.config.name,
        )
        save(self.config, credentials)

        self.config.server = server
        self.config.token = token
        self.config.agent_id = agent_id or ""
        self.config.instance_id = credentials.instance_id
        if root:
            self.config.root = root
        if name:
            self.config.name = name
        self.state.configure(self.config)
        save_config(self.config)
        self._auto_enroll_attempted = False
        await self._restart_session()
        return self.status()

    async def activate_credential(self, server: str) -> dict[str, Any]:
        """切到本地已有的某个云端槽（页面「切换」按钮）。

        只换身份，不动凭据文件；root / name 缺省时才从槽里补。
        """
        await self._stop_enroll()
        target = next(
            (item for item in stored_slots(self.config) if same_server(item.server, server)),
            None,
        )
        if target is None:
            raise ConfigError(f"本地没有这个云端的凭据：{server}")

        self.config.server = target.server
        self.config.token = target.token
        self.config.agent_id = target.agent_id
        self.config.instance_id = target.instance_id
        if not self.config.root:
            self.config.root = target.root or ""
        if not self.config.name:
            self.config.name = target.name
        self.state.configure(self.config)
        save_config(self.config)
        self._auto_enroll_attempted = False
        await self._restart_session()
        return self.status()

    async def forget_credential(self, server: str) -> dict[str, Any]:
        await self._stop_enroll()
        forget_slot(self.config, server)
        if same_server(server, self.config.server):
            self.config.token = ""
            self.config.agent_id = ""
            self.config.instance_id = ""
            self.state.configure(self.config)
            # 删凭据是明确动作，之后应该停住等页面上点「立即注册」，
            # 而不是立刻又提交一条待批准申请。
            self._auto_enroll_attempted = True
            await self._restart_session(state="unregistered")
        return self.status()

    async def clear_credentials(self) -> dict[str, Any]:
        await self._stop_enroll()
        removed = clear_all(self.config)
        self.config.token = ""
        self.config.agent_id = ""
        self.config.instance_id = ""
        self.state.configure(self.config)
        self._auto_enroll_attempted = True
        await self._restart_session(state="unregistered")
        return {"removed": removed, **self.status()}

    async def rotate_token(self) -> dict[str, Any]:
        outcome = await rotate(self.config)
        if outcome.ok:
            self.config.token = outcome.token or ""
            self.state.mark("offline")
            await self._restart_session()
            return {"ok": True, "message": "token 已轮换", **self.status()}
        self.state.mark_error(outcome.code or "rotate_failed", outcome.message or "轮换失败")
        return {
            "ok": False,
            "code": outcome.code,
            "message": outcome.message or "轮换失败",
            **self.status(),
        }

    # --- 连接 -----------------------------------------------------------

    async def reconnect(self) -> dict[str, Any]:
        await self._restart_session()
        return self.status()

    async def pause(self) -> dict[str, Any]:
        self._paused = True
        self.state.paused = True
        self._cancel_enroll_task()
        if self.client is not None:
            self.client.stop()
        self.state.mark("paused")
        self._wake.set()
        return self.status()

    async def resume(self) -> dict[str, Any]:
        self._paused = False
        self.state.paused = False
        self.state.mark("offline")
        self._wake.set()
        return self.status()

    async def cancel_upload(self, upload_id: str) -> dict[str, Any]:
        if self.uploads is None:
            raise ConfigError("当前没有运行中的上传管理器")
        await self.uploads.cancel_local(upload_id)
        return self.status()

    async def restart_process(self) -> dict[str, Any]:
        """软重启：重新 exec 自己。

        外部守护进程的自动重启也能做到，但页面按下去要立刻有反应；而进程
        已经死掉时页面本来就够不着它（那种情况由 backend 走 systemctl）。这里只
        处理"进程还活着但想重来一遍"。
        """
        if self._stop.is_set():
            return {"restarting": False, "message": "进程正在退出"}
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, _reexec)
        return {"restarting": True, "message": "正在重启进程"}

    async def _restart_session(self, state: str | None = None) -> None:
        self._reload = True
        if self.client is not None:
            self.client.stop()
        if state is not None:
            self.state.mark(state)
        self._wake.set()

    async def _cancel_all_uploads(self) -> None:
        if self.uploads is None:
            return
        for item in list(self.uploads.snapshot()["jobs"]):
            await self.uploads.cancel_local(item["upload_id"])

    # --- 观察者 ---------------------------------------------------------

    def on_connecting(self) -> None:
        self.state.mark("connecting")

    def on_connected(self) -> None:
        self.state.connected_at = time.time()
        self.state.last_heartbeat_at = time.time()
        self.state.mark("online")
        logger.info("已连上云端 %s", self.config.server)

    def on_disconnected(self, reason: str) -> None:
        if self._paused or self._stop.is_set():
            return
        self.state.mark("offline")
        self.state.last_error = {"code": "disconnected", "message": reason, "at": time.time()}

    def on_heartbeat(self) -> None:
        self.state.last_heartbeat_at = time.time()

    def on_auth_failed(self, message: str) -> None:
        self.state.connected_at = None
        self.state.mark_error("unauthorized", message)

    # --- 状态 -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        data = self.state.snapshot(self.config, self.uploads)
        data["config"] = self.config.public_dict()
        data["credentials"] = self.list_credentials()
        data["version"] = __version__
        return data

    # --- 等待 -----------------------------------------------------------

    async def _wait_wake(self) -> None:
        await self._wake.wait()
        self._wake.clear()

    async def _sleep_or_wake(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        self._wake.clear()


def _reexec() -> None:  # pragma: no cover - 只有真实进程会走这里
    logger.warning("按页面请求重启进程")
    try:
        os.execv(sys.executable, [sys.executable, "-m", "app.cloud_agent", *sys.argv[1:]])
    except OSError:
        logger.exception("重启失败，请手动重启")
