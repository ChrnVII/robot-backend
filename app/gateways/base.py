"""网关抽象：后端仅通过 IPC 调用机器人进程，禁止直接 import robot_core 内部实现。

迁移来源：core/RobotCoreGateway.java、SensorWorkerGateway.java、InspectionWorkerGateway.java
与 UnixJsonSocketClient.java（含 PeerRejected -> 稳定错误码的映射规则）。

接口对齐 API.md 第 8 节 IPC 契约：
- robot-core：health / device.state / device.list / lease.acquire / lease.release /
  command / emergency.stop / mission.* / checkpoint.get / asset.register / event.append
- sensor-worker：capture / worker.health
- inspection-worker：start / stop / status / detections / latest_frame(s) / resolve_image
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.ipc import JsonSocketClient, JsonSocketError


class GatewayError(RuntimeError):
    """网关错误基类：携带稳定错误码（对齐旧 *Exception(code, message)）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RobotCoreError(GatewayError):
    """robot-core IPC 调用失败。"""


class SensorWorkerError(GatewayError):
    """sensor-worker IPC 调用失败。"""


class InspectionWorkerError(GatewayError):
    """inspection-worker IPC 调用失败。"""


class UnixCaller:
    """UDS JSON Lines 调用封装：把 IPC 错误映射为稳定错误码异常（对齐旧 Java 网关语义）。"""

    def __init__(
        self,
        socket_path: str,
        timeout_seconds: float,
        unavailable_code: str,
        label: str,
        error_cls: type[GatewayError],
    ) -> None:
        self._client = JsonSocketClient(socket_path, timeout_seconds)
        self._unavailable_code = unavailable_code
        self._label = label
        self._error_cls = error_cls

    async def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return await self._client.call(request)
        except JsonSocketError as exc:
            raise self._error_cls(exc.code, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 与旧 Java 网关 getMessage 包装保持一致
            raise self._error_cls(self._unavailable_code, f"{self._label} call failed: {exc}") from exc


def to_camel(key: str) -> str:
    """snake_case -> camelCase（对齐旧 Java JsonNames.toCamelCase）。"""
    result: list[str] = []
    uppercase_next = False
    for character in key:
        if character == "_":
            uppercase_next = True
        elif uppercase_next:
            result.append(character.upper())
            uppercase_next = False
        else:
            result.append(character)
    return "".join(result)


def camelize(value: Any) -> Any:
    """递归转换字典键为 camelCase（对齐旧 Java JsonNames.camelize，供服务层使用）。"""
    if isinstance(value, Mapping):
        return {to_camel(str(key)): camelize(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [camelize(child) for child in value]
    return value
