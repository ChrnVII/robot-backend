"""Authenticated status channels and binary per-camera preview proxies."""

from __future__ import annotations

import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.api.deps import get_camera_directory, get_inspection_service, get_robot_service
from app.api.ws.hub import close_quietly, get_push_hub
from app.common.config import get_settings
from app.common.exceptions import ValidationFailed
from app.security.auth import UnauthorizedError, decode_token
from app.services.inspection import InspectionService
from app.services.robot import RobotService
from app.video import get_camera_stream_service

logger = logging.getLogger(__name__)

ws_router = APIRouter()

_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_CAMERA_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

_BAD_DATA = 1007
_POLICY_VIOLATION = 1008
#: 服务端主动停止（与 api.ws.hub 的「服务重启」语义一致）。
_SERVICE_RESTARTED = 1012


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
        return {"status": await inspection.status(task_run_id)}

    topic = get_push_hub().topic(f"inspection:{task_run_id}", message_type="inspection", provider=provider)
    await topic.serve(websocket)


@ws_router.websocket("/ws/camera/preview")
async def camera_preview_socket(
    websocket: WebSocket,
    accessToken: str | None = Query(default=None),
    cameraId: str | None = Query(default=None),
) -> None:
    """球形摄像头低延迟实时画面（二进制通道）。

    服务端只解封装：文本 ``{"t":"config"}`` 告知 codec/时间轴基准/分辨率，
    二进制包为 ``flags(1)+pts_ms(4)+seq(4)+Annex-B``，解码在浏览器 WebCodecs 完成。
    服务未启用（未配 RTSP 地址/未装 PyAV）时，先发一条错误说明再以 1012 关闭，
    前端据此回退到 JPEG 轮询。
    """
    if not await _authorize(websocket, accessToken):
        return
    await websocket.accept()

    try:
        get_camera_directory().require_ptz(cameraId)
    except ValidationFailed as error:
        await websocket.send_json({
            "t": "error",
            "code": "unknown_camera",
            "message": str(error),
        })
        await close_quietly(websocket, 1008)
        return

    broadcaster = get_camera_stream_service().broadcaster
    if broadcaster is None:
        reason = get_camera_stream_service().skip_reason or "stream_disabled"
        await websocket.send_json({
            "t": "error",
            "code": "stream_disabled",
            "reason": reason,
            "message": "球形摄像头低延迟预览未启用，请回退到 JPEG 轮询",
        })
        await close_quietly(websocket, _SERVICE_RESTARTED)
        return

    client = broadcaster.add_client()

    async def wait_for_disconnect() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    receiver = asyncio.create_task(wait_for_disconnect())
    pending_frame = None
    try:
        config = broadcaster.config_message
        if config:
            await websocket.send_text(config)
        while True:
            pending_frame = asyncio.create_task(client.queue.get())
            done, _ = await asyncio.wait((receiver, pending_frame), return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                break
            item = pending_frame.result()
            if isinstance(item, str):
                await websocket.send_text(item)
                if json.loads(item).get("t") == "stop":
                    await close_quietly(websocket, _SERVICE_RESTARTED)
                    break
            else:
                await websocket.send_bytes(item)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - 客户端异常断开是常态，不记为错误
        logger.debug("摄像头预览客户端异常断开", exc_info=True)
    finally:
        broadcaster.remove_client(client)
        tasks = [task for task in (receiver, pending_frame) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@ws_router.websocket('/ws/inspections/{task_run_id}/cameras/{camera_id}')
async def inspection_camera_socket(websocket: WebSocket, task_run_id: str, camera_id: str,
                                   accessToken: str | None = Query(default=None)):
    if not await _authorize(websocket, accessToken):
        return
    await websocket.accept()
    if not _valid(task_run_id, _ID) or not _valid(camera_id, _CAMERA_ID):
        await close_quietly(websocket, _BAD_DATA)
        return
    import struct
    from app.video.inspection_preview import MAX_PACKET
    writer = receiver = sender = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(
            str(get_settings().inspection_worker_socket) + '.video'), 5)
        writer.write(json.dumps(dict(task_run_id=task_run_id, camera_id=camera_id)).encode() + b'\n')
        await writer.drain()
        async def receive():
            while (await websocket.receive())['type'] != 'websocket.disconnect':
                pass
        async def send():
            while True:
                size = struct.unpack('!I', await reader.readexactly(4))[0]
                if not 1 <= size <= MAX_PACKET:
                    raise ValueError('invalid preview packet size')
                packet = await reader.readexactly(size)
                if packet[:1] == b'T':
                    await websocket.send_text(packet[1:].decode())
                else:
                    await websocket.send_bytes(packet[1:])
        receiver = asyncio.create_task(receive())
        sender = asyncio.create_task(send())
        await asyncio.wait((receiver, sender), return_when=asyncio.FIRST_COMPLETED)
    except (OSError, ValueError, asyncio.TimeoutError, WebSocketDisconnect):
        pass
    finally:
        tasks = [task for task in (receiver, sender) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await close_quietly(websocket, _SERVICE_RESTARTED)
