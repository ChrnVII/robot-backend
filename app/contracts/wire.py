"""IPC 消息契约：操作名常量与控制域请求载荷模型。

后端（services/gateways）用 ``to_payload()`` 组装请求，robot_core 进程用
``from_mapping()`` 解析请求——两侧从此共享同一份字段定义，拼写错误在
解析时即抛 :class:`WireError`（映射为 ``INVALID_REQUEST``），而不是在
深层业务逻辑里以随机 KeyError 的形式暴露。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class WireError(ValueError):
    """IPC 载荷不符合契约（缺失字段 / 非法值）。"""


class Op:
    """UDS JSON Lines 线协议的 operation 常量（双端共用，防止拼写漂移）。"""
    # robot-core
    HEALTH = "health"
    DEVICE_STATE = "device.state"
    LEASE_ACQUIRE = "lease.acquire"
    LEASE_RELEASE = "lease.release"
    COMMAND = "command"
    EMERGENCY_STOP = "emergency.stop"
    ASSET_REGISTER = "asset.register"
    INSPECTION_PERSIST = "inspection.persist"
    # sensor-worker
    CAPTURE = "capture"
    # 历史文档写作 worker.health，各 worker 进程实现与调用方实际使用 "health"；
    # 此处以实现为准统一，避免「按文档改名即断链」。
    WORKER_HEALTH = "health"
    # inspection-worker
    TASK_START = "start"
    TASK_STOP = "stop"
    TASK_STATUS = "status"
    TASK_DETECTIONS = "detections"
    TASK_LATEST_FRAME = "latest_frame"
    TASK_LATEST_FRAMES = "latest_frames"
    TASK_RESOLVE_IMAGE = "resolve_image"
    TASK_SNAPSHOT = "snapshot"


class Channels:
    """PostgreSQL NOTIFY 通道常量（robot_core 发布 / 后端 LISTEN 订阅）。"""

    #: robot_core 写入设备状态、命令完成、推理结果后在此广播变更事件。
    EVENTS = "robot_events"


#: 事件载荷的 topic 取值（双端共用）。
class EventTopic:
    STATE = "state"
    ENVIRONMENT = "environment"
    INSPECTION = "inspection"


def _require(value: Mapping[str, Any], key: str) -> Any:
    if key not in value or value[key] is None:
        raise WireError(f"missing required field: {key}")
    return value[key]


def _optional_str(value: Mapping[str, Any], key: str, default: str) -> str:
    item = value.get(key)
    return str(item) if item is not None else default


@dataclass(frozen=True)
class LeaseAcquireWire:
    """lease.acquire 请求载荷。"""

    holder: str
    mode: str

    def to_payload(self) -> dict[str, Any]:
        return {"operation": Op.LEASE_ACQUIRE, "holder": self.holder, "mode": self.mode}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LeaseAcquireWire":
        return cls(holder=str(_require(value, "holder")), mode=str(_require(value, "mode")))


@dataclass(frozen=True)
class LeaseReleaseWire:
    """lease.release 请求载荷（reason 缺省 released，兼容旧行为）。"""

    lease_id: str
    reason: str = "released"

    def to_payload(self) -> dict[str, Any]:
        return {"operation": Op.LEASE_RELEASE, "lease_id": self.lease_id, "reason": self.reason}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LeaseReleaseWire":
        return cls(
            lease_id=str(_require(value, "lease_id")),
            reason=_optional_str(value, "reason", "released"),
        )


@dataclass(frozen=True)
class CommandWire:
    """command 请求载荷：字段集与旧线协议逐字一致（snake_case）。"""

    target: str
    action: str
    issuer: str
    lease_id: str
    idempotency_key: str
    trace_id: str
    issued_at: str
    expires_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation": Op.COMMAND,
            "target": self.target,
            "action": self.action,
            "issuer": self.issuer,
            "lease_id": self.lease_id,
            "idempotency_key": self.idempotency_key,
            "trace_id": self.trace_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        default_issued_at: str | None = None,
        default_expires_at: str | None = None,
    ) -> "CommandWire":
        """解析命令载荷；issued_at / expires_at 缺省时使用调用方传入的默认值。

        robot-core 侧以「当前时间 + 配置 TTL」作为默认，保持旧实现语义。
        字段按契约顺序校验，缺失时优先报出第一个缺失字段而非时间戳。
        """
        target = str(_require(value, "target"))
        action = str(_require(value, "action"))
        issuer = str(_require(value, "issuer"))
        lease_id = str(_require(value, "lease_id"))
        idempotency_key = str(_require(value, "idempotency_key"))
        trace_id = str(_require(value, "trace_id"))
        issued_at = value.get("issued_at") or default_issued_at
        expires_at = value.get("expires_at") or default_expires_at
        if not issued_at or not expires_at:
            raise WireError("missing required field: issued_at/expires_at")
        return cls(
            target=target,
            action=action,
            issuer=issuer,
            lease_id=lease_id,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            issued_at=str(issued_at),
            expires_at=str(expires_at),
            payload=dict(value.get("payload") or {}),
        )


@dataclass(frozen=True)
class EmergencyStopWire:
    """emergency.stop 请求载荷（reason 缺省 requested，兼容旧行为）。"""

    reason: str
    trace_id: str

    def to_payload(self) -> dict[str, Any]:
        return {"operation": Op.EMERGENCY_STOP, "reason": self.reason, "trace_id": self.trace_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EmergencyStopWire":
        return cls(
            reason=_optional_str(value, "reason", "requested"),
            trace_id=str(_require(value, "trace_id")),
        )
