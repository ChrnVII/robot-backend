"""本地 backend 的 HTTP 客户端：云端命令落到本机设备的唯一出口。

为什么是 HTTP 而不是直接调函数：cloud-agent 是**独立进程**（`python -m
app.cloud_agent`），backend 是另一个进程。计划书里写的 `MediaGateway` 是
"同进程"假设下的形态；这里是它在这个部署形态下的等价物——一条到本机 backend
的短链路，不带任何业务逻辑。

三条纪律：

1. **所有失败都翻译成明确的错误码。** 云端要能区分"相机不通"（换个时间重试）、
   "有人正在本地操作"（让操作者退让）、"参数不对"（别重试）。一律 500 是没用的。
2. **租约由这里申请、这里释放。** 云台命令必须带租约，否则本地人工一操作就
   两边打架——robot-core 的仲裁是"本地人工 > 远程人工 > 自主任务"，这个顺序
   只有在命令真的走租约时才成立。
3. **时间戳不给。** 命令的有效期由 backend 盖章（见 `ControlService`），
   因为工控机的时钟可能和浏览器差好几秒。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import AgentCommandError
from .protocol import ErrorCode

logger = logging.getLogger("robot-cloud-agent")

DEFAULT_TIMEOUT = 8.0
SNAPSHOT_TIMEOUT = 15.0


class LocalBackend:
    """backend 的薄客户端。连接池复用一个客户端，`close()` 时一起释放。"""

    def __init__(self, base_url: str, token: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # --- 基础设施 -------------------------------------------------------

    def _connection(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Accept": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers)
        return self._client

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        raw: bool = False,
    ) -> Any:
        limit = timeout if timeout is not None else self._timeout
        try:
            response = await self._connection().request(
                method, path, json=json, params=params, timeout=limit
            )
        except httpx.HTTPError as exc:
            # 连不上本机 backend 与"相机坏了"是两件事：前者是配置/进程问题，
            # 后者是硬件问题。混成一个错误码会把排查引向相机。
            raise AgentCommandError(
                ErrorCode.BACKEND_UNREACHABLE,
                f"连不上本机 backend {self.base_url}（{method} {path}）：{exc}",
            ) from exc

        if raw:
            if response.status_code >= 400:
                raise _translate(response, path)
            return response

        payload = _json_or_empty(response)
        if response.status_code >= 400:
            raise _translate(response, path, payload)
        # backend 的统一封套：{"code": "OK", "data": ...}
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    async def probe(self) -> tuple[bool, str]:
        """确认 backend_url 指向的确实是 robot backend。

        为什么用 `/api/v1/cameras`：这是 robot backend 独有的只读路由，云端
        server 上没有它。所以它同时能回答两个问题——"通不通"和"是不是同一类服务"。
        配错最常见的形态就是把它指向了云端 server 的端口（8000 而不是 8100），
        那种情况下所有本地请求都会得到 404，却看起来像"设备不支持"。
        """
        try:
            response = await self._connection().get("/api/v1/cameras", timeout=3.0)
        except httpx.HTTPError as exc:
            return False, f"连不上 {self.base_url}：{exc}"
        if response.status_code == 404:
            return False, (
                f"{self.base_url} 上没有 /api/v1/cameras —— 这个地址不像 robot backend"
                "（常见误配：填成了云端 server 的端口；robot backend 约定 8100）"
            )
        if response.status_code >= 400:
            return False, f"{self.base_url}/api/v1/cameras 返回 HTTP {response.status_code}"
        return True, "ok"

    # --- 租约 -----------------------------------------------------------

    async def acquire_lease(self, holder: str, mode: str = "manual_remote") -> dict[str, Any]:
        data = await self._call(
            "POST", "/api/v1/control/leases", json={"holder": holder, "mode": mode}
        )
        if not isinstance(data, dict) or not lease_id_of(data):
            raise AgentCommandError(ErrorCode.INTERNAL, f"租约响应里没有 leaseId：{data!r}")
        return data

    async def release_lease(self, lease_id: str, reason: str = "released") -> None:
        try:
            await self._call(
                "DELETE",
                f"/api/v1/control/leases/{lease_id}",
                params={"reason": reason[:200]},
            )
        except AgentCommandError:
            # 释放失败不影响结果：租约有 TTL，最坏情况是等它自然过期。
            logger.debug("释放租约 %s 失败，等待其自然过期", lease_id)

    # --- 设备命令 -------------------------------------------------------

    async def command(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        lease_id: str,
        idempotency_key: str,
        trace_id: str,
        target: str = "camera",
    ) -> dict[str, Any]:
        body = {
            "target": target,
            "action": action,
            "issuer": "cloud-agent",
            "leaseId": lease_id,
            "idempotencyKey": idempotency_key[:200],
            "traceId": trace_id[:120],
            "payload": payload,
        }
        data = await self._call("POST", "/api/v1/devices/commands", json=body)
        return data if isinstance(data, dict) else {"raw": data}

    # --- 只读查询 -------------------------------------------------------

    async def robot_state(self) -> dict[str, Any]:
        """本机健康快照（robot-core 的 HEALTH）。状态上报的唯一数据源。

        每秒一次本机回环 HTTP 是可以接受的（毫秒级、不跨网），换来的是
        "状态只有一处真相"——不再由 agent 自己拼一份、backend 拼一份。
        """
        data = await self._call("GET", "/api/v1/robot/state", timeout=3.0)
        return data if isinstance(data, dict) else {}

    async def presets(self, camera_id: str | None = None) -> dict[str, Any]:
        params = {"cameraId": camera_id} if camera_id else None
        data = await self._call("GET", "/api/v1/cameras/presets", params=params)
        return data if isinstance(data, dict) else {"items": []}

    async def snapshot(self, camera_id: str | None = None) -> tuple[bytes, dict[str, Any]]:
        params = {"cameraId": camera_id} if camera_id else None
        response = await self._call(
            "GET", "/api/v1/cameras/snapshot", params=params, timeout=SNAPSHOT_TIMEOUT, raw=True
        )
        body = response.content
        if not body:
            raise AgentCommandError(ErrorCode.CAMERA_UNAVAILABLE, "相机返回了空图像")
        return body, {
            "size": len(body),
            "captured_at": response.headers.get("x-camera-captured-at") or "",
            "content_type": response.headers.get("content-type") or "image/jpeg",
        }


def mint_local_access_token() -> str:
    """给"本进程访问本机 backend"签一个短期 access token。

    为什么自己签而不是让用户填：cloud-agent 和 backend 是同一台机器、同一份
    `ROBOT_JWT_SECRET`，签名成本几乎为零，而让人手工填一个一小时就过期的
    token 是错误的可用性设计。取不到（没装 backend 依赖 / 鉴权关闭）时返回空串，
    由调用方按"不需要令牌"处理。
    """
    try:
        from app.common.config import get_settings
        from app.security.auth import create_access_token
    except Exception:  # noqa: BLE001 - 只装了 cloud_agent 的极简部署
        return ""
    try:
        settings = get_settings()
        if not settings.auth_enabled:
            return ""
        return create_access_token("cloud-agent", "service", settings)
    except Exception:  # noqa: BLE001 - 签名失败就当作不需要令牌，让错误在更近的地方暴露
        return ""


def lease_id_of(data: dict[str, Any]) -> str:
    """从租约响应里取 id；取不到说明后端契约变了，调用方据此报 internal。"""
    for key in ("leaseId", "lease_id", "id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _json_or_empty(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _translate(response: httpx.Response, path: str, payload: Any = None) -> AgentCommandError:
    """把 backend 的错误翻成云端能用的错误码。

    这里的分支不是"还原后端错误"，而是回答云端真正关心的两个问题：
    能不能重试、要不要人介入。
    """
    body = payload if isinstance(payload, dict) else _json_or_empty(response)
    code = str(body.get("code") or "").lower()
    message = str(body.get("message") or response.reason_phrase or "")
    # 一定带上"哪个请求"：地址配错时，只有一句 "Not Found" 是查不出东西的。
    where = f"{response.request.method} {response.url}"

    if response.status_code == 409:
        # 409 一律是"当前状态不允许"：租约被本地人工占着，或者命令被仲裁拒了。
        if "held" in message.lower() or "lease" in message.lower():
            return AgentCommandError(ErrorCode.CONTROL_HELD, f"控制权不在云端：{message}")
        return AgentCommandError(ErrorCode.CONTROL_HELD, message or "命令被本机仲裁拒绝")
    if response.status_code in (400, 422):
        return AgentCommandError(
            ErrorCode.INVALID_ARGUMENTS, f"{where} 参数不合法：{message}"
        )
    if response.status_code in (401, 403):
        return AgentCommandError(
            ErrorCode.FORBIDDEN, f"本机后端拒绝了凭据（{path}）：{message}"
        )
    if response.status_code == 404:
        return AgentCommandError(
            ErrorCode.CAMERA_UNAVAILABLE,
            f"{where} 在 backend 上不存在（路径不对，或 backend 版本过旧）：{message}",
        )
    if response.status_code >= 500:
        return AgentCommandError(
            ErrorCode.CAMERA_UNAVAILABLE, f"{where} 返回 HTTP {response.status_code}：{message}"
        )
    return AgentCommandError(
        ErrorCode.INTERNAL, message or f"后端返回 {response.status_code}（{code or 'unknown'}）"
    )
