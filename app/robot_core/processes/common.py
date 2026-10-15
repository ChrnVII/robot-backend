from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

from app.robot_core.common.config import PlatformConfig, load_config


def parser(service: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog=service)
    result.add_argument("--config")
    result.add_argument("--check", action="store_true", help="validate configuration and dependencies, then exit")
    return result


def configure_logging(config: PlatformConfig, service: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(config.values["runtime"]["log_level"]).upper()),
        format=f"%(asctime)s %(levelname)s {service} %(message)s",
    )


def check_output(service: str, config: PlatformConfig, extra: dict) -> None:
    print(json.dumps({"service": service, "status": "ok", "robotId": config.robot_id, **extra}, ensure_ascii=False))


def run(coroutine: Callable[[], Awaitable[None]]) -> None:
    try:
        asyncio.run(coroutine())
    except KeyboardInterrupt:
        pass

