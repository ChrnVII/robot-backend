"""巡检域：/inspections 任务、/detections 分页、/images/{imageId} 证据图。

迁移来源：InspectionController.java：
- 任务与证据图经 inspection-worker 网关（IPC）；
- /detections 与 /results 读取 robot_platform 库的推理记录（InferenceQueryService）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from app.api.deps import get_inference_query_service, get_inspection_service
from app.api.validation import ensure_id, ensure_image_id
from app.common.responses import ok, trace_of
from app.schemas.inspection import StartInspectionRequest
from app.services.inspection import InferenceQueryService, InspectionService

router = APIRouter()


@router.post("/inspections", tags=["inspection"])
async def start_inspection(
    request: Request,
    body: StartInspectionRequest,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    return ok(await inspection.start(body), trace_of(request))


@router.post("/inspections/{task_run_id}/stop", tags=["inspection"])
async def stop_inspection(
    request: Request,
    task_run_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(task_run_id)
    return ok(await inspection.stop(task_run_id), trace_of(request))


@router.get("/inspections/{task_run_id}", tags=["inspection"])
async def inspection_status(
    request: Request,
    task_run_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(task_run_id)
    return ok(await inspection.status(task_run_id), trace_of(request))


@router.get("/inspections/{task_run_id}/detections", tags=["inspection"])
def inspection_detections(
    request: Request,
    task_run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    inference_query: InferenceQueryService = Depends(get_inference_query_service),
) -> dict:
    ensure_id(task_run_id)
    return ok(inference_query.results(task_run_id, offset, limit), trace_of(request))


@router.get("/inspections/{task_run_id}/results", tags=["inspection"])
def inspection_results(
    request: Request,
    task_run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    inference_query: InferenceQueryService = Depends(get_inference_query_service),
) -> dict:
    ensure_id(task_run_id)
    return ok(inference_query.results(task_run_id, offset, limit), trace_of(request))


@router.get("/inspections/{task_run_id}/images/{image_id}", tags=["inspection"])
async def inspection_image(
    task_run_id: str,
    image_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> FileResponse:
    ensure_id(task_run_id)
    ensure_image_id(image_id)
    image = await inspection.resolve_image(task_run_id, image_id)
    return FileResponse(image.path, media_type=image.content_type, headers={"Cache-Control": "no-store"})
