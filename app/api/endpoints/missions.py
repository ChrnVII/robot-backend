"""巡检任务（采集会话）接口。

与 workbench 的 /tasks 并存但互不影响：/tasks 服务旧工作台页面（ui_tasks），
/missions 是新任务链路（missions + mission_points）。

启动运行（:run）属于 P1 会话执行器，这里先不暴露。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import FileResponse

from app.api.deps import get_mission_runner_service, get_mission_service
from app.common.responses import ok, trace_of
from app.services.mission_runner import MissionRunner
from app.services.missions import MissionService

router = APIRouter()


@router.get("/missions", tags=["missions"])
def list_missions(
    request: Request,
    page: int | None = Query(default=None, ge=1, description="页码从 1 开始；不传返回全量数组"),
    pageSize: int | None = Query(default=None, ge=1, le=500, description="每页条数"),
    keyword: str = Query(default="", description="按任务名称 / 任务 ID 模糊搜索"),
    state: str | None = Query(default=None, description="draft / enabled / disabled / archived"),
    service: MissionService = Depends(get_mission_service),
) -> dict:
    return ok(
        service.list_missions(page=page, page_size=pageSize, keyword=keyword, state=state),
        trace_of(request),
    )


@router.get("/mission-runs", tags=["missions"])
def list_mission_runs(
    request: Request,
    page: int | None = Query(default=None, ge=1, description="页码从 1 开始；不传返回数组"),
    pageSize: int | None = Query(default=None, ge=1, le=500, description="每页条数"),
    keyword: str = Query(default="", description="按执行 ID / 任务 ID / 任务名称模糊搜索"),
    state: str | None = Query(default=None, description="queued / executing / completed / failed / aborted / interrupted"),
    service: MissionService = Depends(get_mission_service),
) -> dict:
    """全局运行记录。

    前端「运行数据」页此前读的是 ``/runs``（ui_runs 影子表）：只有底盘任务命令会写，
    采集会话的真实记录在 ``mission_runs``，于是页面长期是空的。这里补上全局视图，
    ``/runs`` 保持原样不动（旧工作台仍在用）。
    """
    return ok(
        service.list_runs(page=page, page_size=pageSize, keyword=keyword, state=state),
        trace_of(request),
    )


@router.post("/missions", tags=["missions"])
def create_mission(
    request: Request,
    body: dict[str, Any] = Body(default_factory=dict),
    service: MissionService = Depends(get_mission_service),
) -> dict:
    return ok(service.create_mission(body), trace_of(request))


@router.get("/missions/{mission_id}", tags=["missions"])
def get_mission(
    request: Request,
    mission_id: str,
    service: MissionService = Depends(get_mission_service),
) -> dict:
    return ok(service.get_mission(mission_id), trace_of(request))


@router.put("/missions/{mission_id}", tags=["missions"])
def update_mission(
    request: Request,
    mission_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    service: MissionService = Depends(get_mission_service),
) -> dict:
    return ok(service.update_mission(mission_id, body), trace_of(request))


@router.delete("/missions/{mission_id}", tags=["missions"])
def archive_mission(
    request: Request,
    mission_id: str,
    service: MissionService = Depends(get_mission_service),
) -> dict:
    return ok(service.archive_mission(mission_id), trace_of(request))


@router.get("/missions/{mission_id}/points", tags=["missions"])
def list_mission_points(
    request: Request,
    mission_id: str,
    service: MissionService = Depends(get_mission_service),
) -> dict:
    return ok(service.list_points(mission_id), trace_of(request))


# ---------------------------------------------------------------------------
# 执行（采集会话）。P1b：模拟采集，目录 / manifest / 事件 / 落库全真。
# ---------------------------------------------------------------------------

@router.post("/missions/{mission_id}/run", tags=["missions"])
def run_mission(
    request: Request,
    mission_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    runner: MissionRunner = Depends(get_mission_runner_service),
) -> dict:
    """启动一次会话。

    body 可选：
    - ``overrides``：本次临时覆盖 video / audio / durationSeconds（只影响本次）
    - ``speed``：模拟加速倍数，默认 1.0（实时）。仅模拟会话生效。
    """
    return ok(
        runner.start(
            mission_id,
            overrides=body.get("overrides") or {},
            speed=float(body.get("speed") or 1.0),
        ),
        trace_of(request),
    )


@router.post("/runs/{task_run_id}/stop", tags=["missions"])
def stop_run(
    request: Request,
    task_run_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    runner: MissionRunner = Depends(get_mission_runner_service),
) -> dict:
    return ok(runner.stop(task_run_id, str(body.get("reason") or "operator")), trace_of(request))


@router.get("/runs/{task_run_id}", tags=["missions"])
def get_run(
    request: Request,
    task_run_id: str,
    runner: MissionRunner = Depends(get_mission_runner_service),
) -> dict:
    return ok(runner.status(task_run_id), trace_of(request))


@router.post("/runs/{task_run_id}/restart", tags=["missions"])
def resume_run(
    request: Request,
    task_run_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
    runner: MissionRunner = Depends(get_mission_runner_service),
) -> dict:
    """中断/失败后重跑一次（新开会话，旧目录保留）。

    路径用 ``/restart`` 而不是 ``/resume``：``workbench`` 路由注册在本模块之前，
    它已经占用了 ``POST /runs/{run_id}/resume``（旧工作台 ui_runs 的重跑）。
    FastAPI 按注册顺序匹配，同路径同方法会被前面的实现截获，导致请求进不到这里。
    """
    return ok(runner.resume(task_run_id, speed=float(body.get("speed") or 1.0)), trace_of(request))


# ---------------------------------------------------------------------------
# 详情与产物（P3）
# ---------------------------------------------------------------------------

@router.get("/missions/{mission_id}/detail", tags=["missions"])
def mission_detail(
    request: Request,
    mission_id: str,
    service: MissionService = Depends(get_mission_service),
) -> dict:
    """任务 + 最近运行 + 最近一次产物清单，详情页一次拿全。"""
    return ok(service.mission_detail(mission_id), trace_of(request))


@router.get("/runs/{task_run_id}/artifacts", tags=["missions"])
def run_artifacts(
    request: Request,
    task_run_id: str,
    service: MissionService = Depends(get_mission_service),
) -> dict:
    return ok(service.run_artifacts(task_run_id), trace_of(request))


@router.get("/runs/{task_run_id}/file", tags=["missions"])
def run_artifact_file(
    task_run_id: str,
    path: str = Query(..., description="相对任务目录的路径，如 audio/continuous/session.wav"),
    service: MissionService = Depends(get_mission_service),
):
    """流式返回任务目录内的单个产物；路径越界直接拒绝。"""
    target = service.resolve_artifact(task_run_id, path)
    media_type = _mime_for(target.name)
    return FileResponse(target, media_type=media_type, filename=target.name)


def _mime_for(name: str) -> str:
    from pathlib import Path as _Path

    return {
        ".wav": "audio/wav",
        ".mkv": "video/x-matroska",
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
    }.get(_Path(name).suffix.lower(), "application/octet-stream")
