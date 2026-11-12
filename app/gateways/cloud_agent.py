"""cloud-agent 网关：通过 UDS 读取状态、下发配置与运维操作。

cloud-agent 是后台服务进程（`app/cloud_agent/`，与 inspection-worker / robot-core
同一套 pid/日志/启停方式），不监听任何 TCP 端口；backend
只经 `run/cloud-agent.sock` 与它说话。这与 robot-core / sensor-worker 的调用
方式一致，页面因此不需要知道对端是进程内还是进程外。

约定：本网关只做转发，不缓存状态——页面看到的永远是 cloud-agent 的当前快照。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.config import Settings
from app.gateways.base import GatewayError, UnixCaller


class CloudAgentError(GatewayError):
    """cloud-agent 未运行或拒绝了请求。"""


class CloudAgentGateway:
    def __init__(self, settings: Settings) -> None:
        self._unix = UnixCaller(
            socket_path=settings.cloud_agent_socket,
            timeout_seconds=settings.cloud_agent_timeout,
            unavailable_code="cloud_agent_unavailable",
            label="Cloud agent",
            error_cls=CloudAgentError,
        )

    async def call(self, op: str, **params: Any) -> dict[str, Any]:
        request: dict[str, Any] = {"op": op}
        request.update({key: value for key, value in params.items() if value is not None})
        return await self._unix.call(request)

    async def raw(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self._unix.call(request)

    async def aclose(self) -> None:
        await self._unix.aclose()


def create_cloud_agent_gateway(settings: Settings) -> CloudAgentGateway:
    return CloudAgentGateway(settings)
