"""sensor-worker 网关实现（采集请求，超时按 65s 量级配置）。

迁移来源：SensorWorkerGateway.java + MockSensorWorkerGateway / UnixSocketSensorWorkerGateway。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.config import Settings
from app.gateways.base import SensorWorkerError, UnixCaller
from app.gateways.mock import MockSensorWorkerGateway


class SensorWorkerGateway:
    """按配置选择传输：transport=mock（默认）或 unix（Linux 工控机 UDS）。"""

    def __init__(self, settings: Settings) -> None:
        self._mock = MockSensorWorkerGateway()
        self._unix: UnixCaller | None = None
        transport = (settings.sensor_worker_transport or "mock").lower()
        if transport == "unix":
            self._unix = UnixCaller(
                socket_path=settings.sensor_worker_socket,
                timeout_seconds=settings.sensor_worker_timeout,
                unavailable_code="sensor_unavailable",
                label="Sensor worker",
                error_cls=SensorWorkerError,
            )
        elif transport != "mock":
            raise ValueError(f"unsupported sensor_worker transport: {transport}")

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self._unix is not None:
            return await self._unix.call(request)
        return await self._mock.call(request)


def create_sensor_worker_gateway(settings: Settings) -> SensorWorkerGateway:
    return SensorWorkerGateway(settings)
