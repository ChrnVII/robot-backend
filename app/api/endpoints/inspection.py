"""Inspection tasks, JSON results and opening task directories."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_inspection_service
from app.api.validation import ensure_camera_id, ensure_id
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


@router.get("/inspections/active", tags=["inspection"])
async def active_inspection(
    request: Request,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    """当前正在跑的任务；没有则 ``{"state": "idle"}``。

    必须声明在 ``/inspections/{task_run_id}`` **之前**，否则 "active" 会被当成任务 ID
    去吃 400（ID 字符集校验）。前端靠它显示"正在跑什么"并拦截第二个启动请求——
    巡检域只有一个任务槽位（worker 里 `_thread` 非空即拒绝）。
    """
    return ok(await inspection.active(), trace_of(request))


@router.get("/inspections/{task_run_id}", tags=["inspection"])
async def inspection_status(
    request: Request,
    task_run_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(task_run_id)
    return ok(await inspection.status(task_run_id), trace_of(request))


@router.get("/inspections/{task_run_id}/file", tags=["inspection"])
async def inspection_file(
    task_run_id: str,
    path: str = Query(..., description="相对任务目录的路径，如 cam01/video.mkv"),
    inspection: InspectionService = Depends(get_inspection_service),
):
    """下载任务目录内的单个产物（视频 / 音频 / 检测记录）。

    只允许任务目录内部：先 resolve 再判断包含关系，`..` 与软链接都会被展开后拦下。
    """
    from fastapi.responses import FileResponse

    from app.common.exceptions import AppError
    from app.common.media_types import mime_for

    directory = await _task_directory(inspection, task_run_id)
    base = directory.resolve()
    target = (base / path).resolve()
    if target != base and base not in target.parents:
        raise AppError("非法的文件路径", code="ARTIFACT_FORBIDDEN", status_code=400)
    if not target.is_file():
        raise AppError(f"文件不存在：{path}", code="ARTIFACT_NOT_FOUND", status_code=404)
    return FileResponse(target, media_type=mime_for(target.name), filename=target.name)


@router.get("/inspections/{task_run_id}/detections", tags=["inspection"])
async def inspection_detections(
    request: Request,
    task_run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    cameraId: str | None = Query(default=None, description="只读该路；缺省按相机顺序合并"),
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(task_run_id)
    if cameraId is not None:
        ensure_camera_id(cameraId)
    return ok(await inspection.detections(task_run_id, offset, limit, cameraId), trace_of(request))


@router.get("/inspections/{task_run_id}/results", tags=["inspection"])
async def inspection_results(
    request: Request,
    task_run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    cameraId: str | None = Query(default=None, description="只读该路；缺省按相机顺序合并"),
    inspection: InspectionService = Depends(get_inspection_service),
) -> dict:
    ensure_id(task_run_id)
    if cameraId is not None:
        ensure_camera_id(cameraId)
    return ok(await inspection.detections(task_run_id, offset, limit, cameraId), trace_of(request))


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
