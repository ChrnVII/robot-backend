"""机器人状态服务：聚合 robot-core 健康与设备状态（迁移 RobotApplicationService.java）。"""

from __future__ import annotations

import re

from app.common.config import Settings
from app.gateways.base import RobotCoreError, camelize
from app.gateways.robot_core import RobotCoreGateway

_DEVICE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RobotService:
    def __init__(self, gateway: RobotCoreGateway, settings: Settings) -> None:
        self._gateway = gateway
        self._settings = settings

    @property
    def robot_id(self) -> str:
        return self._settings.robot_id

    @property
    def mode(self) -> str:
        return self._settings.mode

    async def robot_state(self) -> dict:
        return camelize(await self._gateway.call({"operation": "health"}))

    async def device_state(self, device_id: str) -> dict:
        if not _DEVICE_ID.fullmatch(device_id):
            raise RobotCoreError("invalid_device_id", "Device id contains unsupported characters")
        return camelize(await self._gateway.call({"operation": "device.state", "device_id": device_id}))

    async def core_status(self) -> str:
        """供 /healthz 使用：ok / unavailable（对齐旧 RobotController.health）。"""
        try:
            await self.robot_state()
        except RobotCoreError:
            return "unavailable"
        return "ok"
