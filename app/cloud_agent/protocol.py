"""线上协议：消息帧、命令枚举、错误码。

**这份文件在 server/backend 与 agent 各有一份，内容必须完全一致。**

为什么不抽成共享包：这个协议总共 200 行，抽成独立包要付出三个具体成本——
两个项目的 pyproject 都要写 path 依赖、给 B 装 agent 要装两个 wheel、改一行注释
要动第三个项目。复制只带来一个风险：两端漂移。

漂移由两道测试挡住，两边都会跑：

1. `test_protocol_copy_is_identical` —— 逐字节比对两份源码。它保证"改协议必须
   同时改两边"，不靠人记得。
2. `test_golden_frame_roundtrip` —— 两端都读仓库根的 `contract/protocol-frames.json`，
   断言自己的解析与序列化结果与样例一致。即使有人把两份都改了，只要改出的形状
   不同，也过不了。

帧是 JSON 而不是二进制：可读、可 grep、可贴进日志。控制通道上每秒最多几十帧，
这点序列化开销无关紧要。大块数据不走这里，走 HTTP。
"""

from __future__ import annotations

import json
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 协议版本。两端不匹配直接拒绝连接，而不是让缺失字段在运行时变成一个 None。
PROTOCOL_VERSION = 1

# 控制通道单帧上限。超过就是设计错了——文件数据不该出现在这条连接上。
MAX_FRAME_BYTES = 256 * 1024


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class MessageType(str, Enum):
    HELLO = "hello"          # B -> A：注册，带设备信息与根目录
    COMMAND = "command"      # A -> B：下发指令
    RESULT = "result"        # B -> A：指令结果（成功或失败）
    EVENT = "event"          # 双向：主动通知（心跳、取消、上传失败）
    ACK = "ack"              # A -> B：welcome，携带续传清单
    ERROR = "error"


class CommandName(str, Enum):
    """命令枚举。

    设计成封闭枚举而非自由字符串是刻意的：B 收到任何不在这张表里的命令都直接
    拒绝。即使 A 被攻破，攻击者能让 B 做的事也只是这个有限集合。

    本系统只有读操作。没有 delete、没有 write、没有 run_command——参考项目
    remote-machine-access 的文档里承认了 `run_command` 能绕过路径沙箱
    （`cat /etc/passwd` 即可读全盘），我们不提供这个能力，从设计上消除 RCE 面。
    """

    PING = "ping"
    LIST_DIR = "list_dir"
    STAT_FILE = "stat_file"
    HASH_FILE = "hash_file"
    START_UPLOAD = "start_upload"
    CANCEL_TASK = "cancel_task"


# B 端可以执行的命令白名单。任何不在其中的 command 一律 unknown_command。
AGENT_ALLOWED_COMMANDS: frozenset[CommandName] = frozenset(CommandName)


class ErrorCode(str, Enum):
    """错误码。

    每个失败都必须带明确的 code，不能只回一句人话。前端要靠 code 决定是显示
    "重试"还是"这台机器路径不对"；人话是给人看的，code 是给程序用的。
    """

    UNAUTHORIZED = "unauthorized"
    AGENT_OFFLINE = "agent_offline"
    UNKNOWN_COMMAND = "unknown_command"
    FORBIDDEN = "forbidden"                     # 命令合法，但目标被沙箱拒绝
    INVALID_ARGUMENTS = "invalid_arguments"
    FILE_NOT_FOUND = "file_not_found"
    NOT_A_FILE = "not_a_file"
    SIZE_CHANGED = "size_changed"               # 传输期间文件被追加/截断
    CHECKSUM_MISMATCH = "checksum_mismatch"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    INSUFFICIENT_SPACE = "insufficient_space"   # A 侧磁盘余量不够，建任务前就拒绝
    INTERNAL = "internal"


class ProtocolError(Exception):
    """帧无法解析，或不符合本协议版本。"""


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str


class Message(BaseModel):
    """一帧。

    `id` 让请求与响应配对——WebSocket 本身没有请求/响应语义，这一层必须自己造。
    序列化时丢弃 None 字段，帧更小、日志更干净。
    """

    model_config = ConfigDict(extra="forbid")

    v: int = PROTOCOL_VERSION
    type: MessageType
    id: str = Field(default_factory=new_id)
    # command 故意是裸字符串，不是 CommandName 枚举。
    # 原因：B 收到一个它不认识的新命令时，必须能回 `unknown_command`，而不是在
    # 校验阶段就解析失败、把整条连接关掉。封闭性由"分发时对照白名单"保证——
    # 解析宽松、执行严格，这个组合才允许两端分别升级。
    command: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    ok: bool | None = None
    error: ErrorDetail | None = None
    agent_id: str | None = None
    ts: float = Field(default_factory=time.time)

    # --- 构造快捷方式 ---------------------------------------------------

    @classmethod
    def command_frame(
        cls, command: CommandName, payload: dict[str, Any] | None = None, id: str | None = None
    ) -> Message:
        return cls(
            type=MessageType.COMMAND,
            command=command.value,
            payload=payload or {},
            id=id or new_id(),
        )

    @classmethod
    def result_frame(cls, request_id: str, payload: dict[str, Any] | None = None) -> Message:
        return cls(type=MessageType.RESULT, id=request_id, ok=True, payload=payload or {})

    @classmethod
    def error_frame(cls, request_id: str, code: ErrorCode, message: str) -> Message:
        return cls(
            type=MessageType.RESULT,
            id=request_id,
            ok=False,
            error=ErrorDetail(code=code, message=message),
        )

    @classmethod
    def hello_frame(cls, agent_id: str, payload: dict[str, Any]) -> Message:
        return cls(type=MessageType.HELLO, agent_id=agent_id, payload=payload)

    @property
    def command_name(self) -> CommandName | None:
        """把线上字符串还原成枚举；不认识就返回 None（而不是抛错）。"""
        if self.command is None:
            return None
        try:
            return CommandName(self.command)
        except ValueError:
            return None

    # --- 序列化 ---------------------------------------------------------

    def to_json(self) -> str:
        # mode="json" 让枚举落成字符串值；exclude_none 让帧保持精简。
        data = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def parse(cls, raw: str | bytes) -> Message:
        if isinstance(raw, bytes):
            if len(raw) > MAX_FRAME_BYTES:
                raise ProtocolError(f"frame too large: {len(raw)} bytes")
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("frame is not valid utf-8") from exc

        size = len(raw.encode("utf-8"))
        if size > MAX_FRAME_BYTES:
            raise ProtocolError(f"frame too large: {size} bytes")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"malformed json: {exc.msg} at {exc.pos}") from exc

        if not isinstance(data, dict):
            raise ProtocolError("frame must be a json object")

        version = data.get("v")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                f"unsupported protocol version {version!r}, this build speaks {PROTOCOL_VERSION}"
            )

        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first["loc"]) or "<root>"
            raise ProtocolError(f"invalid frame at {loc}: {first['msg']}") from exc
