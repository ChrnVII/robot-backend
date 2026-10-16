import asyncio
import json
import threading

import pytest

from app.robot_core.core import RobotCore
from app.robot_core.devices import SimulatedDeviceDriver
from app.robot_core.control.audit import EmergencyAudit

pytestmark = pytest.mark.no_database


class OfflineStore:
    def upsert_device_state(self, state):
        raise RuntimeError("database offline")

    def persist_emergency(self, payload):
        raise RuntimeError("database offline")


def test_emergency_stops_devices_without_database_and_retains_audit(tmp_path):
    async def scenario():
        devices = {name: SimulatedDeviceDriver(name) for name in ("motion", "lift")}
        for device in devices.values():
            await device.open()
            device.values["moving"] = True
        core = RobotCore(OfflineStore(), devices, audit_directory=tmp_path)
        await asyncio.wait_for(core.emergency_stop("operator", "trace-stop"), 0.5)
        assert all(not device.values["moving"] for device in devices.values())
        assert core.safety.emergency and core.state == "emergency"
        spool = list(tmp_path.glob("*.json"))
        assert len(spool) == 1
        payload = json.loads(spool[0].read_text(encoding="utf-8"))
        assert len(payload["results"]) == 2
        with pytest.raises(RuntimeError):
            core.audit.drain_once(core.store.persist_emergency)
        assert spool[0].exists()
        recovered = []
        EmergencyAudit(tmp_path).drain_once(recovered.append)
        assert len(recovered) == 1 and not spool[0].exists()
    asyncio.run(scenario())


def test_emergency_attempts_lift_even_when_motion_driver_fails(tmp_path):
    class BrokenMotion(SimulatedDeviceDriver):
        async def execute(self, command):
            raise RuntimeError("drive fault")

    async def scenario():
        motion, lift = BrokenMotion("motion"), SimulatedDeviceDriver("lift")
        await lift.open()
        lift.values["moving"] = True
        core = RobotCore(OfflineStore(), {"motion": motion, "lift": lift}, audit_directory=tmp_path)
        with pytest.raises(RuntimeError, match="did not confirm"):
            await core.emergency_stop("test", "trace")
        assert not lift.values["moving"]
        assert list(tmp_path.glob("*.json"))
    asyncio.run(scenario())


def test_health_and_state_use_cache_without_polling_devices_or_database():
    async def scenario():
        device = SimulatedDeviceDriver("motion")
        await device.open()
        core = RobotCore(OfflineStore(), {"motion": device})
        core._cache(await device.read_state())

        async def forbidden():
            raise AssertionError("a read API must not poll hardware")
        device.read_state = forbidden
        assert (await core.health())["devices"]["motion"]["health"] == "ok"
        assert core.device_state("motion")["source"] == "simulator"
    asyncio.run(scenario())


def test_slow_command_audit_does_not_block_emergency(tmp_path):
    from datetime import datetime, timedelta, timezone
    from app.robot_core.contracts import DeviceCommand

    entered, release = threading.Event(), threading.Event()

    class SlowStore(OfflineStore):
        def save_command(self, command, **kwargs):
            entered.set()
            release.wait(2)
            raise RuntimeError("audit failed")

    async def scenario():
        device = SimulatedDeviceDriver("motion")
        await device.open()
        device.values["moving"] = True
        core = RobotCore(SlowStore(), {"motion": device}, audit_directory=tmp_path)
        lease, _ = core.leases.emergency_lease()
        now = datetime.now(timezone.utc)
        command = DeviceCommand("motion", "move", {}, "operator", lease.lease_id,
                                now.isoformat(), (now + timedelta(seconds=1)).isoformat())
        pending = asyncio.create_task(core.execute(command))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.wait_for(core.emergency_stop("test", "trace"), 0.5)
            assert not device.values["moving"]
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(scenario())


def test_successful_command_survives_state_refresh_failure():
    from datetime import datetime, timedelta, timezone
    from app.robot_core.contracts import DeviceCommand

    class Store(OfflineStore):
        def save_command(self, _command):
            return True

        def finish_command(self, result):
            self.result = result

        def append_event(self, event):
            pass

    class Driver(SimulatedDeviceDriver):
        async def read_state(self):
            raise RuntimeError("telemetry unavailable")

    async def scenario():
        store = Store()
        driver = Driver("motion")
        await driver.open()
        core = RobotCore(store, {"motion": driver})
        lease, _ = core.leases.emergency_lease()
        now = datetime.now(timezone.utc)
        result = await core.execute(DeviceCommand("motion", "stop", {}, "operator", lease.lease_id,
            now.isoformat(), (now + timedelta(seconds=1)).isoformat()))
        assert result.state in {"completed", "succeeded"}
        assert store.result == result
    asyncio.run(scenario())
