"""cloud-agent 配置：TOML 文件 + 环境变量 + 命令行，外加运行期落盘。

优先级：命令行参数 > 环境变量 > TOML 文件 > 默认值。

与 monitor 的 agent 相比，这里多了三件事：

1. **配置可写回**。页面改地址、共享目录、并发数时，由本进程独占写入 TOML，
   backend 不碰文件——避免两个进程同时改同一份配置。
2. **凭据不进 TOML**。token 仍然只存在 `credentials.py` 管理的槽里（0600），
   配置文件里只有非敏感项，可以放心被页面读取。
3. **地址推导规则原样保留**：`https://host` 与 `wss://host/ws/agent` 都必须能
   推出 `ws_url` 与 `http_base`。写错不会报错，只会表现为"能连上但上传 404"。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from urllib.parse import urlsplit

WS_PATH = "/ws/agent"
ENV_PREFIX = "ROBOT_CLOUD_AGENT_"

# 只允许写回这些字段：token / agent_id / instance_id 属于凭据，绝不能落进 TOML。
PERSISTED_FIELDS = (
    "server",
    "root",
    "name",
    "heartbeat_interval",
    "max_concurrent_uploads",
    "verify_tls",
    "log_dir",
    "log_level",
    "state_dir",
    "control_socket",
    "backend_url",
)


class ConfigError(Exception):
    pass


@dataclass
class AgentConfig:
    server: str = ""
    # 仅用于命令行 / 环境变量覆盖；不写回文件，也不在页面展示
    token: str = ""
    # 共享目录：任意绝对路径。为空表示还没配置，进程会停在"未注册/未配置"状态。
    root: str = ""
    name: str | None = None
    heartbeat_interval: float = 15.0
    max_concurrent_uploads: int = 3
    verify_tls: bool = True
    # 凭据目录；生产由环境变量指到 /var/lib/robot/cloud-agent
    state_dir: str | None = None
    # 注册后由云端分配，轮换 token 时要带上
    agent_id: str | None = None
    # 云端的身份，与地址无关。换地址时靠它认出"还是那一台"。
    instance_id: str | None = None
    log_dir: str | None = None
    log_level: str = "INFO"
    open_timeout: float = 10.0
    http_timeout: float = 60.0
    # 本地控制面（UDS）。相对路径按进程工作目录解析。
    control_socket: str = "run/cloud-agent.sock"
    # 本地 backend 的 HTTP 地址。媒体上行要连它的 /ws/camera/preview，
    # 云台/预置点/快照要打它的设备接口——这几件事都发生在 backend 进程里，
    # 不能在本进程内直接调函数。
    backend_url: str = "http://127.0.0.1:8000"
    # 给 backend 的令牌。留空时自动签发（同一份 ROBOT_JWT_SECRET）；
    # 只有 backend 与 agent 用了不同 secret 时才需要手填。
    backend_token: str = ""
    # 本次启动读到的配置文件路径；页面保存时写回同一个文件。
    config_path: str | None = None

    def validate(self) -> None:
        """只校验"给了的值是否合法"。

        server / root / token 是否齐全不在这里判断：它们可以从凭据文件补，
        由运行期的 supervisor 统一解析并给出更准确的报错。
        """
        if self.max_concurrent_uploads < 1:
            raise ConfigError("max_concurrent_uploads 至少为 1")
        if self.heartbeat_interval <= 0:
            raise ConfigError("heartbeat_interval 必须大于 0")
        if self.control_socket.strip() == "":
            raise ConfigError("control_socket 不能为空")
        if not self.backend_url.strip().startswith(("http://", "https://")):
            raise ConfigError(f"backend_url 必须是 http(s) 地址：{self.backend_url!r}")
        if self.server.strip():
            _ = self.http_base  # 协议写错要在这里就炸，而不是等到连的时候

    # --- 派生 URL -------------------------------------------------------

    @property
    def _parts(self):
        return urlsplit(self.server.strip())

    @property
    def ws_url(self) -> str:
        parts = self._parts
        if parts.scheme in ("ws", "wss"):
            if parts.path.rstrip("/").endswith(WS_PATH):
                return self.server.strip()
            return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}{WS_PATH}"
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}{self._base_path()}{WS_PATH}"

    @property
    def http_base(self) -> str:
        parts = self._parts
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
        if scheme not in ("http", "https"):
            raise ConfigError(f"server 的协议不对：{parts.scheme!r}，应为 http/https/ws/wss")
        return f"{scheme}://{parts.netloc}{self._base_path()}"

    def _base_path(self) -> str:
        path = self._parts.path.rstrip("/")
        path = path.removesuffix(WS_PATH)
        return path

    @property
    def sandbox_root(self) -> str:
        """共享目录的绝对路径（不校验是否存在，交给 Sandbox）。"""
        if not self.root.strip():
            raise ConfigError("缺少共享目录 root")
        return str(Path(self.root).expanduser().resolve())

    # --- 页面展示 -------------------------------------------------------

    def public_dict(self) -> dict:
        """给页面看的配置：不含 token、不含 agent_id。"""
        return {
            "server": self.server,
            "root": self.root,
            "name": self.name or "",
            "heartbeat_interval": self.heartbeat_interval,
            "max_concurrent_uploads": self.max_concurrent_uploads,
            "verify_tls": self.verify_tls,
            "log_level": self.log_level,
            "state_dir": str(resolve_state_dir(self)),
            "control_socket": self.control_socket,
            "backend_url": self.backend_url,
            "config_path": str(resolve_config_path(self)),
        }

    def replace(self, **changes: object) -> AgentConfig:
        updated = replace(self, **changes)
        updated.validate()
        return updated


# --- 路径解析 -----------------------------------------------------------


def project_root() -> Path | None:
    """从本文件向上找 pyproject.toml；wheel 安装时返回 None。"""
    here = Path(__file__).resolve()
    for parent in here.parents[:6]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def default_state_dir() -> Path:
    explicit = os.environ.get(ENV_PREFIX + "STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    root = project_root()
    if root is not None:
        return root / ".cloud-agent"
    return Path.home() / ".local" / "state" / "robot-cloud-agent"


def default_config_path() -> Path:
    explicit = os.environ.get(ENV_PREFIX + "CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    root = project_root()
    if root is not None:
        return root / "configs" / "cloud-agent.toml"
    return Path.home() / ".config" / "robot-cloud-agent" / "cloud-agent.toml"


def resolve_state_dir(config: AgentConfig) -> Path:
    return Path(config.state_dir).expanduser() if config.state_dir else default_state_dir()


def resolve_config_path(config: AgentConfig) -> Path:
    if config.config_path:
        return Path(config.config_path).expanduser()
    return default_config_path()


# --- 读取 ---------------------------------------------------------------


def load_config(path: str | os.PathLike[str] | None = None, **overrides: object) -> AgentConfig:
    """读 TOML + 环境变量 + 显式覆盖，返回配置。"""
    data: dict[str, object] = {}

    resolved_path = Path(path).expanduser() if path else None
    if resolved_path is not None:
        if not resolved_path.is_file():
            raise ConfigError(f"配置文件不存在：{resolved_path}")
        with open(resolved_path, "rb") as handle:
            data.update(tomllib.load(handle))
        data["config_path"] = str(resolved_path)

    valid = {f.name for f in fields(AgentConfig)}
    for name in valid:
        env_value = os.environ.get(ENV_PREFIX + name.upper())
        if env_value is not None:
            data[name] = env_value

    # 共享变量名兼容：backend 侧用 ROBOT_CLOUD_AGENT_SOCKET 指控制面，
    # 本进程按字段推导会得到 ..._CONTROL_SOCKET。两个都认，避免部署时
    # 只改一处、结果两端各连各的 socket。
    shared_socket = os.environ.get("ROBOT_CLOUD_AGENT_SOCKET")
    if shared_socket is not None and os.environ.get(ENV_PREFIX + "CONTROL_SOCKET") is None:
        data["control_socket"] = shared_socket

    for key, value in overrides.items():
        if value is not None and key in valid:
            data[key] = value

    # 环境变量是字符串，这里把数值/布尔字段转回来
    for key in ("heartbeat_interval", "open_timeout", "http_timeout"):
        if key in data and isinstance(data[key], str):
            data[key] = float(data[key])
    if "max_concurrent_uploads" in data and isinstance(data["max_concurrent_uploads"], str):
        data["max_concurrent_uploads"] = int(data["max_concurrent_uploads"])
    if "verify_tls" in data and isinstance(data["verify_tls"], str):
        data["verify_tls"] = data["verify_tls"].strip().lower() not in ("0", "false", "no")

    unknown = set(data) - valid
    if unknown:
        raise ConfigError(f"未知的配置项：{', '.join(sorted(unknown))}")

    return AgentConfig(**data)  # type: ignore[arg-type]


# --- 写回 ---------------------------------------------------------------


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    # JSON 字符串转义是 TOML basic string 的子集：引号、反斜杠、换行都能正确表达。
    return json.dumps(str(value), ensure_ascii=False)


def save_config(config: AgentConfig) -> Path:
    """把非敏感项写回 TOML（原子替换，0600）。

    唯一写文件的模块之一：共享目录永远是只读，这条不变量见
    `tests/test_handlers.py::test_no_write_mode_open_in_agent_source`。
    """
    target = resolve_config_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# 由 cloud-agent 写入；手工编辑也可以，改完重启或在页面上点「重新加载」。",
        "",
    ]
    for name in PERSISTED_FIELDS:
        value = getattr(config, name)
        if value is None:
            continue
        lines.append(f"{name} = {_toml_value(value)}")

    payload = "\n".join(lines) + "\n"
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - 非 POSIX 文件系统
        pass
    return target
