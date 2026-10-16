"""网络相机 HTTP 基建：PTZ 控制驱动与 JPEG 采集 provider 共用。

此前 ``devices/network_camera.py``（控制）与 ``sensing/network_camera.py``（采集）
各自维护同一份 digest/basic 认证构造与凭据脱敏逻辑，改一处极易漏另一处。
本模块把这类与业务无关的 HTTP 细节收敛为单一实现。
"""

from __future__ import annotations

import base64
import re
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    build_opener,
    urlopen,
)

_SECRET_QUERY = re.compile(r"(?i)(password|passwd|pwd)=([^&\s]+)")


def build_camera_opener(
    base_url: str,
    username: Optional[str],
    password: Optional[str],
    auth_scheme: str,
    opener: Optional[Callable[..., Any]] = None,
) -> Callable[..., Any]:
    """构造与 ``urlopen`` 同签名的 opener：digest 认证经 handler，其余走 urlopen。

    显式传入 opener（测试替身）时原样返回。
    """
    if opener is not None:
        return opener
    if auth_scheme == "digest" and username:
        manager = HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, base_url, username, password or "")
        return build_opener(HTTPDigestAuthHandler(manager)).open
    return urlopen


def basic_auth_header(
    username: Optional[str], password: Optional[str], auth_scheme: str
) -> Optional[str]:
    """basic 认证头；非 basic 方案或未配置用户名时返回 None。"""
    if auth_scheme == "basic" and username is not None:
        credentials = f"{username}:{password or ''}".encode()
        return "Basic " + base64.b64encode(credentials).decode("ascii")
    return None


def safe_url(value: str) -> str:
    """去除 URL 中的用户信息（用户名/密码），用于错误信息与日志。"""
    try:
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            host = parts.hostname or "unknown-host"
            if parts.port:
                host = f"{host}:{parts.port}"
            return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
    except ValueError:
        pass
    return value


def redact_secrets(text: str, *, url: Optional[str] = None) -> str:
    """脱敏错误文本：替换 URL 用户信息与查询串中的密码参数。"""
    if url is not None:
        text = text.replace(url, safe_url(url))
    return _SECRET_QUERY.sub(r"\1=***", text)
