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

四条链路各自有 QoS 契约（见 `docs/实时接入平台架构与远程监控实施方案.md` §3.2），
但**只有控制与状态共用这条 C0 连接**：状态帧允许整帧丢弃，命令帧不允许。媒体走
独立的 `/ws/agent/media`（二进制帧头见本文件末尾），事件走 HTTP 批量上报。
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
    HELLO = "hello"          # B -> A：注册，带设备信息、根目录与能力清单
    COMMAND = "command"      # A -> B：下发指令
    RESULT = "result"        # B -> A：指令结果（成功或失败）
    EVENT = "event"          # 双向：主动通知（心跳、取消、上传失败）
    STATE = "state"          # B -> A：设备状态快照。只留最新，可整帧丢弃
    ACK = "ack"              # A -> B：welcome，携带续传清单
    ERROR = "error"

    # --- 媒体链路的文本帧 ---------------------------------------------
    # 这三个只出现在 `/ws/agent/media` 上，不走 C0。放在同一张枚举里是因为
    # 两端都要判断"这一帧该在哪条连接上处理"，分两套枚举只会多一处漂移点。
    # 媒体链路上的**二进制帧**不走 Message：它是 flags+pts+seq+Annex-B，
    # 见文件末尾的 MEDIA_FLAG_*。
    MEDIA_CONFIG = "media_config"      # B -> A：codec / 时间轴基准 / 分辨率 / 档位
    MEDIA_STATS = "media_stats"        # 双向：会话统计（码率、fps、丢帧）
    MEDIA_CONTROL = "media_control"    # A -> B：请求关键帧 / 切档位 / 终止会话


class CommandName(str, Enum):
    """命令枚举（协议词汇表）。

    设计成封闭枚举而非自由字符串是刻意的：B 收到任何不在这张表里的命令都直接
    拒绝。即使 A 被攻破，攻击者能让 B 做的事也只是这个有限集合。

    文件类命令只有读操作。没有 delete、没有 write、没有 run_command——参考项目
    remote-machine-access 的文档里承认了 `run_command` 能绕过路径沙箱
    （`cat /etc/passwd` 即可读全盘），我们不提供这个能力，从设计上消除 RCE 面。

    `camera.*` 是远程监控链路（P1）的命令。设备驱动仍是模拟实现的部分
    （AGV / 升降 / 灯光）**没有**对应命令——不把模拟能力包装成"已支持远程控制"。
    """

    PING = "ping"
    LIST_DIR = "list_dir"
    STAT_FILE = "stat_file"
    HASH_FILE = "hash_file"
    START_UPLOAD = "start_upload"
    CANCEL_TASK = "cancel_task"

    # --- 球形摄像头 -----------------------------------------------------
    # 信封字段统一由 A 填：command_id / session_id / sequence / deadline /
    # idempotency_key / trace_id（见方案 §5.1）。B 侧据此做去重与过期判定。
    CAMERA_STREAM_START = "camera.stream.start"
    CAMERA_STREAM_STOP = "camera.stream.stop"
    CAMERA_PTZ = "camera.ptz"
    CAMERA_PRESET_LIST = "camera.preset.list"
    CAMERA_PRESET_GOTO = "camera.preset.goto"
    CAMERA_SNAPSHOT = "camera.snapshot"        # 回包只带元信息，图像走独立二进制通道
    CAMERA_STATE = "camera.state"              # 读取当前云台/预置点，用于纠正 unknown


#: B 端**当前构建**能执行的命令。
#:
#: 它与 `CommandName` 不是一回事：枚举是协议词汇表（A 可能下发、别的构建可能
#: 实现），这张表是本构建真的能做什么。两者分开，老 agent 收到 `camera.ptz`
#: 时才会回 `unknown_command` 而不是关闭连接——这正是"解析宽松、执行严格"的
#: 落地方式，也是两端能分别升级的前提。
#:
#: `camera.state` 暂列在枚举里但不在这里：云台位置需要相机侧的状态接口
#: （ISAPI ``PTZCtrl/status``），当前部署没有配置它。缺这条时页面应显示
#: "无法确认当前预置点"，而不是假装查到了。P2 补上后再加进来。
AGENT_ALLOWED_COMMANDS: frozenset[CommandName] = frozenset(
    {
        CommandName.PING,
        CommandName.LIST_DIR,
        CommandName.STAT_FILE,
        CommandName.HASH_FILE,
        CommandName.START_UPLOAD,
        CommandName.CANCEL_TASK,
        CommandName.CAMERA_STREAM_START,
        CommandName.CAMERA_STREAM_STOP,
        CommandName.CAMERA_PTZ,
        CommandName.CAMERA_PRESET_LIST,
        CommandName.CAMERA_PRESET_GOTO,
        CommandName.CAMERA_SNAPSHOT,
    }
)


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

    # --- 控制链路 -------------------------------------------------------
    # 这三个是"命令没有明确成功"的三种不同原因，页面上的处置完全不同：
    # expired 说明积压太久（该重发），stale 说明被更新的一条覆盖了（什么都不做），
    # control_held 说明有人正在本地操作（要让操作者退让）。
    EXPIRED = "expired"                         # 超过 deadline，未执行
    STALE = "stale"                             # sequence 落后于已执行序号，丢弃
    CONTROL_HELD = "control_held"               # 无有效租约 / 本地人工优先

    # --- 媒体链路 -------------------------------------------------------
    CAMERA_UNAVAILABLE = "camera_unavailable"   # 相机侧不可达（RTSP/ISAPI 失败）
    STREAM_DISABLED = "stream_disabled"         # 本机未启用低延迟预览


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

    @classmethod
    def state_frame(cls, payload: dict[str, Any]) -> Message:
        """状态快照帧。payload 必须带 `state_version` 与 `observed_at`。

        没有 version 就无法在云端判断新旧，没有 observed_at 就无法在页面上
        显示"数据延迟多少秒"——两者都在 payload 里由生产者填，而不是这一层
        代填，因为只有生产者知道状态是在哪一刻采集的。
        """
        return cls(type=MessageType.STATE, payload=payload)

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


# --- 媒体通道的二进制帧 -------------------------------------------------
#
# 与 `app/video/codec.py`、`robot-frontend/src/composables/useCameraStream.js`
# 三处必须一致，格式刻意沿用已有的本地预览链路，不重新设计：
#
#     flags(1) + pts_ms(4, LE) + seq(4, LE) + Annex-B 载荷
#
# 云端只在机器人出口与浏览器入口之间原样转发，不解析 NAL、不重打包。

MEDIA_HEADER_BYTES = 9

MEDIA_FLAG_KEY = 0x01        # 关键帧（IDR），客户端可在此重建解码器
MEDIA_FLAG_CONFIG = 0x02     # 配置帧（预留）
MEDIA_FLAG_RESET = 0x04      # 上游丢过帧，客户端必须先 reset 解码器
MEDIA_FLAG_GOP_START = 0x08  # 降帧率跳帧后重新起步的首帧（P2 使用）
