"""inspection-worker 网关实现（巡检任务、检测分页与证据图解析）。

迁移来源：InspectionWorkerGateway.java + MockInspectionWorkerGateway /
UnixSocketInspectionWorkerGateway。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.config import Settings
from app.gateways.base import InspectionWorkerError, UnixCaller
from app.gateways.mock import MockInspectionWorkerGateway


class InspectionWorkerGateway:
    """按配置选择传输：transport=mock（默认）或 unix（Linux 工控机 UDS）。"""

    def __init__(self, settings: Settings) -> None:
        self._mock = MockInspectionWorkerGateway()
        self._unix: UnixCaller | None = None
        transport = (settings.inspection_worker_transport or "mock").lower()
        if transport == "unix":
            self._unix = UnixCaller(
                socket_path=settings.inspection_worker_socket,
                timeout_seconds=settings.inspection_worker_timeout,
                unavailable_code="inspection_unavailable",
                label="Inspection worker",
                error_cls=InspectionWorkerError,
            )
        elif transport != "mock":
            raise ValueError(f"unsupported inspection_worker transport: {transport}")

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if self._unix is not None:
            return await self._unix.call(request)
        return await self._mock.call(request)


def create_inspection_worker_gateway(settings: Settings) -> InspectionWorkerGateway:
    return InspectionWorkerGateway(settings)
