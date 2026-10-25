#!/usr/bin/env python3
"""环境传感器连通性自检（只读）。

用法：
    .venv/bin/python deploy/check_environment.py
    .venv/bin/python deploy/check_environment.py --config configs/hardware.yaml --port /dev/ttyS0

它导入**生产驱动**（ModbusSerialEnvironmentDriver）读一轮三路传感器，因此能一次
区分四类故障：

- 配置写错      → ``config_invalid: ...``
- 权限不足      → ``/dev/ttyS0 打不开... Permission denied``
- 串口被占用    → ``/dev/ttyS0 打不开... Device or resource busy``
- 接线/从站问题 → 某一路 ``crc_mismatch / no_response``

只发 FC03/FC04 读请求，不写任何寄存器。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.robot_core.devices.environment_serial import ModbusSerialEnvironmentDriver  # noqa: E402
from app.robot_core.contracts import HealthState  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="环境传感器只读自检")
    parser.add_argument("--config", default=str(ROOT / "configs" / "hardware.yaml"))
    parser.add_argument("--port", default=None, help="覆盖配置里的串口，例如 /dev/ttyS0")
    args = parser.parse_args()

    values = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    environment = dict((values.get("devices") or {}).get("environment") or {})
    if args.port:
        environment["port"] = args.port
    if environment.get("transport") != "serial":
        print(f"配置里 devices.environment.transport = {environment.get('transport')!r}，本脚本只用于 serial")
        return 2

    driver = ModbusSerialEnvironmentDriver.from_mapping(environment)
    settings = driver.settings
    stopbits = int(settings.stopbits) if float(settings.stopbits).is_integer() else settings.stopbits
    print(f"串口 {settings.port} @ {settings.baudrate} "
          f"{settings.bytesize}{settings.parity}{stopbits}，"
          f"{len(settings.channels)} 路从站")

    state = asyncio.run(driver.read_state())
    print(f"健康：{state.health.value}   质量：{state.quality}")
    for channel in settings.channels:
        names = ", ".join(field.name for field in channel.fields)
        print(f"  - 从站 {channel.slave:>3} 0x{channel.register:04X} x{channel.count} "
              f"[{channel.label or names}]")
    if state.values:
        print("读数：")
        for name, value in state.values.items():
            print(f"  {name:>18} = {value}")
    if state.raw_fault_code:
        print(f"故障：{state.raw_fault_code}")
    return 0 if state.health in {HealthState.OK, HealthState.DEGRADED} else 1


if __name__ == "__main__":
    raise SystemExit(main())
