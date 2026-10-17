"""媒体域出入参：MediaCaptureRequest 与媒体资产响应（camelCase 线上契约）。

``deviceId`` 语义统一为「相机编号」：球形摄像头的设备名 ``camera``（兼容旧调用）、
球形编号（如 ``VAR-1``）、多路抓图相机编号（``cam01``…），以及非相机设备
``audio`` / ``thermal``。字符集与巡检 CameraId 一致；deviceId 与 mediaType 的
匹配检查仍在 service 层（与旧 MediaCaptureService 一致）。
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# 注意：与 app/schemas/inspection.py 的 CameraId、app/common/cameras.py 保持同一字符集
DeviceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")]


class MediaCaptureRequest(BaseModel):
    taskRunId: str = Field(min_length=1, max_length=128)
    waypointId: str = Field(min_length=1, max_length=128)
    deviceId: DeviceId
    mediaType: Literal["image", "audio", "thermal"]
    parameters: dict[str, Any] = Field(default_factory=dict)
