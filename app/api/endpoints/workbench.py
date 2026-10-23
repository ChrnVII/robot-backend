"""工作台域（Vue 页面）：任务、模板、运行、告警、地图、参数、预置点、升降。

迁移来源：UiController.java + UiApplicationService.java（原内存态 + JSON 文件，现已落库）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_camera_directory, get_control_service, get_workbench_service
from app.common.exceptions import ValidationFailed
from app.common.responses import ok, trace_of
from app.gateways.base import GatewayError
from app.services.cameras import CameraDirectoryService
from app.services.control import ControlService
from app.services.workbench import WorkbenchService

router = APIRouter()


@router.get("/tasks", tags=["workbench"])
def list_tasks(
    request: Request,
    page: int | None = Query(default=None, ge=1, description="页码从 1 开始；不传返回全量数组"),
    pageSize: int | None = Query(default=None, ge=1, le=500, description="每页条数"),
    keyword: str = Query(default="", description="按任务名称 / 任务 ID 模糊搜索"),
    enabled: str | None = Query(default=None, description="on / off，缺省不过滤"),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(
        service.list_tasks(page=page, page_size=pageSize, keyword=keyword, enabled=enabled),
        trace_of(request),
    )


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
def list_runs(
    request: Request,
    page: int | None = Query(default=None, ge=1, description="页码从 1 开始；不传返回全量数组"),
    pageSize: int | None = Query(default=None, ge=1, le=500, description="每页条数"),
    keyword: str = Query(default="", description="按执行 ID / 任务 ID 模糊搜索"),
    status: str | None = Query(default=None, description="执行状态码，缺省不过滤"),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(
        service.list_runs(page=page, page_size=pageSize, keyword=keyword, status=status),
        trace_of(request),
    )


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
def list_alerts(
    request: Request,
    page: int | None = Query(default=None, ge=1, description="页码从 1 开始；不传返回全量数组"),
    pageSize: int | None = Query(default=None, ge=1, le=500, description="每页条数"),
    keyword: str = Query(default="", description="按告警内容 / 告警 ID / 执行 ID 模糊搜索"),
    level: str | None = Query(default=None, description="告警等级，缺省不过滤"),
    status: str | None = Query(default=None, description="处理状态，缺省不过滤"),
    service: WorkbenchService = Depends(get_workbench_service),
) -> dict:
    return ok(
        service.list_alerts(
            page=page, page_size=pageSize, keyword=keyword, level=level, status=status
        ),
        trace_of(request),
    )


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
async def list_presets(
    request: Request,
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    service: WorkbenchService = Depends(get_workbench_service),
    directory: CameraDirectoryService = Depends(get_camera_directory),
    control: ControlService = Depends(get_control_service),
) -> dict:
    """预置点清单：以相机为准。

    本地库只是名字缓存——相机 Web 端建的预置点、或因换机丢配置，都必须以
    设备实际存在的预置点为准，否则「列表里有点、点下去不动」。
    相机不可达时回落到缓存，保证页面仍可用（source 会标明来源）。
    """
    directory.require_ptz(cameraId)
    cached = await run_in_threadpool(service.list_presets)
    try:
        presets = await control.camera_presets()
    except GatewayError as error:
        return ok(
            {"rows": cached, "source": "database", "detail": str(error)},
            trace_of(request),
        )
    names = {
        int(item.get("presetId") or item.get("id") or 0): item.get("name")
        for item in cached
    }
    rows = [
        {**item, "name": names.get(int(item["presetId"])) or item.get("name") or ""}
        for item in presets
    ]
    return ok({"rows": rows, "source": "camera"}, trace_of(request))


@router.post("/cam/presets", tags=["workbench"])
def save_preset(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    service: WorkbenchService = Depends(get_workbench_service),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    directory.require_ptz(body.get("cameraId"))
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
    cameraId: str | None = Query(default=None, description="相机编号，缺省为球形摄像头"),
    service: WorkbenchService = Depends(get_workbench_service),
    directory: CameraDirectoryService = Depends(get_camera_directory),
) -> dict:
    directory.require_ptz(cameraId)
    service.delete_preset(preset_id)
    return ok({"deleted": True}, trace_of(request))


@router.get("/lift/state", tags=["workbench"])
def lift_state(request: Request, service: WorkbenchService = Depends(get_workbench_service)) -> dict:
    return ok(service.lift_state(), trace_of(request))
