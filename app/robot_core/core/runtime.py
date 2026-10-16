from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Dict

from datetime import datetime, timedelta, timezone

from app.robot_core.common import new_id, utc_now
from app.robot_core.contracts import CommandResult, DeviceCommand, DeviceState, Event, HealthState
from app.robot_core.control import LeaseManager, SafetySupervisor
from app.robot_core.control.safety import CommandRejected
from app.robot_core.control.audit import EmergencyAudit
from app.robot_core.devices import DeviceDriver
from app.robot_core.storage import PlatformStore

logger = logging.getLogger(__name__)


class RobotCore:
    """Owns device writers, control arbitration, safety checks, and command audit."""

    def __init__(self, store: PlatformStore, devices: Dict[str, DeviceDriver], *, lease_ttl_ms: int = 3000,
                 max_motion_distance_m: float = 2.0, audit_directory: Path | None = None):
        self.store = store
        self.devices = devices
        self.leases = LeaseManager(store, lease_ttl_ms)
        self.safety = SafetySupervisor(self.leases, max_motion_distance_m)
        self.state = "booting"
        self._poll_task = None
        self._poll_interval_seconds = 5.0
        self._states: dict[str, DeviceState] = {}
        self._received_at: dict[str, str] = {}
        self._device_locks: dict[str, asyncio.Lock] = {}
        self.audit = EmergencyAudit(audit_directory)
        self._audit_task: asyncio.Task | None = None
        self._audit_wake = asyncio.Event()

    def _cache(self, state: DeviceState) -> None:
        self._states[state.device_id] = state
        self._received_at[state.device_id] = utc_now()

    async def _save_state(self, state: DeviceState) -> None:
        self._cache(state)
        try:
            await asyncio.to_thread(self.store.upsert_device_state, state)
        except Exception:
            logger.warning("state audit unavailable: %s", state.device_id, exc_info=True)

    async def _audit_loop(self) -> None:
        while True:
            self._audit_wake.clear()
            try:
                await asyncio.to_thread(self.audit.drain_once, self.store.persist_emergency)
            except Exception:
                logger.warning("emergency audit deferred; retained for retry", exc_info=True)
            try:
                await asyncio.wait_for(self._audit_wake.wait(), 2.0)
            except asyncio.TimeoutError:
                pass

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
            self._cache(state)
            if state.health.value not in {"ok", "degraded"}:
                unavailable = True
        self.state = "degraded" if unavailable else "idle"
        self._poll_task = asyncio.create_task(self._poll_states())
        self._audit_task = asyncio.create_task(self._audit_loop())

    async def _poll_states(self) -> None:
        while True:
            for device in self.devices.values():
                try:
                    state = await device.read_state()
                    await self._save_state(state)
                except Exception as error:
                    self._cache(DeviceState(
                        device.device_id, HealthState.UNAVAILABLE, utc_now(), "robot-core",
                        {}, quality="invalid", raw_fault_code=str(error)[:256],
                    ))
                    if self.state not in {"emergency", "stopped"}:
                        self.state = "degraded"
            if self.state == "degraded" and self._states and all(
                value.health in {HealthState.OK, HealthState.DEGRADED} for value in self._states.values()
            ):
                self.state = "idle"
            await asyncio.sleep(self._poll_interval_seconds)

    async def stop(self) -> None:
        if self._audit_task is not None:
            self._audit_task.cancel()
            await asyncio.gather(self._audit_task, return_exceptions=True)
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
        created = None
        try:
            self.safety.validate(command)
            device = self.devices.get(command.target)
            if device is None:
                raise CommandRejected("device_unknown")
            created = await asyncio.to_thread(self.store.save_command, command)
            if not created:
                existing = await asyncio.to_thread(self.store.command_by_idempotency_key, command.idempotency_key)
                return CommandResult(
                    str(existing["command_id"]), "duplicate",
                    details={"original_state": existing["state"], "original_result": existing["result"]},
                    reject_reason="idempotency_key_exists",
                )
            lock = self._device_locks.setdefault(command.target, asyncio.Lock())
            async with lock:
                # Re-check after DB/queue waits: an emergency may have preempted us.
                self.safety.validate(command)
                result = await device.execute(command)
                try:
                    self._cache(await asyncio.wait_for(device.read_state(), 0.2))
                except Exception:
                    logger.warning("state refresh after command failed: %s", command.target, exc_info=True)
        except CommandRejected as error:
            # An accepted command may have been preempted after its audit was saved.
            if created is None:
                created = await asyncio.to_thread(
                    self.store.save_command, command, state="rejected", reject_reason=str(error)
                )
            if not created:
                existing = await asyncio.to_thread(self.store.command_by_idempotency_key, command.idempotency_key)
                return CommandResult(
                    str(existing["command_id"]), "duplicate",
                    details={"original_state": existing["state"], "original_result": existing["result"]},
                    reject_reason="idempotency_key_exists",
                )
            result = CommandResult(command.command_id, "rejected", reject_reason=str(error))
        await asyncio.to_thread(self.store.finish_command, result)
        await asyncio.to_thread(
            self.store.append_event, Event("command.completed", "robot-core", command.trace_id, asdict(result))
        )
        return result

    async def emergency_stop(self, reason: str, trace_id: str) -> None:
        self.safety.emergency = True
        self.state = "emergency"
        lease, previous = self.leases.emergency_lease()
        now = datetime.now(timezone.utc)
        commands = []
        calls = []
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
            commands.append(command)
            calls.append(asyncio.wait_for(device.execute(command), timeout=2.0))
        # All stop requests are attempted concurrently, even when one driver fails.
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        results = [
            outcome if isinstance(outcome, CommandResult) else CommandResult(
                command.command_id, "failed", reject_reason=type(outcome).__name__
            ) for command, outcome in zip(commands, outcomes)
        ]
        event = Event("robot.emergency", "robot-core", trace_id, {"reason": reason, "at": utc_now()})
        payload = {
            "lease": asdict(lease), "previous_lease_id": previous.lease_id if previous else None,
            "commands": [asdict(command) for command in commands],
            "results": [asdict(result) for result in results], "event": asdict(event),
        }
        try:
            await asyncio.to_thread(self.audit.enqueue, payload)
        except OSError:
            logger.critical("emergency stopped devices; audit is retained in memory because disk is unavailable", exc_info=True)
        self._audit_wake.set()
        for command, result in zip(commands, results):
            if result.state in {"succeeded", "completed"}:
                try:
                    self._cache(await asyncio.wait_for(self.devices[command.target].read_state(), 0.2))
                except Exception:
                    logger.warning("state refresh after stop failed: %s", command.target)
        if any(result.state not in {"succeeded", "completed"} for result in results):
            raise RuntimeError("one or more devices did not confirm emergency stop")

    async def health(self) -> dict:
        devices = {name: state.as_dict() for name, state in self._states.items()}
        return {"service": "robot-core", "state": self.state, "timestamp": utc_now(), "devices": devices}

    def device_state(self, device_id: str) -> dict:
        state = self._states.get(device_id)
        if state is None:
            raise KeyError(f"device state not found: {device_id}")
        return {**state.as_dict(), "received_at": self._received_at[device_id]}
