"""inspection-worker 网关实现（巡检任务、检测分页与证据图解析）。

迁移来源：InspectionWorkerGateway.java + UnixSocketInspectionWorkerGateway.java。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.config import Settings
from app.gateways.base import InspectionWorkerError, UnixCaller


class InspectionWorkerGateway:
    """通过 Linux 工控机 UDS 调用 inspection-worker 进程。"""

    def __init__(self, settings: Settings) -> None:
        self._unix = UnixCaller(
            socket_path=settings.inspection_worker_socket,
            timeout_seconds=settings.inspection_worker_timeout,
            unavailable_code="inspection_unavailable",
            label="Inspection worker",
            error_cls=InspectionWorkerError,
        )

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self._unix.call(request)

    async def aclose(self) -> None:
        await self._unix.aclose()


def create_inspection_worker_gateway(settings: Settings) -> InspectionWorkerGateway:
    return InspectionWorkerGateway(settings)
