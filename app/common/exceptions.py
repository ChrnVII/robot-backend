"""业务异常与全局异常处理器。

迁移来源：old_project/backend/.../api/GlobalExceptionHandler.java；
错误码表见 old_project/backend/docs/API.md 第 5 节。
"""

import logging
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.responses import error_response, trace_of
from app.contracts import IpcErrorCode, is_not_found_code
from app.gateways.base import InspectionWorkerError, RobotCoreError, SensorWorkerError

logger = logging.getLogger(__name__)

_CAMEL_BOUNDARY = re.compile(r"([a-z])([A-Z])")


class AppError(Exception):
    """业务异常基类：稳定错误码 + HTTP 状态码。"""

    code = "INTERNAL_ERROR"
    status_code = 500

    def __init__(
        self,
        message: str = "internal error",
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class ValidationFailed(AppError):
    """字段缺失/枚举非法等等价于旧 IllegalArgumentException 的 400。"""

    code = "VALIDATION_ERROR"
    status_code = 400


def _snake_upper(code: str) -> str:
    """camelCase -> SNAKE_UPPER（对齐旧 Java errorCode()）。"""
    return _CAMEL_BOUNDARY.sub(r"\1_\2", code).upper()


def _field_errors(exc: RequestValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = [str(item) for item in error.get("loc", ()) if item not in ("body", "query", "path")]
        field = ".".join(location) or "request"
        fields.setdefault(field, str(error.get("msg", "invalid")))
    return fields


def register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常处理：业务异常、校验错误、网关错误与兜底 500。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        return error_response(exc.code, str(exc), exc.status_code, trace_id=trace_of(request))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        if any(str(item.get("type", "")).startswith("json_invalid") for item in errors):
            return error_response("INVALID_JSON", "Request body is invalid", 400, trace_id=trace_of(request))
        return error_response(
            "VALIDATION_ERROR", "Request validation failed", 400,
            trace_id=trace_of(request), data=_field_errors(exc),
        )

    @app.exception_handler(RobotCoreError)
    async def handle_robot_core(request: Request, exc: RobotCoreError):
        status_code = 503 if exc.code == "core_unavailable" else 409
        return error_response(exc.code.upper(), str(exc), status_code, trace_id=trace_of(request))

    @app.exception_handler(SensorWorkerError)
    async def handle_sensor_worker(request: Request, exc: SensorWorkerError):
        status_code = 503 if exc.code == "sensor_unavailable" else 409
        return error_response(_snake_upper(exc.code), str(exc), status_code, trace_id=trace_of(request))

    @app.exception_handler(InspectionWorkerError)
    async def handle_inspection_worker(request: Request, exc: InspectionWorkerError):
        if exc.code == "inspection_unavailable":
            status_code = 503
        elif is_not_found_code(exc.code):
            status_code = 404
        elif exc.code in (IpcErrorCode.INVALID_REQUEST, IpcErrorCode.UNSUPPORTED_OPERATION):
            status_code = 400
        else:
            status_code = 409
        return error_response(_snake_upper(exc.code), str(exc), status_code, trace_id=trace_of(request))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        return error_response("HTTP_ERROR", str(exc.detail), exc.status_code, trace_id=trace_of(request))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        logger.exception("unexpected backend error", exc_info=exc)
        return error_response("INTERNAL_ERROR", "Unexpected backend error", 500, trace_id=trace_of(request))
