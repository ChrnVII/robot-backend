"""云端接入域：状态、配置、凭据、注册、连接与进程操作。

页面上的每个按钮都对应这里的一个端点，最终变成 cloud-agent 控制面的一次 op：

    GET    /cloud-link/status
    GET    /cloud-link/logs?since=&limit=
    GET    /cloud-link/config
    PUT    /cloud-link/config
    POST   /cloud-link/config/reload
    GET    /cloud-link/credentials
    POST   /cloud-link/credentials
    DELETE /cloud-link/credentials?server=
    DELETE /cloud-link/credentials
    POST   /cloud-link/enroll
    POST   /cloud-link/enroll/cancel
    POST   /cloud-link/reconnect | pause | resume | rotate
    POST   /cloud-link/uploads/{upload_id}/cancel
    POST   /cloud-link/restart

注意 `DELETE /cloud-link/credentials` 用 query 而不是路径参数：云端地址含
`://` 与斜杠，塞进路径要么被转义得面目全非，要么直接 404。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_cloud_agent_gateway
from app.common.responses import ok, trace_of
from app.gateways.base import camelize
from app.gateways.cloud_agent import CloudAgentGateway
from app.schemas.cloud_link import (
    CloudConfigUpdate,
    CloudCredentialRequest,
    CloudEnrollRequest,
)

router = APIRouter()

# 线上 camelCase -> cloud-agent 配置文件的 snake_case
_CONFIG_FIELDS = {
    "server": "server",
    "root": "root",
    "name": "name",
    "heartbeatInterval": "heartbeat_interval",
    "maxConcurrentUploads": "max_concurrent_uploads",
    "verifyTls": "verify_tls",
    "logLevel": "log_level",
}


def _config_payload(body: CloudConfigUpdate) -> dict[str, Any]:
    given = body.model_dump(exclude_unset=True)
    return {_CONFIG_FIELDS[key]: value for key, value in given.items() if key in _CONFIG_FIELDS}


@router.get("/cloud-link/status", tags=["cloud-link"])
async def cloud_status(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("status")), trace_of(request))


@router.get("/cloud-link/logs", tags=["cloud-link"])
async def cloud_logs(
    request: Request,
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    data = await gateway.call("logs", since=since, limit=limit)
    return ok(camelize(data), trace_of(request))


@router.get("/cloud-link/config", tags=["cloud-link"])
async def cloud_config(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("get_config")), trace_of(request))


@router.put("/cloud-link/config", tags=["cloud-link"])
async def update_cloud_config(
    request: Request,
    body: CloudConfigUpdate,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    config = _config_payload(body)
    if not config:
        return ok(camelize(await gateway.call("get_config")), trace_of(request))
    data = await gateway.call("set_config", config=config)
    return ok(camelize(data), trace_of(request))


@router.post("/cloud-link/config/reload", tags=["cloud-link"])
async def reload_cloud_config(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("reload_config")), trace_of(request))


@router.get("/cloud-link/credentials", tags=["cloud-link"])
async def list_cloud_credentials(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("list_credentials")), trace_of(request))


@router.post("/cloud-link/credentials", tags=["cloud-link"])
async def put_cloud_credential(
    request: Request,
    body: CloudCredentialRequest,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    data = await gateway.call(
        "put_credential",
        server=body.server,
        token=body.token,
        agent_id=body.agentId,
        root=body.root,
        name=body.name,
    )
    return ok(camelize(data), trace_of(request))


@router.post("/cloud-link/credentials/activate", tags=["cloud-link"])
async def activate_cloud_credential(
    request: Request,
    server: str = Query(min_length=1, max_length=512),
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    data = await gateway.call("activate_credential", server=server)
    return ok(camelize(data), trace_of(request))


@router.delete("/cloud-link/credentials", tags=["cloud-link"])
async def delete_cloud_credential(
    request: Request,
    server: str | None = Query(default=None),
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    if server:
        data = await gateway.call("forget_credential", server=server)
    else:
        data = await gateway.call("clear_credentials")
    return ok(camelize(data), trace_of(request))


@router.post("/cloud-link/enroll", tags=["cloud-link"])
async def start_cloud_enroll(
    request: Request,
    body: CloudEnrollRequest | None = None,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    payload = body or CloudEnrollRequest()
    data = await gateway.call("enroll", server=payload.server, name=payload.name, root=payload.root)
    return ok(camelize(data), trace_of(request))


@router.post("/cloud-link/enroll/cancel", tags=["cloud-link"])
async def cancel_cloud_enroll(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("cancel_enroll")), trace_of(request))


@router.post("/cloud-link/reconnect", tags=["cloud-link"])
async def reconnect_cloud(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("reconnect")), trace_of(request))


@router.post("/cloud-link/pause", tags=["cloud-link"])
async def pause_cloud(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("pause")), trace_of(request))


@router.post("/cloud-link/resume", tags=["cloud-link"])
async def resume_cloud(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("resume")), trace_of(request))


@router.post("/cloud-link/rotate", tags=["cloud-link"])
async def rotate_cloud_token(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("rotate")), trace_of(request))


@router.post("/cloud-link/uploads/{upload_id}/cancel", tags=["cloud-link"])
async def cancel_cloud_upload(
    request: Request,
    upload_id: str,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    data = await gateway.call("cancel_upload", upload_id=upload_id)
    return ok(camelize(data), trace_of(request))


@router.post("/cloud-link/restart", tags=["cloud-link"])
async def restart_cloud_agent(
    request: Request,
    gateway: CloudAgentGateway = Depends(get_cloud_agent_gateway),
) -> dict:
    return ok(camelize(await gateway.call("restart")), trace_of(request))
