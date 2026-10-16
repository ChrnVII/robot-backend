"""WebSocket 路由：/ws/state、/ws/environment、/ws/inspections/{taskRunId}、/ws/video/{streamId}。

消息格式与旧 Java 契约一致：{"type": ..., "data": ..., "timestamp": ...}；
taskRunId / streamId / cameraId 按 [A-Za-z0-9._-]{1,128} 校验（非法时以 1007 关闭）。

v2 改造：
- 推送经 PushHub 主题广播（同主题客户端共享一次 IPC 调用，替代每客户端轮询）；
- auth_enabled=true 时校验访问令牌（查询参数 ``accessToken`` 或 Authorization 头），
  无令牌/令牌非法以 1008 关闭——对齐 REST 的 /api/v1 鉴权要求；
- 推送失败不再立即断开（见 hub 的连续失败阈值）。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Query, WebSocket

from app.api.deps import get_inspection_service, get_robot_service
from app.api.ws.hub import close_quietly, get_push_hub
from app.common.config import get_settings
from app.security.auth import UnauthorizedError, decode_token
from app.services.inspection import InspectionService
from app.services.robot import RobotService

logger = logging.getLogger(__name__)

ws_router = APIRouter()

_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_CAMERA_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

_BAD_DATA = 1007
_POLICY_VIOLATION = 1008


def _valid(value: str | None, pattern: re.Pattern[str]) -> bool:
    return bool(value) and bool(pattern.fullmatch(value or ""))


async def _authorize(websocket: WebSocket, access_token: str | None = Query(default=None)) -> bool:
    """鉴权：auth 关闭时直接放行；开启时校验访问令牌，失败以 1008 关闭。"""
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    token = access_token
    if not token:
        header = websocket.headers.get("authorization", "")
        if header.startswith("Bearer "):
            token = header[len("Bearer "):].strip()
    if not token:
        await close_quietly(websocket, _POLICY_VIOLATION)
        return False
    try:
        decode_token(token, settings, "access")
    except UnauthorizedError:
        logger.debug("rejecting websocket with invalid access token")
        await close_quietly(websocket, _POLICY_VIOLATION)
        return False
    return True


@ws_router.websocket("/ws/state")
async def state_socket(
    websocket: WebSocket,
    accessToken: str | None = Query(default=None),
    robot: RobotService = Depends(get_robot_service),
) -> None:
    if not await _authorize(websocket, accessToken):
        return
    await websocket.accept()
    topic = get_push_hub().topic("state", message_type="state", provider=robot.robot_state)
    await topic.serve(websocket)


@ws_router.websocket("/ws/environment")
async def environment_socket(
    websocket: WebSocket,
    accessToken: str | None = Query(default=None),
    robot: RobotService = Depends(get_robot_service),
) -> None:
    if not await _authorize(websocket, accessToken):
        return
    await websocket.accept()

    async def provider() -> dict:
        return await robot.device_state("environment")

    topic = get_push_hub().topic("environment", message_type="environment", provider=provider)
    await topic.serve(websocket)


@ws_router.websocket("/ws/inspections/{task_run_id}")
async def inspection_socket(
    websocket: WebSocket,
    task_run_id: str,
    accessToken: str | None = Query(default=None),
    inspection: InspectionService = Depends(get_inspection_service),
) -> None:
    if not await _authorize(websocket, accessToken):
        return
    await websocket.accept()
    if not _valid(task_run_id, _ID):
        await close_quietly(websocket, _BAD_DATA)
        return

    async def provider() -> dict:
        return await inspection.snapshot(task_run_id)

    topic = get_push_hub().topic(f"inspection:{task_run_id}", message_type="inspection", provider=provider)
    await topic.serve(websocket)


@ws_router.websocket("/ws/video/{stream_id}")
async def video_socket(
    websocket: WebSocket,
    stream_id: str,
    accessToken: str | None = Query(default=None),
    cameraId: str | None = Query(default=None),
    inspection: InspectionService = Depends(get_inspection_service),
) -> None:
    if not await _authorize(websocket, accessToken):
        return
    await websocket.accept()
    if not _valid(stream_id, _ID) or (cameraId is not None and not _valid(cameraId, _CAMERA_ID)):
        await close_quietly(websocket, _BAD_DATA)
        return

    async def provider() -> dict:
        return {
            "streamId": stream_id,
            **await inspection.snapshot(stream_id, camera_id=cameraId, include_detections=False),
        }

    topic = get_push_hub().topic(f"video:{stream_id}:{cameraId or '*'}", message_type="video", provider=provider)
    await topic.serve(websocket)
