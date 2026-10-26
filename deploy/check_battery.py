#!/usr/bin/env python3
"""电池 BMS 连通性自检（只读）。

用法：
    .venv/bin/python deploy/check_battery.py
    .venv/bin/python deploy/check_battery.py --config configs/hardware.yaml --port /dev/ttyS5

它导入**生产驱动**（ModbusSerialBatteryDriver）读一轮寄存器，因此能一次区分四类故障：

- 配置写错      → ``config_invalid: ...``
- 权限不足      → ``/dev/ttyS5 打不开... Permission denied``
- 串口被占用    → ``/dev/ttyS5 打不开... Device or resource busy``
- 接线/从站问题 → ``crc_mismatch / short_response / modbus_exception_*``

只发 FC03 读请求，**不写任何寄存器**——BMS 的写命令可能改保护参数，误发代价远高于
传感器，所以这个脚本永不写。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.robot_core.devices.battery_serial import ModbusSerialBatteryDriver  # noqa: E402
from app.robot_core.contracts import HealthState  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="电池 BMS 只读自检")
    parser.add_argument("--config", default=str(ROOT / "configs" / "hardware.yaml"))
    parser.add_argument("--port", default=None, help="覆盖配置里的串口，例如 /dev/ttyS5")
    args = parser.parse_args()

    values = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    battery = dict((values.get("devices") or {}).get("battery") or {})
    if args.port:
        battery["port"] = args.port
    if battery.get("transport") != "serial":
        print(f"配置里 devices.battery.transport = {battery.get('transport')!r}，本脚本只用于 serial")
        return 2

    driver = ModbusSerialBatteryDriver.from_mapping(battery)
    settings = driver.settings
    if settings is None:
        print(f"配置无法解析：{driver.config_error}")
        return 2
    stopbits = int(settings.stopbits) if float(settings.stopbits).is_integer() else settings.stopbits
    print(f"串口 {settings.port} @ {settings.baudrate} "
          f"{settings.bytesize}{settings.parity}{stopbits}，"
          f"从站 {settings.slave} 0x{settings.register:04X} x{settings.count}")

    state = asyncio.run(driver.read_state())
    print(f"健康：{state.health.value}   质量：{state.quality}")
    if state.values:
        print("读数：")
        for name, value in state.values.items():
            print(f"  {name:>22} = {value}")
        percent = state.values.get("battery_percent")
        if percent is not None and percent < 20:
            print(f"提醒：当前电量 {percent}%，低于安全线，请安排充电")
    if state.raw_fault_code:
        print(f"故障：{state.raw_fault_code}")
    return 0 if state.health in {HealthState.OK, HealthState.DEGRADED} else 1


if __name__ == "__main__":
    raise SystemExit(main())
