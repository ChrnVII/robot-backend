"""Inspection tasks, JSON results and task folder files."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from app.api.deps import get_inspection_service
from app.api.validation import ensure_id
from app.common.responses import ok, trace_of
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
    if directory is None or not directory.is_relative_to(root.resolve()) or not directory.is_dir():
        raise AppError('任务文件夹不存在', code='NOT_FOUND', status_code=404)
    return directory, status


@router.get('/inspections/{task_run_id}/files', tags=['inspection'])
async def inspection_files(request: Request, task_run_id: str,
                           inspection: InspectionService = Depends(get_inspection_service)):
    directory, status = await _task_directory(inspection, task_run_id)
    active = status.get('state') in {'starting', 'running', 'stopping'}
    files = [dict(name=name, size=(directory / name).stat().st_size, writing=active and name == 'video.mkv')
             for name in ('video.mkv', 'inspection.json') if (directory / name).is_file()]
    return ok(dict(dir=str(directory), files=files), trace_of(request))


@router.get('/inspections/{task_run_id}/files/{name}', tags=['inspection'])
async def inspection_file(task_run_id: str, name: str,
                          inspection: InspectionService = Depends(get_inspection_service)):
    from app.common.exceptions import AppError
    from fastapi.responses import Response
    directory, status = await _task_directory(inspection, task_run_id)
    if name not in {'video.mkv', 'inspection.json'} or not (directory / name).is_file():
        raise AppError('文件不存在', code='NOT_FOUND', status_code=404)
    if name == 'video.mkv' and status.get('state') in {'starting', 'running', 'stopping'}:
        raise AppError('录像正在写入，请先停止任务', code='RECORDING_ACTIVE', status_code=409)
    if name == 'inspection.json':
        # Read an atomic snapshot; the writer may replace the file immediately afterwards.
        import asyncio
        data = await asyncio.to_thread((directory / name).read_bytes)
        return Response(data, media_type='application/json', headers={'Content-Disposition': 'attachment; filename="inspection.json"'})
    return FileResponse(directory / name, media_type='video/x-matroska', filename=name)
