"""指令处理器：ping / list_dir / stat_file / hash_file / start_upload / cancel_task。

纪律：

- **所有文件系统调用都走 `asyncio.to_thread`。** `os.scandir` 在万级目录上要
  几百毫秒，网络挂载上可能几秒。直接写在协程里会卡住事件循环——心跳发不出去，
  A 判离线，正在传的文件被打断。
- **所有失败都翻译成明确的错误码。** 权限问题必须是 `forbidden` 而不是
  `internal`，否则前端分不清"该换个文件"还是"服务坏了"。
- **只读。** 整个文件里不存在 `"w"`、`"a"`、`"r+"` 打开文件的地方。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AgentCommandError
from .protocol import AGENT_ALLOWED_COMMANDS, CommandName, ErrorCode
from .sandbox import SEP, Sandbox
from .uploader import UploadJob, UploadManager

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


@dataclass
class HandlerContext:
    sandbox: Sandbox
    uploads: UploadManager


async def dispatch(command: CommandName, payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    handler = _HANDLERS.get(command)
    if handler is None:
        # command_name 已经保证在枚举内，这里再挡一次：
        # 枚举将来扩容但忘写 handler 时，行为是"明确拒绝"而不是 KeyError。
        raise AgentCommandError(ErrorCode.UNKNOWN_COMMAND, f"命令暂未实现：{command.value}")
    return await handler(payload, ctx)


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


# --- 参数小工具 ---------------------------------------------------------


def _string(payload: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, f"{key} 必须是字符串")
    return value


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
}

# 白名单与实现必须一一对应。少了说明枚举扩容却没写 handler，
# 多了说明有人加了枚举外的命令——启动时就该炸。
assert set(_HANDLERS) == set(AGENT_ALLOWED_COMMANDS), (
    f"handler 与命令白名单不一致：{set(_HANDLERS) ^ set(AGENT_ALLOWED_COMMANDS)}"
)
