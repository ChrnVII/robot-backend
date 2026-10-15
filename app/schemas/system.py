"""系统域出入参：平台信息、健康与版本。

字段名与旧 Java DTO 保持一致（camelCase 线上契约）。
"""

from typing import Any

from pydantic import BaseModel


class PlatformResponse(BaseModel):
    robotId: str
    mode: str
    architecture: str = "vue-python"
    apiVersion: str = "v1"


class HealthResponse(BaseModel):
    status: str
    service: str
    robotId: str
    coreStatus: str
    timestamp: str


class VersionResponse(BaseModel):
    version: str
    apiVersion: str = "v1"


class ReadyResponse(BaseModel):
    ready: bool = True
    status: str = "ok"


def platform_payload(robot_id: str, mode: str) -> dict[str, Any]:
    return PlatformResponse(robotId=robot_id, mode=mode).model_dump()
