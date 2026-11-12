"""cloud-agent 进程入口。

    .venv/bin/python -m app.cloud_agent --config configs/cloud-agent.toml

与 monitor 的 agent 不同，这不是"跑一次、传完就退"的脚本，而是常驻守护进程：

- 没有凭据也照常启动，停在 `unregistered`，等页面填地址 / 填 token / 发起注册；
- 控制面只开 UDS（`run/cloud-agent.sock`），不监听任何 TCP 端口；
- 退出由 SIGTERM / SIGINT 触发，先取消注册与上传，再释放客户端。

CLI 参数保留，是因为首次部署与排障都需要一条不依赖页面的启动路径。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .agent import CloudAgent
from .config import AgentConfig, ConfigError, default_config_path, load_config
from .control import AgentControl, ControlServer
from .state import install_log_buffer


def require_linux(platform_name: str | None = None) -> None:
    """生产入口只接受 Linux。不是 "linux" 就直接退出。

    路径沙箱、只读挂载、POSIX 文件语义全都按 Linux 设计。
    单元测试与端到端测试不经过这个入口，所以在任意开发机上仍然可以跑。
    """
    name = platform_name if platform_name is not None else sys.platform
    if name == "linux":
        return
    print(
        f"cloud-agent 只支持 Linux，当前平台是 {name}。\n"
        "云端的 server 与 robot 的工控机都按 Linux 部署；非 Linux 上的行为不做保证。\n"
        "（单元测试与控制面测试不走这个入口，可跨平台运行。）",
        file=sys.stderr,
    )
    raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cloud_agent",
        description="把工控机目录只读共享给云端，并在本地提供状态/控制面",
    )
    parser.add_argument("--config", default=None, help="TOML 配置文件路径")
    parser.add_argument("--server", default=None, help="云端地址，https:// 或 wss:// 均可")
    parser.add_argument("--token", default=None, help="设备 token（云端 issue-token 签发）")
    parser.add_argument("--root", default=None, help="要共享的目录（任意路径，只读）")
    parser.add_argument("--name", default=None, help="本机名称，仅用于云端展示")
    parser.add_argument("--state-dir", default=None, help="凭据目录，默认项目内 .cloud-agent/")
    parser.add_argument("--control-socket", default=None, help="本地控制面 UDS 路径")
    parser.add_argument("--log-dir", default=None, help="日志目录（不传则只输出到 stderr）")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="同时上传的分片数，默认 3（再高会让云端的磁盘寻道成为瓶颈）",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 TLS 证书校验（只用于自签证书的内网测试）",
    )
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="不启动本地控制面（仅用于排查控制面本身的问题）",
    )
    return parser


def setup_logging(level: str, log_dir: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_dir:
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(directory / "cloud-agent.log", encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    # 页面读的是内存环形缓冲，不依赖 journald 的权限与轮转
    install_log_buffer()


def _resolve_config(args: argparse.Namespace) -> AgentConfig:
    path = args.config
    if path is None:
        candidate = default_config_path()
        path = str(candidate) if candidate.is_file() else None
    try:
        config = load_config(
            path,
            server=args.server,
            token=args.token,
            root=args.root,
            name=args.name,
            log_dir=args.log_dir,
            log_level=args.log_level,
            state_dir=args.state_dir,
            control_socket=args.control_socket,
            max_concurrent_uploads=args.concurrency,
            verify_tls=False if args.insecure else None,
        )
        config.validate()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return config


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    require_linux()

    config = _resolve_config(args)
    setup_logging(config.log_level, config.log_dir)
    logger = logging.getLogger("robot-cloud-agent")

    try:
        asyncio.run(_run(config, control_enabled=not args.no_control))
    except KeyboardInterrupt:
        logger.info("收到中断，退出")


async def _run(config: AgentConfig, *, control_enabled: bool = True) -> None:
    agent = CloudAgent(config)
    control_server: ControlServer | None = None

    if control_enabled:
        control_server = ControlServer(config.control_socket, AgentControl(agent))
        try:
            await control_server.start()
        except Exception as exc:
            print(f"控制面启动失败：{exc}", file=sys.stderr)
            raise SystemExit(2) from exc

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, agent.stop)
        except NotImplementedError:  # pragma: no cover - Windows 开发机
            signal.signal(sig, lambda *_: agent.stop())

    try:
        await agent.run()
    finally:
        if control_server is not None:
            await control_server.close()


if __name__ == "__main__":
    main()
