from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Dict

from datetime import datetime, timedelta, timezone

from app.robot_core.common import new_id, utc_now
from app.robot_core.contracts import CommandResult, DeviceCommand, DeviceState, Event, HealthState
from app.robot_core.control import LeaseManager, SafetySupervisor
from app.robot_core.control.safety import CommandRejected
from app.robot_core.devices import DeviceDriver
from app.robot_core.storage import PlatformStore


class RobotCore:
    """Owns device writers, control arbitration, safety checks, and command audit."""

    def __init__(self, store: PlatformStore, devices: Dict[str, DeviceDriver], *, lease_ttl_ms: int = 3000,
                 max_motion_distance_m: float = 2.0):
        self.store = store
        self.devices = devices
        self.leases = LeaseManager(store, lease_ttl_ms)
        self.safety = SafetySupervisor(self.leases, max_motion_distance_m)
        self.state = "booting"
        self._poll_task = None
        self._poll_interval_seconds = 5.0

    async def start(self) -> None:
        self.state = "self_checking"
        unavailable = False
        for device in self.devices.values():
            try:
                await device.open()
                state = await device.self_test()
            except Exception as error:
                state = DeviceState(
                    device.device_id, HealthState.UNAVAILABLE, utc_now(), "robot-core",
                    {}, quality="invalid", raw_fault_code=str(error)[:256],
                )
            self.store.upsert_device_state(state)
            if state.health.value not in {"ok", "degraded"}:
                unavailable = True
        self.state = "degraded" if unavailable else "idle"
        self._poll_task = asyncio.create_task(self._poll_states())

    async def _poll_states(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_seconds)
            for device in self.devices.values():
                try:
                    state = await device.read_state()
                    self.store.upsert_device_state(state)
                except Exception:
                    # A failed poll must not bring down the control process.
                    continue
            latest = self.store.device_state("environment")
            if self.state == "degraded" and latest and latest["health"] == "ok":
                self.state = "idle"

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for device in self.devices.values():
            await device.close()
        self.state = "stopped"

    async def execute(self, command: DeviceCommand) -> CommandResult:
        try:
            self.safety.validate(command)
            device = self.devices.get(command.target)
            if device is None:
                raise CommandRejected("device_unknown")
            created = self.store.save_command(command)
            if not created:
                existing = self.store.command_by_idempotency_key(command.idempotency_key)
                return CommandResult(
                    str(existing["command_id"]), "duplicate",
                    details={"original_state": existing["state"], "original_result": existing["result"]},
                    reject_reason="idempotency_key_exists",
                )
            result = await device.execute(command)
        except CommandRejected as error:
            created = self.store.save_command(command, state="rejected", reject_reason=str(error))
            if not created:
                existing = self.store.command_by_idempotency_key(command.idempotency_key)
                return CommandResult(
                    str(existing["command_id"]), "duplicate",
                    details={"original_state": existing["state"], "original_result": existing["result"]},
                    reject_reason="idempotency_key_exists",
                )
            result = CommandResult(command.command_id, "rejected", reject_reason=str(error))
        self.store.finish_command(result)
        self.store.append_event(Event("command.completed", "robot-core", command.trace_id, asdict(result)))
        return result

    async def emergency_stop(self, reason: str, trace_id: str) -> None:
        self.safety.emergency = True
        self.state = "emergency"
        lease = self.leases.acquire("robot-core", "emergency")
        now = datetime.now(timezone.utc)
        for target in ("motion", "lift"):
            device = self.devices.get(target)
            if device is None:
                continue
            command = DeviceCommand(
                target=target, action="stop", payload={"reason": reason}, issuer="robot-core",
                lease_id=lease.lease_id, issued_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=1)).isoformat(),
                idempotency_key=new_id("emergency"), trace_id=trace_id,
            )
            await self.execute(command)
        self.store.append_event(Event("robot.emergency", "robot-core", trace_id, {"reason": reason, "at": utc_now()}))

    async def health(self) -> dict:
        devices = {}
        for name, device in self.devices.items():
            state = await device.read_state()
            self.store.upsert_device_state(state)
            devices[name] = state.as_dict()
        return {"service": "robot-core", "state": self.state, "timestamp": utc_now(), "devices": devices}

    def device_state(self, device_id: str) -> dict:
        state = self.store.device_state(device_id)
        if state is None:
            raise KeyError(f"device state not found: {device_id}")
        return state
