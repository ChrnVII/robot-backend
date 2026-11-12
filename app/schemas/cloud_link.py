"""云端接入域的出入参（camelCase 线上契约）。

这里只描述页面会传的字段；cloud-agent 返回的是状态快照，字段随版本演进，
用自由字典 + camelize 透传，避免两边每加一个指标就要改一次 schema。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CloudConfigUpdate(BaseModel):
    """部分更新：只处理显式给出的字段，未给的不动。"""

    server: str | None = Field(default=None, max_length=512)
    root: str | None = Field(default=None, max_length=4096)
    name: str | None = Field(default=None, max_length=128)
    heartbeatInterval: float | None = Field(default=None, gt=0, le=3600)
    maxConcurrentUploads: int | None = Field(default=None, ge=1, le=32)
    verifyTls: bool | None = None
    logLevel: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None


class CloudCredentialRequest(BaseModel):
    """手工填凭据：对应云端 `issue-token` 的输出。"""

    server: str = Field(min_length=1, max_length=512)
    token: str = Field(min_length=1, max_length=512)
    agentId: str | None = Field(default=None, max_length=128)
    root: str | None = Field(default=None, max_length=4096)
    name: str | None = Field(default=None, max_length=128)


class CloudEnrollRequest(BaseModel):
    server: str | None = Field(default=None, max_length=512)
    name: str | None = Field(default=None, max_length=128)
    root: str | None = Field(default=None, max_length=4096)
