"""FastAPI 应用装配入口（uvicorn / fastapi CLI 均指向 app.main:app）。

职责：创建应用、注册中间件与异常处理器、挂载 REST / WebSocket 路由、
管理生命周期（建表、默认管理员、启动摘要）。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router, root_router
from app.api.ws.routes import ws_router
from app.common.config import get_settings
from app.common.exceptions import register_exception_handlers
from app.common.logging import setup_logging
from app.common.middleware import TraceIdMiddleware
from app.db.database import get_session_factory, init_db

logger = logging.getLogger(__name__)


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
    logger.info(
        "backend ready: robot=%s mode=%s auth=%s",
        settings.robot_id, settings.mode, settings.auth_enabled,
    )
    yield
    # 退出：释放数据库连接
    from app.api import deps
    from app.db.database import reset_engine

    if deps.get_inference_query_service.cache_info().currsize:
        deps.get_inference_query_service().dispose()
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
