"""实时推送骨架：按 topic 周期推送（默认 500ms，对齐旧 @Scheduled(fixedDelay=500)）。

迁移来源：RobotRealtimeHandler.java 的会话管理与推送循环。
每条消息格式：{"type": ..., "data": ..., "timestamp": ...}（与旧 Java 契约一致）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import WebSocket

from app.common.clock import utc_now_iso

logger = logging.getLogger(__name__)

PUSH_INTERVAL_SECONDS = 0.5

Provider = Callable[[], Awaitable[dict]]


async def push_loop(websocket: WebSocket, message_type: str, provider: Provider,
                    interval: float = PUSH_INTERVAL_SECONDS) -> None:
    """持续推送：构建消息 -> 发送 -> 间隔；异常由调用方处理（关闭连接）。"""
    while True:
        data = await provider()
        await websocket.send_json({"type": message_type, "data": data, "timestamp": utc_now_iso()})
        await asyncio.sleep(interval)


async def close_quietly(websocket: WebSocket, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass
