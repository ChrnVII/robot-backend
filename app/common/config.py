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

    # 鉴权（目标 API；默认关闭以保持旧前端联调契约，生产开启）
    auth_enabled: bool = False
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 14 * 24 * 3600
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
