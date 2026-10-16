"""robot-core 网关实现（Unix domain socket 上的 JSON Lines 契约）。

迁移来源：RobotCoreGateway.java + UnixSocketRobotCoreGateway.java。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.config import Settings
from app.gateways.base import RobotCoreError, UnixCaller


class RobotCoreGateway:
    """通过 Linux 工控机 UDS 调用 robot-core 进程。"""

    def __init__(self, settings: Settings) -> None:
        self._unix = UnixCaller(
            socket_path=settings.robot_core_socket,
            timeout_seconds=settings.robot_core_timeout,
            unavailable_code="core_unavailable",
            label="Robot core",
            error_cls=RobotCoreError,
        )

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self._unix.call(request)

    async def aclose(self) -> None:
        await self._unix.aclose()


def create_robot_core_gateway(settings: Settings) -> RobotCoreGateway:
    return RobotCoreGateway(settings)
