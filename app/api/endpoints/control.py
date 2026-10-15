"""控制域：/control/leases、/devices/commands、/emergency-stop。

迁移来源：RobotController.java；运动类命令必须带租约、TTL 与幂等键。
命令/急停同时触发工作台副作用（升降状态、运行记录、急停告警），与旧实现一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_control_service, get_workbench_service
from app.api.validation import ensure_lease_id
from app.common.responses import ok, trace_of
from app.schemas.control import CommandRequest, EmergencyStopRequest, LeaseRequest
from app.services.control import ControlService
from app.services.workbench import WorkbenchService

router = APIRouter()


@router.post("/control/leases", tags=["control"])
async def acquire_lease(
    request: Request,
    body: LeaseRequest,
    control: ControlService = Depends(get_control_service),
) -> dict:
    return ok(await control.acquire_lease(body), trace_of(request))


@router.delete("/control/leases/{lease_id}", tags=["control"])
async def release_lease(
    request: Request,
    lease_id: str,
    reason: str = Query(default="released", max_length=256),
    control: ControlService = Depends(get_control_service),
) -> dict:
    ensure_lease_id(lease_id)
    return ok(await control.release_lease(lease_id, reason), trace_of(request))


@router.post("/devices/commands", tags=["control"])
async def execute_command(
    request: Request,
    body: CommandRequest,
    control: ControlService = Depends(get_control_service),
    workbench: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    result = await control.execute_command(body)
    task = body.payload.get("task")
    task_id = str(task) if task is not None else None
    workbench.apply_lift(body.action, body.payload)
    if body.action == "motion.start_task" and task_id is not None:
        workbench.create_run(task_id)
    workbench.apply_task_command(task_id, body.action, result)
    return ok(result, trace_of(request))


@router.post("/emergency-stop", tags=["control"])
async def emergency_stop(
    request: Request,
    body: EmergencyStopRequest,
    control: ControlService = Depends(get_control_service),
    workbench: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    result = await control.emergency_stop(body)
    workbench.create_alert("critical", f"机器人已执行急停：{body.reason}", "safety")
    return ok(result, trace_of(request))
