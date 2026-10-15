"""媒体域出入参：MediaCaptureRequest 与媒体资产响应（camelCase 线上契约）。

校验规则对齐旧 Java DTO：deviceId/mediaType 枚举，deviceId 与 mediaType 必须匹配
（匹配检查在 service 层，与旧 MediaCaptureService 一致）。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class MediaCaptureRequest(BaseModel):
    taskRunId: str = Field(min_length=1, max_length=128)
    waypointId: str = Field(min_length=1, max_length=128)
    deviceId: Literal["camera", "audio", "thermal"]
    mediaType: Literal["image", "audio", "thermal"]
    parameters: dict[str, Any] = Field(default_factory=dict)
