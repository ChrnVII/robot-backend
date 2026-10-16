"""工作台域（Vue 页面）：任务、模板、运行、告警、地图、参数、预置点、升降。

迁移来源：UiController.java + UiApplicationService.java（原内存态 + JSON 文件，现已落库）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from app.api.deps import get_workbench_service
from app.common.exceptions import ValidationFailed
from app.common.responses import ok, trace_of
from app.services.workbench import WorkbenchService

router = APIRouter()


@router.get("/tasks", tags=["workbench"])
def list_tasks(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.list_tasks(), trace_of(request))


@router.post("/tasks", tags=["workbench"])
def save_task(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(service.save_task(body), trace_of(request))


@router.delete("/tasks/{task_id}", tags=["workbench"])
def delete_task(
    request: Request,
    task_id: str,
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    service.delete_task(task_id)
    return ok({"deleted": True}, trace_of(request))


@router.get("/task-templates", tags=["workbench"])
def list_templates(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.list_templates(), trace_of(request))


@router.post("/task-templates", tags=["workbench"])
def save_template(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(service.save_template(body), trace_of(request))


@router.delete("/task-templates/{template_id}", tags=["workbench"])
def delete_template(
    request: Request,
    template_id: str,
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    service.delete_template(template_id)
    return ok({"deleted": True}, trace_of(request))


@router.get("/runs", tags=["workbench"])
def list_runs(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.list_runs(), trace_of(request))


@router.post("/runs/{run_id}/resume", tags=["workbench"])
def resume_run(
    request: Request,
    run_id: str,
    body: dict[str, Any] | None = Body(default=None),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(service.resume_run(run_id, body), trace_of(request))


@router.delete("/runs/{run_id}", tags=["workbench"])
def delete_run(
    request: Request,
    run_id: str,
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    service.delete_run(run_id)
    return ok({"deleted": True}, trace_of(request))


@router.get("/alerts", tags=["workbench"])
def list_alerts(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.list_alerts(), trace_of(request))


@router.post("/alerts/{alert_id}", tags=["workbench"])
def handle_alert(
    request: Request,
    alert_id: str,
    body: dict[str, Any] | None = Body(default=None),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(service.handle_alert(alert_id, body), trace_of(request))


@router.delete("/alerts/{alert_id}", tags=["workbench"])
def delete_alert(
    request: Request,
    alert_id: str,
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    service.delete_alert(alert_id)
    return ok({"deleted": True}, trace_of(request))


@router.get("/config", tags=["workbench"])
def get_config(
    request: Request,
    configType: str | None = None,
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    payload = service.inspection_config() if configType == "inspection" else service.get_config()
    return ok(payload, trace_of(request))


@router.put("/config", tags=["workbench"])
def save_config(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(service.save_config(body), trace_of(request))


@router.get("/map/stations", tags=["workbench"])
def get_stations(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.get_map()["stations"], trace_of(request))


@router.put("/map/stations", tags=["workbench"])
def save_stations(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(service.save_map(body), trace_of(request))


@router.get("/cam/presets", tags=["workbench"])
def list_presets(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.list_presets(), trace_of(request))


@router.post("/cam/presets", tags=["workbench"])
def save_preset(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    try:
        preset_id = int(str(body.get("id")))
    except (TypeError, ValueError) as exc:
        # 自由 JSON 载荷下的必填字段校验：非法/缺失 id 应返回 400 而非兜底 500。
        raise ValidationFailed("preset id must be an integer") from exc
    return ok(service.save_preset(preset_id), trace_of(request))


@router.delete("/cam/presets/{preset_id}", tags=["workbench"])
def delete_preset(
    request: Request,
    preset_id: int,
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    service.delete_preset(preset_id)
    return ok({"deleted": True}, trace_of(request))


@router.get("/lift/state", tags=["workbench"])
def lift_state(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.lift_state(), trace_of(request))
