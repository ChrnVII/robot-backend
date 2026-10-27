from __future__ import annotations

from typing import Any, Dict

from app.robot_core.common import utc_now
from app.robot_core.contracts import CommandResult, DeviceCommand, DeviceState, HealthState


class SimulatedDeviceDriver:
    def __init__(self, device_id: str, initial_values: Dict[str, Any] | None = None):
        self.device_id = device_id
        self.opened = False
        # 模拟驱动不提供电量：编造一个常数（此前是 87%）会被前端当成真实读数，
        # 且无法与「BMS 未接」区分。读不到就上报空值，页面显示横杠。
        self.values: Dict[str, Any] = initial_values or {
            "moving": False, "position_m": 0.0,
        }

    async def open(self) -> None:
        self.opened = True

    async def self_test(self) -> DeviceState:
        return await self.read_state()

    async def read_state(self) -> DeviceState:
        health = HealthState.OK if self.opened else HealthState.UNAVAILABLE
        return DeviceState(self.device_id, health, utc_now(), "simulator", dict(self.values))

    async def execute(self, command: DeviceCommand) -> CommandResult:
        if not self.opened:
            return CommandResult(command.command_id, "rejected", reject_reason="device_not_open")
        if command.action == "stop":
            self.values["moving"] = False
        elif command.action == "move":
            self.values["moving"] = True
            self.values["position_m"] += float(command.payload.get("distance_m", 0.0))
            self.values["moving"] = False
        else:
            self.values[command.action] = dict(command.payload)
        return CommandResult(command.command_id, "succeeded", details={"device": self.device_id, "values": dict(self.values)})

    async def close(self) -> None:
        self.values["moving"] = False
        self.opened = False
