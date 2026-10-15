"""REST 路由汇总。

- auth 路由保持开放（/auth/login、/auth/refresh、/auth/logout）；
- 其余 /api/v1 路由统一挂 require_authentication：auth 关闭时零影响，
  开启（ROBOT_AUTH_ENABLED=true）时要求 Bearer 访问令牌；
- root_router：根路径探针与版本（/healthz、/readyz、/version，保持公开）。
"""

from fastapi import APIRouter, Depends

from app.api.endpoints import (
    auth,
    control,
    inspection,
    media,
    robot,
    system,
    video,
    workbench,
)
from app.security.auth import require_authentication

api_router = APIRouter()
api_router.include_router(auth.router, tags=["auth"])

protected = APIRouter()
protected.include_router(system.router, tags=["system"])
protected.include_router(robot.router, tags=["robot"])
protected.include_router(control.router, tags=["control"])
protected.include_router(media.router, tags=["media"])
protected.include_router(inspection.router, tags=["inspection"])
protected.include_router(video.router, tags=["video"])
protected.include_router(workbench.router, tags=["workbench"])
api_router.include_router(protected, dependencies=[Depends(require_authentication)])

root_router = APIRouter()
root_router.include_router(system.root_router, tags=["system"])
