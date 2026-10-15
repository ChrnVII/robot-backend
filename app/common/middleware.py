"""X-Trace-Id 中间件。

与旧 Java TraceIdFilter 行为一致：沿用请求头中合法的 [A-Za-z0-9._:-]{1,128}，
否则生成新的 trace id；响应头回写 X-Trace-Id，业务层通过 request.state.trace_id 读取。
"""

import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

TRACE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def new_trace_id() -> str:
    return f"trace-{uuid.uuid4().hex[:16]}"


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("X-Trace-Id", "")
        trace_id = incoming if TRACE_ID_PATTERN.fullmatch(incoming) else new_trace_id()
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response
