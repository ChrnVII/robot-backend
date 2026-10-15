"""robot-core 网关实现（unix：UDS JSON Lines；mock：开发联调）。

迁移来源：RobotCoreGateway.java + MockRobotCoreGateway / UnixSocketRobotCoreGateway。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.config import Settings
from app.gateways.base import RobotCoreError, UnixCaller
from app.gateways.mock import MockRobotCoreGateway


class RobotCoreGateway:
    """按配置选择传输：transport=mock（默认）或 unix（Linux 工控机 UDS）。"""

    def __init__(self, settings: Settings) -> None:
        self._mock = MockRobotCoreGateway()
        self._unix: UnixCaller | None = None
        transport = (settings.robot_core_transport or "mock").lower()
        if transport == "unix":
            self._unix = UnixCaller(
                socket_path=settings.robot_core_socket,
                timeout_seconds=settings.robot_core_timeout,
                unavailable_code="core_unavailable",
                label="Robot core",
                error_cls=RobotCoreError,
            )
        elif transport != "mock":
            raise ValueError(f"unsupported robot_core transport: {transport}")

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self._unix is not None:
            return await self._unix.call(request)
        return await self._mock.call(request)


def create_robot_core_gateway(settings: Settings) -> RobotCoreGateway:
    return RobotCoreGateway(settings)
