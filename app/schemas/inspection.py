"""巡检域出入参：StartInspectionRequest、StartVideoStreamRequest（camelCase 线上契约）。

校验规则对齐旧 Java DTO：模式枚举、ID/摄像头 ID 字符集、durationSeconds >= 0、最多 8 路。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# 注意：pydantic 的 pattern 默认是「搜索」语义，等价旧 Java .matches() 需要加锚点
CameraId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
RunId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")]


class StartInspectionRequest(BaseModel):
    mode: Literal["single", "four", "eight", "custom"] = "single"
    taskRunId: RunId | None = None
    durationSeconds: float = Field(default=0.0, ge=0.0)
    cameraIds: list[CameraId] = Field(default_factory=list, max_length=8)
    pipeline: Literal["inference", "passthrough"] = "inference"


class StartVideoStreamRequest(BaseModel):
    mode: Literal["single", "four", "eight", "custom"] = "single"
    streamId: RunId | None = None
    durationSeconds: float = Field(default=0.0, ge=0.0)
    cameraIds: list[CameraId] = Field(default_factory=list, max_length=8)
