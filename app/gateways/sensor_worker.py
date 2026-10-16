"""sensor-worker 网关实现（采集请求，超时按 65s 量级配置）。

迁移来源：SensorWorkerGateway.java + UnixSocketSensorWorkerGateway.java。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.config import Settings
from app.gateways.base import SensorWorkerError, UnixCaller


class SensorWorkerGateway:
    """通过 Linux 工控机 UDS 调用 sensor-worker 进程。"""

    def __init__(self, settings: Settings) -> None:
        self._unix = UnixCaller(
            socket_path=settings.sensor_worker_socket,
            timeout_seconds=settings.sensor_worker_timeout,
            unavailable_code="sensor_unavailable",
            label="Sensor worker",
            error_cls=SensorWorkerError,
        )

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self._unix.call(request)

    async def aclose(self) -> None:
        await self._unix.aclose()


def create_sensor_worker_gateway(settings: Settings) -> SensorWorkerGateway:
    return SensorWorkerGateway(settings)
