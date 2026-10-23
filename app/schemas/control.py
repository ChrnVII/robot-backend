"""控制域出入参：LeaseRequest、CommandRequest、EmergencyStopRequest（camelCase 线上契约）。

校验规则对齐旧 Java DTO（jakarta validation）：长度、枚举与 expiresAt > issuedAt。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ControlMode = Literal["maintenance", "manual_local", "manual_remote", "automatic", "emergency"]


class LeaseRequest(BaseModel):
    holder: str = Field(min_length=1, max_length=128)
    mode: ControlMode


class CommandRequest(BaseModel):
    target: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    issuer: str = Field(min_length=1, max_length=128)
    leaseId: str = Field(min_length=1, max_length=128)
    idempotencyKey: str = Field(min_length=1, max_length=256)
    traceId: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    issuedAt: datetime | None = None
    expiresAt: datetime | None = None

    @model_validator(mode="after")
    def _validate_expiry(self) -> "CommandRequest":
        if self.issuedAt is not None and self.expiresAt is not None and self.expiresAt <= self.issuedAt:
            raise ValueError("expiresAt must be after issuedAt")
        return self


class EmergencyStopRequest(BaseModel):
    traceId: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="requested", max_length=512)

    @model_validator(mode="after")
    def _default_reason(self) -> "EmergencyStopRequest":
        if not self.reason.strip():
            self.reason = "requested"
        return self


class EmergencyResetRequest(BaseModel):
    """解除急停锁。

    急停期间普通租约已被 robot-core 抢占，因此 leaseId 可选；显式提供时
    robot-core 仍会校验其有效性，避免复位变成绕过控制权仲裁的通道。
    """

    traceId: str = Field(min_length=1, max_length=128)
    reason: str = Field(default="operator_reset", max_length=512)
    leaseId: str | None = Field(default=None, max_length=128)
