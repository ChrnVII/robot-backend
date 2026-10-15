"""机器人域：/robot/state、/environment、/devices/{deviceId}/state。

迁移来源：RobotController.java（经 RobotCoreGateway 读取 robot-core，响应 camelize）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_robot_service
from app.common.responses import ok, trace_of
from app.services.robot import RobotService

router = APIRouter()


@router.get("/robot/state", tags=["robot"])
async def robot_state(request: Request, robot: RobotService = Depends(get_robot_service)) -> dict:
    return ok(await robot.robot_state(), trace_of(request))


@router.get("/environment", tags=["robot"])
async def environment(request: Request, robot: RobotService = Depends(get_robot_service)) -> dict:
    return ok(await robot.device_state("environment"), trace_of(request))


@router.get("/devices/{device_id}/state", tags=["robot"])
async def device_state(
    request: Request,
    device_id: str,
    robot: RobotService = Depends(get_robot_service),
) -> dict:
    return ok(await robot.device_state(device_id), trace_of(request))
