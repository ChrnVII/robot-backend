"""Inspection tasks, JSON results and opening task directories."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_inspection_service
from app.api.validation import ensure_id
from app.common.responses import ok, trace_of
from app.common.local_folder import reveal_in_file_manager
from app.schemas.inspection import StartInspectionRequest
from app.services.inspection import InspectionService

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
async def inspection_detections(
    request: Request,
    task_run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(task_run_id)
    return ok(await inspection.detections(task_run_id, offset, limit), trace_of(request))


@router.get("/inspections/{task_run_id}/results", tags=["inspection"])
async def inspection_results(
    request: Request,
    task_run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(task_run_id)
    return ok(await inspection.detections(task_run_id, offset, limit), trace_of(request))


async def _task_directory(inspection, task_run_id):
    from pathlib import Path
    from app.common.exceptions import AppError
    ensure_id(task_run_id)
    status = await inspection.status(task_run_id)
    value = status.get('outputDir')
    root = Path(__file__).resolve().parents[3] / 'data' / 'cams'
    directory = Path(value).resolve() if value else None
    if directory is None or directory == root.resolve() or not directory.is_relative_to(root.resolve()) or not directory.is_dir():
        raise AppError('任务文件夹不存在', code='NOT_FOUND', status_code=404)
    return directory


@router.post('/inspections/{task_run_id}/open', tags=['inspection'])
async def open_inspection_directory(request: Request, task_run_id: str,
                                    inspection: InspectionService = Depends(get_inspection_service)):
    directory = await _task_directory(inspection, task_run_id)
    opened, detail = await asyncio.to_thread(reveal_in_file_manager, str(directory))
    payload = dict(opened=opened, dir=str(directory))
    if not opened:
        payload['detail'] = detail
    return ok(payload, trace_of(request))
