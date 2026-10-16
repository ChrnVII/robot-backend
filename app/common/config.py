"""应用配置（pydantic-settings）。

环境变量前缀 ROBOT_，例如 ROBOT_CORS_ORIGINS、ROBOT_ENVIRONMENT、ROBOT_DATABASE_URL。
凭据不进入代码与仓库（沿用旧平台约定：systemd credentials / 环境变量注入）。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROBOT_", env_file="robot.env", extra="ignore")

    app_name: str = "inspection-robot-backend"
    version: str = "0.2.0"
    environment: str = "dev"  # dev | prod
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173"  # 逗号分隔，对应旧 FRONTEND_ORIGIN

    # 机器人身份与运行模式（对应旧 robot.robot-id / robot.mode）
    robot_id: str = "robot-dev-001"
    platform_config: str = ""  # Shared YAML path, checked at startup when configured.
    mode: str = "simulated"  # simulated | hardware

    # 机器人进程网关：通过 Linux 工控机 UDS（Unix domain socket）通信
    robot_core_socket: str = "run/robot-core.sock"
    robot_core_timeout: float = 5.0
    sensor_worker_socket: str = "run/sensor-worker.sock"
    sensor_worker_timeout: float = 65.0
    inspection_worker_socket: str = "run/inspection-worker.sock"
    inspection_worker_timeout: float = 10.0

    # 后端业务库（用户 / 任务 / 告警 / 报告 / 工作台数据，以及只读的检测结果；
    # PostgreSQL DSN）。生产与 robot-core 共用同一个 robot_platform 库，
    # 检测查询因此复用该 DSN 对应的共享 engine，不再单独配置第二个地址。
    database_url: str = ""
    auto_migrate: bool = True

    # 鉴权（目标 API；默认关闭以保持旧前端联调契约，生产开启）
    auth_enabled: bool = False
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 14 * 24 * 3600
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"

    # WebSocket 事件驱动推送：订阅 PostgreSQL NOTIFY 通道（robot_core 广播），
    # 事件到达即刷新对应主题；关闭后仅保留 500ms 周期兜底推送。
    ws_event_listener_enabled: bool = True

    # 球形摄像头低延迟预览：PyAV 解复用 → WebSocket 透传 Annex-B → 浏览器 WebCodecs。
    # 地址留空（或显式关闭）时整条链路静默降级，前端回退到 /api/v1/cam/stream 的
    # JPEG 轮询 —— 保证没配摄像头时页面依然可用。
    # 建议使用**子码流**（海康为 .../Streaming/Channels/102），且编码为 H.264：
    # WebCodecs 的 HEVC 支持依赖平台，H.265 子码流在 Chrome 上不保证能解。
    camera_stream_enabled: bool = True
    camera_stream_url: str = ""
    camera_stream_transport: str = "tcp"  # tcp | udp
    camera_stream_timeout_us: int = 5_000_000  # RTSP 超时（微秒）
    camera_stream_client_queue: int = 8  # 每个浏览器订阅者的帧队列上限
    camera_stream_reconnect_delay: float = 2.0

    # 实时检测叠加（可选，只推给前端，不落库 —— 留档仍走 inference-worker）
    camera_stream_detect_enabled: bool = False
    camera_stream_model: str = ""  # YOLO 权重路径，如 models/best.pt
    camera_stream_detect_every: int = 1
    camera_stream_imgsz: int = 640
    camera_stream_conf: float = 0.35
    camera_stream_iou: float = 0.5

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
