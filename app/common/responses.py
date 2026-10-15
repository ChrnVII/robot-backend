"""统一响应封套（与 Vue 前端契约保持一致）。

成功与失败均返回：
    {"code": "OK", "message": "success", "data": {}, "traceId": "...", "timestamp": "..."}

错误码表迁移来源：old_project/backend/docs/API.md 第 5 节。
"""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.common.clock import utc_now_iso


def envelope(
    code: str = "OK",
    message: str = "success",
    data: Any = None,
    trace_id: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "data": data,
        "traceId": trace_id,
        "timestamp": utc_now_iso(),
    }


def ok(data: Any = None, trace_id: str = "") -> dict[str, Any]:
    """成功封套（对齐旧 ApiResponse.success）。"""
    return envelope(code="OK", message="success", data=data, trace_id=trace_id)


def trace_of(request: Request) -> str:
    return getattr(request.state, "trace_id", "") or "unknown"


def error_response(
    code: str,
    message: str,
    status_code: int,
    *,
    trace_id: str = "",
    data: Any = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=envelope(code=code, message=message, data=data, trace_id=trace_id),
    )
