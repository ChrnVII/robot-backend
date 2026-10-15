"""WebSocket 路由：/ws/state、/ws/environment、/ws/inspections/{taskRunId}、/ws/video/{streamId}。

消息格式与旧 Java 契约一致：{"type": ..., "data": ..., "timestamp": ...}；
taskRunId / streamId / cameraId 按 [A-Za-z0-9._-]{1,128} 校验（非法时以 1007 关闭）。
迁移来源：RobotRealtimeHandler.java。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.api.deps import get_inspection_service, get_robot_service
from app.api.ws.hub import close_quietly, push_loop
from app.services.inspection import InspectionService
from app.services.robot import RobotService

logger = logging.getLogger(__name__)

ws_router = APIRouter()

_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_CAMERA_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

_BAD_DATA = 1007
_SERVICE_RESTARTED = 1012


def _valid(value: str | None, pattern: re.Pattern[str]) -> bool:
    return bool(value) and bool(pattern.fullmatch(value or ""))


@ws_router.websocket("/ws/state")
async def state_socket(websocket: WebSocket, robot: RobotService = Depends(get_robot_service)) -> None:
    await websocket.accept()
    await _serve(websocket, "state", robot.robot_state)


@ws_router.websocket("/ws/environment")
async def environment_socket(websocket: WebSocket, robot: RobotService = Depends(get_robot_service)) -> None:
    await websocket.accept()

    async def provider() -> dict:
        return await robot.device_state("environment")

    await _serve(websocket, "environment", provider)


@ws_router.websocket("/ws/inspections/{task_run_id}")
async def inspection_socket(
    websocket: WebSocket,
    task_run_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> None:
    await websocket.accept()
    if not _valid(task_run_id, _ID):
        await close_quietly(websocket, _BAD_DATA)
        return

    async def provider() -> dict:
        return {
            "status": await inspection.status(task_run_id),
            "detections": await inspection.detections(task_run_id, 0, 20),
            "frame": await inspection.latest_frame(task_run_id),
            "frames": await inspection.latest_frames(task_run_id),
        }

    await _serve(websocket, "inspection", provider)


@ws_router.websocket("/ws/video/{stream_id}")
async def video_socket(
    websocket: WebSocket,
    stream_id: str,
    inspection: InspectionService = Depends(get_inspection_service),
) -> None:
    await websocket.accept()
    camera_id = websocket.query_params.get("cameraId")
    if not _valid(stream_id, _ID) or (camera_id is not None and not _valid(camera_id, _CAMERA_ID)):
        await close_quietly(websocket, _BAD_DATA)
        return

    async def provider() -> dict:
        return {
            "streamId": stream_id,
            "status": await inspection.status(stream_id),
            "frame": await inspection.latest_frame(stream_id, camera_id),
        }

    await _serve(websocket, "video", provider)


async def _serve(websocket: WebSocket, message_type: str, provider) -> None:
    try:
        await push_loop(websocket, message_type, provider)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - 与旧实现一致：推送失败关闭连接
        logger.debug("closing websocket after publish failure", exc_info=True)
        await close_quietly(websocket, _SERVICE_RESTARTED)
