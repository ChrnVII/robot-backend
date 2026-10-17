"""FastAPI 依赖注入：网关实例、业务服务与数据库会话。

网关实例通过 Linux 工控机 UDS 连接机器人进程（进程内单例）。
测试可用 app.dependency_overrides 或 get_settings.cache_clear() 替换。
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session

from app.common.config import Settings, get_settings
from app.db.database import get_db
from app.gateways.inspection_worker import InspectionWorkerGateway, create_inspection_worker_gateway
from app.gateways.robot_core import RobotCoreGateway, create_robot_core_gateway
from app.gateways.sensor_worker import SensorWorkerGateway, create_sensor_worker_gateway
from app.services.cameras import CameraDirectoryService
from app.services.control import ControlService
from app.services.inspection import InferenceQueryService, InspectionService
from app.services.media import MediaService
from app.services.robot import RobotService
from app.services.workbench import WorkbenchService


def current_settings() -> Settings:
    return get_settings()


@lru_cache(maxsize=1)
def get_robot_core_gateway() -> RobotCoreGateway:
    return create_robot_core_gateway(get_settings())


@lru_cache(maxsize=1)
def get_sensor_worker_gateway() -> SensorWorkerGateway:
    return create_sensor_worker_gateway(get_settings())


@lru_cache(maxsize=1)
def get_inspection_worker_gateway() -> InspectionWorkerGateway:
    return create_inspection_worker_gateway(get_settings())


@lru_cache(maxsize=1)
def get_robot_service() -> RobotService:
    return RobotService(get_robot_core_gateway(), get_settings())


@lru_cache(maxsize=1)
def get_control_service() -> ControlService:
    return ControlService(get_robot_core_gateway())


@lru_cache(maxsize=1)
def get_camera_directory() -> CameraDirectoryService:
    return CameraDirectoryService(get_settings())


@lru_cache(maxsize=1)
def get_media_service() -> MediaService:
    return MediaService(get_sensor_worker_gateway(), get_camera_directory())


@lru_cache(maxsize=1)
def get_inspection_service() -> InspectionService:
    return InspectionService(get_inspection_worker_gateway(), get_camera_directory())


@lru_cache(maxsize=1)
def get_inference_query_service() -> InferenceQueryService:
    # 复用后端共享 engine（检测记录与后端自有表同处 robot_platform 库），
    # 避免为只读查询再开一个连接池。
    return InferenceQueryService()


def get_workbench_service(session: Session = Depends(get_db)) -> WorkbenchService:
    return WorkbenchService(session, get_settings())
