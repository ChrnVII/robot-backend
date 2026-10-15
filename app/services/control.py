"""控制服务：租约申请/释放、设备命令、急停转发（迁移 RobotApplicationService 控制部分）。"""

from __future__ import annotations

from datetime import timedelta

from app.common.clock import iso_z, utc_now
from app.gateways.base import camelize
from app.gateways.robot_core import RobotCoreGateway
from app.schemas.control import CommandRequest, EmergencyStopRequest, LeaseRequest


class ControlService:
    def __init__(self, gateway: RobotCoreGateway) -> None:
        self._gateway = gateway

    async def acquire_lease(self, request: LeaseRequest) -> dict:
        return camelize(await self._gateway.call({
            "operation": "lease.acquire",
            "holder": request.holder,
            "mode": request.mode,
        }))

    async def release_lease(self, lease_id: str, reason: str | None) -> dict:
        return camelize(await self._gateway.call({
            "operation": "lease.release",
            "lease_id": lease_id,
            "reason": reason or "released",
        }))

    async def execute_command(self, request: CommandRequest) -> dict:
        issued_at = request.issuedAt or utc_now()
        expires_at = request.expiresAt or (issued_at + timedelta(seconds=1))
        return camelize(await self._gateway.call({
            "operation": "command",
            "target": request.target,
            "action": request.action,
            "issuer": request.issuer,
            "lease_id": request.leaseId,
            "idempotency_key": request.idempotencyKey,
            "trace_id": request.traceId,
            "payload": request.payload,
            "issued_at": iso_z(issued_at),
            "expires_at": iso_z(expires_at),
        }))

    async def emergency_stop(self, request: EmergencyStopRequest) -> dict:
        return camelize(await self._gateway.call({
            "operation": "emergency.stop",
            "trace_id": request.traceId,
            "reason": request.reason,
        }))
