"""球形摄像头低延迟实时画面（PyAV + WebSocket + 浏览器 WebCodecs）。

移植自独立的单文件 demo（单码流 RTSP → Annex-B 透传 → WebCodecs 硬解），
并接入本项目的配置、鉴权与生命周期。与既有实现的关系：

- **替换**：``CameraView`` 的画面区域由「每 1 秒轮询 ``/api/v1/cam/stream`` 取一张
  JPEG」改为「WebSocket 收 Annex-B → 浏览器硬解上屏」，延迟从秒级降到亚秒级，
  并且不再为每一帧落盘、写库；
- **保留**：云台控制、预置点、抓图、重启仍走 robot-core 的 ISAPI 驱动，本包不参与；
- **降级**：未配置 RTSP 地址 / 未安装 PyAV / 浏览器不是安全上下文时，前端自动退回
  JPEG 轮询，页面始终可用。

对外入口：

- :func:`get_camera_stream_service` —— 生命周期单例
- :func:`reset_camera_stream_service` —— 测试用重置
"""

from app.video.service import (
    CameraStreamService,
    get_camera_stream_service,
    reset_camera_stream_service,
)

__all__ = [
    "CameraStreamService",
    "get_camera_stream_service",
    "reset_camera_stream_service",
]
