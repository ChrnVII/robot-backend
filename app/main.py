"""FastAPI 应用装配入口（uvicorn / fastapi CLI 均指向 app.main:app）。

职责：创建应用、注册中间件与异常处理器、挂载 REST / WebSocket 路由、
管理生命周期（建表、默认管理员、WS 推送中枢与 PG 事件监听、网关连接池释放）。
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router, root_router
from app.api.ws.events import PgEventListener
from app.api.ws.hub import get_push_hub, reset_push_hub
from app.api.ws.routes import ws_router
from app.common.config import Settings, get_settings
from app.common.exceptions import register_exception_handlers
from app.common.logging import setup_logging
from app.common.middleware import TraceIdMiddleware
from app.contracts import Channels
from app.db.database import get_session_factory, init_db

logger = logging.getLogger(__name__)


def _start_event_listener(settings: Settings) -> PgEventListener | None:
    """启动 PG NOTIFY 监听线程：事件到达即唤醒对应 WS 主题。

    数据库未配置或显式关闭时返回 None——WS 仍有 500ms 周期兜底推送。
    """
    if not settings.ws_event_listener_enabled or not settings.database_url:
        return None
    loop = asyncio.get_running_loop()
    hub = get_push_hub()

    def on_event(payload: dict) -> None:
        # 监听线程 -> 事件循环：线程安全投递唤醒信号。
        loop.call_soon_threadsafe(hub.wake_payload, payload)

    listener = PgEventListener(settings.database_url, channel=Channels.EVENTS, on_event=on_event)
    listener.start()
    return listener


async def _close_gateways() -> None:
    """释放网关的 IPC 连接池（顺序：先停推送，再关连接）。"""
    from app.api import deps

    for factory in (
        deps.get_robot_core_gateway,
        deps.get_sensor_worker_gateway,
        deps.get_inspection_worker_gateway,
    ):
        try:
            gateway = factory()
        except Exception:  # noqa: BLE001 - 未被使用的网关无需释放
            continue
        aclose = getattr(gateway, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不阻断进程退出
            logger.debug("closing gateway failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    # 始终确保存在默认管理员（鉴权开启时用于首次登录；开启后请立即改密）
    from app.security.auth import ensure_default_admin

    session = get_session_factory()()
    try:
        if ensure_default_admin(session, settings):
            logger.warning(
                "已创建默认管理员 %s（默认密码仅用于开发/首次登录，请立即修改）",
                settings.default_admin_username,
            )
    finally:
        session.close()
    listener = _start_event_listener(settings)
    logger.info(
        "backend ready: robot=%s mode=%s auth=%s ws_events=%s",
        settings.robot_id, settings.mode, settings.auth_enabled, bool(listener),
    )
    yield
    # 退出：停事件监听 → 关推送订阅 → 释放网关 IPC 连接池 → 释放数据库引擎
    if listener is not None:
        listener.stop()
    await get_push_hub().shutdown()
    reset_push_hub()
    await _close_gateways()
    from app.db.database import reset_engine

    reset_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging()

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

    app.add_middleware(TraceIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(root_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    app.include_router(ws_router)
    register_exception_handlers(app)

    return app


app = create_app()
