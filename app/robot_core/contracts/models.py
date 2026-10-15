from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from app.robot_core.common import new_id, parse_utc, utc_now


class ContractError(ValueError):
    pass


class HealthState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    FAULT = "fault"


INFERENCE_STATES = {
    "ok", "uncertain", "invalid_input", "capability_unavailable",
    "model_unhealthy", "resource_exhausted", "timeout", "internal_error",
}
INFERENCE_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class DeviceState:
    device_id: str
    health: HealthState
    sampled_at: str
    source: str
    values: Mapping[str, Any] = field(default_factory=dict)
    quality: str = "valid"
    raw_fault_code: Optional[str] = None

    def __post_init__(self) -> None:
        parse_utc(self.sampled_at)
        if not self.device_id or not self.source:
            raise ContractError("device_id and source are required")

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["health"] = self.health.value
        return value


@dataclass(frozen=True)
class ControlLease:
    lease_id: str
    holder: str
    mode: str
    priority: int
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        if self.mode not in {"maintenance", "manual_local", "manual_remote", "automatic", "emergency"}:
            raise ContractError("unsupported control mode")
        if parse_utc(self.expires_at) <= parse_utc(self.issued_at):
            raise ContractError("lease expires_at must be after issued_at")


@dataclass(frozen=True)
class DeviceCommand:
    target: str
    action: str
    payload: Mapping[str, Any]
    issuer: str
    lease_id: str
    issued_at: str
    expires_at: str
    command_id: str = field(default_factory=lambda: new_id("cmd"))
    idempotency_key: str = field(default_factory=lambda: new_id("idem"))
    trace_id: str = field(default_factory=lambda: new_id("trace"))

    def __post_init__(self) -> None:
        if not all((self.target, self.action, self.issuer, self.lease_id, self.idempotency_key)):
            raise ContractError("command identifiers are required")
        if parse_utc(self.expires_at) <= parse_utc(self.issued_at):
            raise ContractError("command expires_at must be after issued_at")


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    state: str
    completed_at: str = field(default_factory=utc_now)
    details: Mapping[str, Any] = field(default_factory=dict)
    reject_reason: Optional[str] = None


@dataclass(frozen=True)
class CaptureRequest:
    task_run_id: str
    waypoint_id: str
    device_id: str
    media_type: str
    request_id: str = field(default_factory=lambda: new_id("request"))
    trace_id: str = field(default_factory=lambda: new_id("trace"))
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InferenceRequest:
    capability: str
    asset_id: str
    task_run_id: str
    request_id: str = field(default_factory=lambda: new_id("request"))
    trace_id: str = field(default_factory=lambda: new_id("trace"))
    context: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = INFERENCE_CONTRACT_VERSION
    deadline_at: Optional[str] = None
    model_id: Optional[str] = None
    model_version: Optional[str] = None
    idempotency_key: str = field(default_factory=lambda: new_id("infer-idem"))

    def __post_init__(self) -> None:
        if self.schema_version != INFERENCE_CONTRACT_VERSION:
            raise ContractError(f"unsupported inference request schema: {self.schema_version}")
        if not all((self.capability, self.asset_id, self.task_run_id, self.request_id,
                    self.trace_id, self.idempotency_key)):
            raise ContractError("inference request identifiers are required")
        if self.deadline_at:
            parse_utc(self.deadline_at)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InferenceRequest":
        return cls(
            capability=str(value["capability"]), asset_id=str(value["asset_id"]),
            task_run_id=str(value["task_run_id"]), request_id=str(value["request_id"]),
            trace_id=str(value["trace_id"]), context=dict(value.get("context", {})),
            schema_version=int(value.get("schema_version", INFERENCE_CONTRACT_VERSION)),
            deadline_at=value.get("deadline_at"), model_id=value.get("model_id"),
            model_version=value.get("model_version"),
            idempotency_key=str(value["idempotency_key"]),
        )


@dataclass(frozen=True)
class InferenceResponse:
    request_id: str
    capability: str
    status: str
    model_version: str
    result: Mapping[str, Any] = field(default_factory=dict)
    evidence_asset_ids: tuple[str, ...] = ()
    error_stage: Optional[str] = None
    suggested_action: Optional[str] = None
    finished_at: str = field(default_factory=utc_now)
    schema_version: int = INFERENCE_CONTRACT_VERSION
    trace_id: str = field(default_factory=lambda: new_id("trace"))
    model_id: Optional[str] = None
    model_sha256: Optional[str] = None
    started_at: Optional[str] = None
    latency_ms: Optional[float] = None
    result_schema: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in INFERENCE_STATES:
            raise ContractError(f"unsupported inference status: {self.status}")
        if self.schema_version != INFERENCE_CONTRACT_VERSION:
            raise ContractError(f"unsupported inference response schema: {self.schema_version}")
        if not self.request_id or not self.capability or not self.trace_id:
            raise ContractError("inference response identifiers are required")
        parse_utc(self.finished_at)
        if self.started_at:
            parse_utc(self.started_at)
        if self.status == "ok" and (not self.model_id or not self.model_version or not self.result_schema):
            raise ContractError("successful inference response requires model and result schema identity")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InferenceResponse":
        return cls(
            request_id=str(value["request_id"]), capability=str(value["capability"]),
            status=str(value["status"]), model_version=str(value["model_version"]),
            result=dict(value.get("result", {})),
            evidence_asset_ids=tuple(str(item) for item in value.get("evidence_asset_ids", ())),
            error_stage=value.get("error_stage"), suggested_action=value.get("suggested_action"),
            finished_at=str(value.get("finished_at", utc_now())),
            schema_version=int(value.get("schema_version", INFERENCE_CONTRACT_VERSION)),
            trace_id=str(value.get("trace_id") or new_id("trace")), model_id=value.get("model_id"),
            model_sha256=value.get("model_sha256"), started_at=value.get("started_at"),
            latency_ms=float(value["latency_ms"]) if value.get("latency_ms") is not None else None,
            result_schema=value.get("result_schema"),
        )


@dataclass(frozen=True)
class Event:
    event_type: str
    source: str
    trace_id: str
    data: Mapping[str, Any]
    event_id: str = field(default_factory=lambda: new_id("event"))
    occurred_at: str = field(default_factory=utc_now)
