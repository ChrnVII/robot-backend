"""指令处理器：文件类 + 摄像头类。

纪律：

- **所有文件系统调用都走 `asyncio.to_thread`。** `os.scandir` 在万级目录上要
  几百毫秒，网络挂载上可能几秒。直接写在协程里会卡住事件循环——心跳发不出去，
  A 判离线，正在传的文件被打断。
- **所有失败都翻译成明确的错误码。** 权限问题必须是 `forbidden` 而不是
  `internal`，否则前端分不清"该换个文件"还是"服务坏了"。
- **文件侧只读。** 整个文件里不存在 `"w"`、`"a"`、`"r+"` 打开文件的地方。

摄像头类命令（`camera.*`）多一层仲裁：去重、序号、过期（见 `arbiter.py`）。
它们不直接摸设备，而是走本机 backend 的 HTTP 接口——backend 才是唯一持有
相机凭据与 ISAPI 配置的进程，也是控制租约的签发方。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .arbiter import CommandArbiter, parse_envelope
from .backend import LocalBackend, lease_id_of
from .errors import AgentCommandError
from .media import MediaError, MediaManager
from .protocol import AGENT_ALLOWED_COMMANDS, CommandName, ErrorCode, new_id
from .sandbox import SEP, Sandbox
from .uploader import UploadJob, UploadManager

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

#: 需要仲裁（去重/序号/过期）的命令。
ARBITRATED_COMMANDS: frozenset[CommandName] = frozenset(
    {
        CommandName.CAMERA_STREAM_START,
        CommandName.CAMERA_STREAM_STOP,
        CommandName.CAMERA_PTZ,
        CommandName.CAMERA_PRESET_GOTO,
        CommandName.CAMERA_SNAPSHOT,
    }
)

#: 云台速度：云端给 1–7 的档位（和大多数球机 Web 界面一致），
#: 设备驱动要 0–1 的浮点，在这里换算一次，别让两层各猜一套。
PTZ_SPEED_MAX = 7.0

#: 抓图的二进制回传回调：(request_id, jpeg, meta)。
SnapshotSink = Callable[[str, bytes, dict[str, Any]], Awaitable[None]]


class LeaseKeeper:
    """按会话维护本机控制租约。

    租约 TTL 默认 3s（`safety.lease_ttl_ms`），所以每收到一条云台命令就续一次；
    同一 holder 续期不会换 lease_id，两个标签页不会互相顶掉。

    为什么必须走租约：robot-core 的仲裁顺序是"本地人工 > 远程人工 > 自主任务"。
    不带租约的命令会被直接拒，等于云端根本控不动；带租约则本地人一操作，
    云端立刻收到 `control_held` 而不是"点了没反应"。
    """

    #: 续期节流：小于这个间隔的重复申请没意义（TTL 是秒级）。
    RENEW_AFTER = 1.5

    def __init__(self, backend: LocalBackend) -> None:
        self._backend = backend
        self._leases: dict[str, tuple[str, float]] = {}

    def _holder(self, session_id: str) -> str:
        return f"cloud:{session_id or 'adhoc'}"

    async def ensure(self, session_id: str) -> str:
        now = time.time()
        current = self._leases.get(session_id)
        if current is not None and now - current[1] < self.RENEW_AFTER:
            return current[0]
        data = await self._backend.acquire_lease(self._holder(session_id))
        lease_id = lease_id_of(data)
        self._leases[session_id] = (lease_id, now)
        return lease_id

    async def release(self, session_id: str) -> None:
        current = self._leases.pop(session_id, None)
        if current is not None:
            await self._backend.release_lease(current[0], "cloud session closed")

    async def release_all(self) -> None:
        for session_id in list(self._leases):
            await self.release(session_id)


@dataclass
class HandlerContext:
    sandbox: Sandbox
    uploads: UploadManager
    # 下面三项在 P1 才有；单独实例化 handler 的单元测试可以不传。
    backend: LocalBackend | None = None
    media: MediaManager | None = None
    leases: LeaseKeeper | None = None
    snapshots: SnapshotSink | None = None
    arbiter: CommandArbiter = field(default_factory=CommandArbiter)


async def dispatch(
    command: CommandName, payload: dict[str, Any], ctx: HandlerContext
) -> dict[str, Any]:
    handler = _HANDLERS.get(command)
    if handler is None:
        # command_name 已经保证在枚举内，这里再挡一次：
        # 枚举将来扩容但忘写 handler 时，行为是"明确拒绝"而不是 KeyError。
        raise AgentCommandError(ErrorCode.UNKNOWN_COMMAND, f"命令暂未实现：{command.value}")
    if command in ARBITRATED_COMMANDS:
        return await _dispatch_arbitrated(command, handler, payload, ctx)
    return await handler(payload, ctx)


async def _dispatch_arbitrated(
    command: CommandName,
    handler: Callable[[dict[str, Any], HandlerContext], Awaitable[dict[str, Any]]],
    payload: dict[str, Any],
    ctx: HandlerContext,
) -> dict[str, Any]:
    """去重 → 过期 → 序号 → 执行。

    顺序不能换：先查去重是"这条我做过，直接给答案"（比重发更早），
    再判过期（做也可能来不及），最后才比序号（决定要不要覆盖上一个动作）。
    """
    envelope = parse_envelope(payload, new_id())

    replayed = ctx.arbiter.replay(envelope)
    if replayed is not None:
        # 重发：不重复执行，但要回同一个答案，否则云端会一直当它没到。
        return {**replayed, "replayed": True}

    if ctx.arbiter.is_expired(envelope):
        raise AgentCommandError(
            ErrorCode.EXPIRED,
            f"{command.value} 超过 deadline（{(envelope.deadline or 0) - time.time():.2f}s），未执行",
        )

    if ctx.arbiter.is_stale(envelope):
        raise AgentCommandError(
            ErrorCode.STALE,
            f"sequence {envelope.sequence} 落后于已执行序号，已丢弃（更新的指令已覆盖它）",
        )

    result = await handler(payload, ctx)
    ctx.arbiter.accept(envelope)
    ctx.arbiter.remember(envelope, result)
    return result


# --- ping ---------------------------------------------------------------


async def _ping(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    return {"pong": time.time(), "echo": payload.get("echo")}


# --- list_dir -----------------------------------------------------------


async def _list_dir(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    rel = _string(payload, "rel", default="")
    limit = _clamp_int(payload.get("limit"), DEFAULT_LIMIT, 1, MAX_LIMIT)
    cursor = _cursor_offset(payload.get("cursor"))

    target = ctx.sandbox.resolve(rel)
    try:
        is_dir = await asyncio.to_thread(target.is_dir)
    except OSError as exc:
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"无法访问 {rel or '/'}：{exc}") from exc
    if not is_dir:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, f"{rel!r} 不是目录")
    if not await asyncio.to_thread(os.access, target, os.R_OK | os.X_OK):
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"没有读取 {rel or '/'} 的权限")

    entries = await asyncio.to_thread(_scan, target, rel)

    page = entries[cursor : cursor + limit]
    next_cursor = cursor + limit if cursor + limit < len(entries) else None
    return {
        "path": rel,
        "entries": page,
        "total": len(entries),
        "next_cursor": str(next_cursor) if next_cursor is not None else None,
    }


def _scan(target: Path, rel: str) -> list[dict[str, Any]]:
    """列目录。单项失败就跳过该项，不让一个坏文件毁掉整个列表。

    软链接不跟随：它可能是逃逸的入口（沙箱会在 resolve 阶段拦住），也可能只是
    一个普通快捷方式。统一按 `symlink` 展示，前端不提供展开——少一个入口，
    就少一条要论证的路径。
    """
    entries: list[dict[str, Any]] = []
    with os.scandir(target) as iterator:
        for entry in iterator:
            try:
                if entry.is_symlink():
                    info = entry.stat(follow_symlinks=False)
                    kind = "symlink"
                elif entry.is_dir(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    kind = "dir"
                elif entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    kind = "file"
                else:
                    # FIFO / socket / 设备文件：不共享，也不展示
                    continue
            except OSError:
                continue

            entries.append(
                {
                    "name": entry.name,
                    "rel": f"{rel}{SEP}{entry.name}" if rel else entry.name,
                    "type": kind,
                    "size": info.st_size if kind != "dir" else 0,
                    "mtime": info.st_mtime,
                }
            )

    # 目录在前、文件在后，同类按名字排（大小写归一，避免 A/a 交替排列）
    entries.sort(key=lambda item: (item["type"] != "dir", str(item["name"]).casefold()))
    return entries


# --- stat_file ----------------------------------------------------------


async def _stat_file(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    rel = _string(payload, "rel", default="")
    if not rel:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "需要指定文件路径")

    target = ctx.sandbox.resolve(rel)
    return await asyncio.to_thread(_stat_sync, target, rel)


def _stat_sync(target: Path, rel: str) -> dict[str, Any]:
    try:
        info = target.stat()
    except FileNotFoundError as exc:
        raise AgentCommandError(ErrorCode.FILE_NOT_FOUND, f"文件不存在：{rel}") from exc
    except PermissionError as exc:
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"没有权限访问：{rel}") from exc
    except OSError as exc:
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"无法访问 {rel}：{exc}") from exc

    if target.is_dir():
        return {"rel": rel, "name": target.name, "is_dir": True, "size": 0, "mtime": info.st_mtime}

    # 有 mtime/size 不等于能读。POSIX 权限位只影响 open，先试一次再报大小，
    # 免得建完任务、分完块、传到一半才发现读不了。
    try:
        with open(target, "rb") as handle:
            handle.read(1)
    except PermissionError as exc:
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"没有读取权限：{rel}") from exc
    except OSError as exc:
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"无法读取：{rel}：{exc}") from exc

    return {
        "rel": rel,
        "name": target.name,
        "is_dir": False,
        "size": info.st_size,
        "mtime": info.st_mtime,
    }


# --- hash_file ----------------------------------------------------------
#
# A 用它做"上传前判重"：库里已经有同尺寸文件时，先让 B 算一遍 sha256，
# 一致就整份跳过传输。代价是一次全量读盘，收益是几 GB 的上传。
# 只在 A 侧发现同尺寸候选时才会被调用，所以绝大多数文件不会多读这一遍。


async def _hash_file(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    rel = _string(payload, "rel", default="")
    if not rel:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "需要指定文件路径")

    target = ctx.sandbox.resolve(rel)
    return await asyncio.to_thread(_hash_sync, target, rel)


def _hash_sync(target: Path, rel: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(target, "rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    except FileNotFoundError as exc:
        raise AgentCommandError(ErrorCode.FILE_NOT_FOUND, f"文件不存在：{rel}") from exc
    except PermissionError as exc:
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"没有读取权限：{rel}") from exc
    except OSError as exc:
        raise AgentCommandError(ErrorCode.FORBIDDEN, f"无法读取：{rel}：{exc}") from exc

    return {"rel": rel, "sha256": digest.hexdigest(), "size": size}


# --- start_upload / cancel_task -----------------------------------------


async def _start_upload(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    job = UploadJob.from_payload(payload)
    # 先确认文件还在、还读得了，再 ack。否则 ack 完了才发现读不了，
    # A 侧要等一个超时才知道失败。
    stat = await asyncio.to_thread(_stat_sync, ctx.sandbox.resolve(job.rel), job.rel)
    if stat["is_dir"]:
        raise AgentCommandError(ErrorCode.NOT_A_FILE, f"{job.rel} 是目录")
    if int(stat["size"]) != job.total_size:
        raise AgentCommandError(
            ErrorCode.SIZE_CHANGED,
            f"{job.rel} 大小已变化：任务创建时 {job.total_size}，现在 {stat['size']}",
        )

    await ctx.uploads.start(job)
    return {"accepted": True, "upload_id": job.upload_id}


async def _cancel_task(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    upload_id = _string(payload, "upload_id", default="")
    if not upload_id:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "需要 upload_id")
    await ctx.uploads.cancel(upload_id)
    return {"canceled": upload_id}


# --- camera.* -----------------------------------------------------------
#
# 这一组不碰设备：相机凭据与 ISAPI 配置只在 backend 进程里，控制租约也由它签发。
# agent 的职责是"把云端的意图翻译成一次本机调用，并把结果如实回传"。

#: 与 `network_camera.py` 的方向枚举一致。
PTZ_DIRECTIONS = frozenset({"up", "down", "left", "right", "zoom_in", "zoom_out", "stop"})


def _media(ctx: HandlerContext) -> MediaManager:
    if ctx.media is None:
        raise AgentCommandError(ErrorCode.STREAM_DISABLED, "本进程未启用媒体上行")
    return ctx.media


def _backend(ctx: HandlerContext) -> LocalBackend:
    if ctx.backend is None:
        raise AgentCommandError(ErrorCode.INTERNAL, "本进程没有配置本地 backend 地址")
    return ctx.backend


def _media_error(exc: MediaError) -> AgentCommandError:
    try:
        code = ErrorCode(exc.code)
    except ValueError:
        code = ErrorCode.CAMERA_UNAVAILABLE
    return AgentCommandError(code, exc.message)


def _ptz_speed(value: Any) -> float:
    """云端用 1–7 档（和球机 Web 界面一致），驱动要 0–1。"""
    if value is None:
        return 0.5
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "speed 必须是数字") from exc
    number = max(1.0, min(PTZ_SPEED_MAX, number))
    return round(number / PTZ_SPEED_MAX, 3)


async def _lease(ctx: HandlerContext, session_id: str) -> str:
    if ctx.leases is None:
        raise AgentCommandError(ErrorCode.INTERNAL, "本进程没有租约管理器")
    return await ctx.leases.ensure(session_id)


async def _camera_stream_start(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    session_id = _required(payload, "session_id")
    camera_id = _optional_string(payload, "camera_id") or "default"
    profile = _optional_string(payload, "profile") or "low"
    try:
        return await _media(ctx).start(session_id, camera_id, profile)
    except MediaError as exc:
        raise _media_error(exc) from exc


async def _camera_stream_stop(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    session_id = _required(payload, "session_id")
    stopped = await _media(ctx).stop(session_id, "cloud_stop")
    # 会话结束顺手交还控制权：不交还的话，本地人要等 3s 租约到期才能操作，
    # 表现为"退出了云端还在抢控制"。
    if ctx.leases is not None:
        await ctx.leases.release(session_id)
    return {"stopped": stopped, "session_id": session_id}


async def _camera_ptz(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    session_id = _optional_string(payload, "session_id") or ""
    action = (_optional_string(payload, "action") or "start").lower()
    direct = (_optional_string(payload, "direct") or "stop").lower()
    if action == "stop":
        direct = "stop"
    if direct not in PTZ_DIRECTIONS:
        raise AgentCommandError(
            ErrorCode.INVALID_ARGUMENTS, f"不支持的云台方向：{direct!r}"
        )

    lease_id = await _lease(ctx, session_id)
    result = await _backend(ctx).command(
        action="camera.ptz",
        payload={"direct": direct, "speed": _ptz_speed(payload.get("speed"))},
        lease_id=lease_id,
        idempotency_key=_optional_string(payload, "idempotency_key") or new_id(),
        trace_id=_optional_string(payload, "trace_id") or "",
    )
    return {"direct": direct, "state": str(result.get("state") or "succeeded"), "leaseId": lease_id}


async def _camera_preset_list(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    camera_id = _optional_string(payload, "camera_id")
    data = await _backend(ctx).presets(camera_id)
    items = data.get("items") if isinstance(data, dict) else None
    return {"items": list(items or []), "cameraId": data.get("cameraId") if isinstance(data, dict) else None}


async def _camera_preset_goto(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    session_id = _optional_string(payload, "session_id") or ""
    preset = payload.get("preset")
    try:
        preset_id = int(str(preset))
    except (TypeError, ValueError) as exc:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "preset 必须是整数") from exc
    if preset_id < 1:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "preset 必须大于 0")

    lease_id = await _lease(ctx, session_id)
    result = await _backend(ctx).command(
        # 注意：驱动里的动作名是 `camera.goto_preset`，链路上的命令名是
        # `camera.preset.goto`。两层各用各的命名，别在中间层把两者混成一套。
        action="camera.goto_preset",
        payload={"preset": preset_id},
        lease_id=lease_id,
        idempotency_key=_optional_string(payload, "idempotency_key") or new_id(),
        trace_id=_optional_string(payload, "trace_id") or "",
    )
    return {"preset": preset_id, "state": str(result.get("state") or "succeeded")}


async def _camera_snapshot(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    """抓一张并立刻把二进制回传。

    图像本体走 `/internal/snapshot/{request_id}`，这条命令的回包只有元信息：
    控制通道单帧上限 256KB，一张图 base64 之后还要涨 33%，塞进来会挤掉命令队列。
    """
    if ctx.snapshots is None:
        raise AgentCommandError(ErrorCode.INTERNAL, "本进程没有配置快照回传通道")
    request_id = _optional_string(payload, "command_id") or new_id()
    camera_id = _optional_string(payload, "camera_id")
    image, meta = await _backend(ctx).snapshot(camera_id)
    await ctx.snapshots(request_id, image, meta)
    return {
        "request_id": request_id,
        "size": len(image),
        "captured_at": meta.get("captured_at"),
    }


# --- 参数小工具 ---------------------------------------------------------


def _string(payload: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, f"{key} 必须是字符串")
    return value


def _required(payload: dict[str, Any], key: str) -> str:
    value = _optional_string(payload, key)
    if not value:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, f"需要 {key}")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    """取可选字符串：缺省/空值都返回 None，类型不对才算错。"""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, f"{key} 必须是字符串")
    return value.strip() or None


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "limit 必须是整数")
    return max(low, min(high, value))


def _cursor_offset(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        offset = int(str(value))
    except (TypeError, ValueError) as exc:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "cursor 不合法") from exc
    if offset < 0:
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "cursor 不合法")
    return offset


_HANDLERS: dict[CommandName, Callable[[dict[str, Any], HandlerContext], Any]] = {
    CommandName.PING: _ping,
    CommandName.LIST_DIR: _list_dir,
    CommandName.STAT_FILE: _stat_file,
    CommandName.HASH_FILE: _hash_file,
    CommandName.START_UPLOAD: _start_upload,
    CommandName.CANCEL_TASK: _cancel_task,
    CommandName.CAMERA_STREAM_START: _camera_stream_start,
    CommandName.CAMERA_STREAM_STOP: _camera_stream_stop,
    CommandName.CAMERA_PTZ: _camera_ptz,
    CommandName.CAMERA_PRESET_LIST: _camera_preset_list,
    CommandName.CAMERA_PRESET_GOTO: _camera_preset_goto,
    CommandName.CAMERA_SNAPSHOT: _camera_snapshot,
}

# 白名单与实现必须一一对应。少了说明枚举扩容却没写 handler，
# 多了说明有人加了枚举外的命令——启动时就该炸。
assert set(_HANDLERS) == set(AGENT_ALLOWED_COMMANDS), (
    f"handler 与命令白名单不一致：{set(_HANDLERS) ^ set(AGENT_ALLOWED_COMMANDS)}"
)
