"""控制服务：租约申请/释放、设备命令、急停与复位、只读设备查询（迁移 RobotApplicationService 控制部分）。

IPC 载荷统一经 app.contracts 契约模型组装（字段定义双端共用），
服务层不再手拼 dict。
"""

from __future__ import annotations

import base64
import binascii
from datetime import timedelta

from app.common.clock import iso_z, utc_now
from app.common.config import Settings
from app.contracts import (
    CommandWire,
    DeviceQueryWire,
    EmergencyResetWire,
    EmergencyStopWire,
    LeaseAcquireWire,
    LeaseReleaseWire,
)
from app.gateways.base import GatewayError, camelize
from app.gateways.robot_core import RobotCoreGateway
from app.schemas.control import CommandRequest, EmergencyResetRequest, EmergencyStopRequest, LeaseRequest

#: 服务端盖章的命令有效期上下限（毫秒），防止配置写错把命令锁死或放得过宽。
_MIN_TTL_MS = 1000
_MAX_TTL_MS = 30_000


class ControlService:
    def __init__(self, gateway: RobotCoreGateway, settings: Settings | None = None) -> None:
        self._gateway = gateway
        self._settings = settings

    @property
    def command_ttl_ms(self) -> int:
        configured = int(getattr(self._settings, "command_ttl_ms", 5000) or 5000)
        return max(_MIN_TTL_MS, min(_MAX_TTL_MS, configured))

    async def acquire_lease(self, request: LeaseRequest) -> dict:
        wire = LeaseAcquireWire(holder=request.holder, mode=request.mode)
        return camelize(await self._gateway.call(wire.to_payload()))

    async def release_lease(self, lease_id: str, reason: str | None) -> dict:
        wire = LeaseReleaseWire(lease_id=lease_id, reason=reason or "released")
        return camelize(await self._gateway.call(wire.to_payload()))

    async def execute_command(self, request: CommandRequest) -> dict:
        """下发设备命令。

        时间戳一律由服务端生成：客户端时间戳会随浏览器/工控机的时钟漂移
        （离线机器人很常见），一旦快过 robot-core 一秒以上，所有命令都会以
        command_expired 被拒——这正是「云台点了没反应」的隐藏成因之一。
        调用方若显式传入更早的 expiresAt，仍然尊重（刻意要求提前失效）。
        """
        issued_at = utc_now()
        expires_at = issued_at + timedelta(milliseconds=self.command_ttl_ms)
        if request.expiresAt is not None and request.issuedAt is not None:
            expires_at = min(expires_at, request.expiresAt)
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

    async def reset_emergency(self, request: EmergencyResetRequest) -> dict:
        wire = EmergencyResetWire(
            trace_id=request.traceId,
            reason=request.reason or "operator_reset",
            lease_id=request.leaseId,
        )
        return camelize(await self._gateway.call(wire.to_payload()))

    async def query_device(self, device_id: str, query: str, params: dict | None = None) -> dict:
        wire = DeviceQueryWire(device_id=device_id, query=query, params=dict(params or {}))
        return camelize(await self._gateway.call(wire.to_payload()))

    async def camera_presets(self) -> list[dict]:
        """相机端预置点（唯一事实来源）；失败由调用方决定是否回落到本地缓存。"""
        result = await self.query_device("camera", "presets")
        presets = result.get("presets")
        return list(presets) if isinstance(presets, list) else []

    async def camera_snapshot(self) -> tuple[bytes, dict]:
        """抓一张 JPEG。返回 (图像字节, 元信息)。

        base64 只在 robot-core → backend 这一段存在（JSON IPC 过不了二进制），
        到这里立刻解回字节，向上（HTTP 响应、云端回传）全是二进制。
        """
        result = await self.query_device("camera", "snapshot")
        encoded = result.get("jpegBase64") or result.get("jpeg_base64")
        if not isinstance(encoded, str) or not encoded:
            raise GatewayError("camera returned no image data")
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise GatewayError(f"camera image is not valid base64: {exc}") from exc
        meta = {
            "size": int(result.get("size") or len(image)),
            "captured_at": result.get("capturedAt") or result.get("captured_at"),
            "source": result.get("source") or "camera",
        }
        return image, meta
