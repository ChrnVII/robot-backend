"""IPC 线协议共享契约：后端与 robot_core 双端的单一事实来源。

本包必须保持零依赖（不得 import app.* 其他模块），因为它是 FastAPI 后端与
robot_core 五进程共同引用的「合同层」——历史上两侧各自维护心中的契约
（后端手拼 dict、对端靠 request["字段名"] 直取、错误码透传 Python 异常类名），
拼写错误只能在运行时暴露。契约核收拢三件事：

- 稳定错误码（``errors``）：替代「异常类名即错误码」的脆弱透传；
- 操作名常量与请求载荷模型（``wire``）：command / lease / emergency.stop 等
  控制域 IPC 消息在此一处定义、双端共用。

分层规则更新：``app/contracts`` 是唯一允许被 api/services 与 robot_core 同时
import 的共享内核；除此之外 ``api → services → gateways → ipc →(UDS)→ robot_core``
的既有分层与「api/services 不得直接 import robot_core 内部实现」规则不变。
"""

from app.contracts.errors import (
    LEGACY_NOT_FOUND_CODES,
    IpcErrorCode,
    ipc_error_code,
    is_not_found_code,
)
from app.contracts.wire import (
    Channels,
    CommandWire,
    DeviceQueryWire,
    InspectionRequestWire,
    EmergencyResetWire,
    EmergencyStopWire,
    EventTopic,
    LeaseAcquireWire,
    LeaseReleaseWire,
    Op,
    WireError,
)

__all__ = [
    "Channels",
    "CommandWire",
    "DeviceQueryWire",
    "InspectionRequestWire",
    "EmergencyResetWire",
    "EmergencyStopWire",
    "EventTopic",
    "IpcErrorCode",
    "LEGACY_NOT_FOUND_CODES",
    "LeaseAcquireWire",
    "LeaseReleaseWire",
    "Op",
    "WireError",
    "ipc_error_code",
    "is_not_found_code",
]
