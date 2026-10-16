"""控制服务：租约申请/释放、设备命令、急停转发（迁移 RobotApplicationService 控制部分）。

IPC 载荷统一经 app.contracts 契约模型组装（字段定义双端共用），
服务层不再手拼 dict。
"""

from __future__ import annotations

from datetime import timedelta

from app.common.clock import iso_z, utc_now
from app.contracts import (
    CommandWire,
    EmergencyStopWire,
    LeaseAcquireWire,
    LeaseReleaseWire,
)
from app.gateways.base import camelize
from app.gateways.robot_core import RobotCoreGateway
from app.schemas.control import CommandRequest, EmergencyStopRequest, LeaseRequest


class ControlService:
    def __init__(self, gateway: RobotCoreGateway) -> None:
        self._gateway = gateway

    async def acquire_lease(self, request: LeaseRequest) -> dict:
        wire = LeaseAcquireWire(holder=request.holder, mode=request.mode)
        return camelize(await self._gateway.call(wire.to_payload()))

    async def release_lease(self, lease_id: str, reason: str | None) -> dict:
        wire = LeaseReleaseWire(lease_id=lease_id, reason=reason or "released")
        return camelize(await self._gateway.call(wire.to_payload()))

    async def execute_command(self, request: CommandRequest) -> dict:
        issued_at = request.issuedAt or utc_now()
        expires_at = request.expiresAt or (issued_at + timedelta(seconds=1))
        wire = CommandWire(
            target=request.target,
            action=request.action,
            issuer=request.issuer,
            lease_id=request.leaseId,
            idempotency_key=request.idempotencyKey,
            trace_id=request.traceId,
            issued_at=iso_z(issued_at),
            expires_at=iso_z(expires_at),
            payload=request.payload,
        )
        return camelize(await self._gateway.call(wire.to_payload()))

    async def emergency_stop(self, request: EmergencyStopRequest) -> dict:
        wire = EmergencyStopWire(reason=request.reason, trace_id=request.traceId)
        return camelize(await self._gateway.call(wire.to_payload()))
