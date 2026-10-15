import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.robot_core.contracts import DeviceCommand
from app.robot_core.control import LeaseManager, SafetySupervisor
from app.robot_core.control.leases import LeaseConflict
from app.robot_core.control.safety import CommandRejected
from app.robot_core.core import RobotCore
from app.robot_core.devices import SimulatedDeviceDriver
from app.robot_core.storage import PlatformStore


class ControlSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PlatformStore(Path(self.temp.name) / "platform.db")
        self.leases = LeaseManager(self.store, ttl_ms=3000)
        self.safety = SafetySupervisor(self.leases, max_motion_distance_m=2.0)

    def tearDown(self):
        self.temp.cleanup()

    def command(self, lease_id, **changes):
        now = datetime.now(timezone.utc)
        values = dict(
            target="motion", action="move", payload={"distance_m": 0.5}, issuer="operator",
            lease_id=lease_id, issued_at=now.isoformat(), expires_at=(now + timedelta(seconds=1)).isoformat(),
            idempotency_key="command-key", trace_id="trace-1",
        )
        values.update(changes)
        return DeviceCommand(**values)

    def test_lower_priority_control_cannot_preempt_local_operator(self):
        self.leases.acquire("local", "manual_local")
        with self.assertRaises(LeaseConflict):
            self.leases.acquire("scheduler", "automatic")

    def test_expired_command_is_rejected(self):
        lease = self.leases.acquire("local", "manual_local")
        now = datetime.now(timezone.utc)
        command = self.command(lease.lease_id, issued_at=(now - timedelta(seconds=2)).isoformat(), expires_at=(now - timedelta(seconds=1)).isoformat())
        with self.assertRaisesRegex(CommandRejected, "command_expired"):
            self.safety.validate(command)

    def test_motion_limit_is_enforced(self):
        lease = self.leases.acquire("local", "manual_local")
        with self.assertRaisesRegex(CommandRejected, "motion_limit_exceeded"):
            self.safety.validate(self.command(lease.lease_id, payload={"distance_m": 3.0}))

    def test_emergency_rejects_motion_but_allows_stop(self):
        lease = self.leases.acquire("local", "manual_local")
        self.safety.emergency = True
        with self.assertRaisesRegex(CommandRejected, "emergency_active"):
            self.safety.validate(self.command(lease.lease_id))
        self.safety.validate(self.command(lease.lease_id, action="stop"))

    def test_core_executes_and_audits_simulated_command(self):
        async def scenario():
            core = RobotCore(self.store, {"motion": SimulatedDeviceDriver("motion")})
            await core.start()
            lease = core.leases.acquire("operator", "manual_local")
            result = await core.execute(self.command(lease.lease_id))
            self.assertEqual(result.state, "succeeded")
            with self.store.connect() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM commands").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 1)
            await core.stop()
        asyncio.run(scenario())

    def test_duplicate_command_is_not_executed_twice(self):
        async def scenario():
            device = SimulatedDeviceDriver("motion")
            core = RobotCore(self.store, {"motion": device})
            await core.start()
            lease = core.leases.acquire("operator", "manual_local")
            first = self.command(lease.lease_id)
            first_result = await core.execute(first)
            second = self.command(lease.lease_id, command_id="different-command-id")
            second_result = await core.execute(second)
            self.assertEqual(first_result.state, "succeeded")
            self.assertEqual(second_result.state, "duplicate")
            self.assertEqual(second_result.command_id, first.command_id)
            self.assertEqual(device.values["position_m"], 0.5)
            await core.stop()
        asyncio.run(scenario())

    def test_rejected_duplicate_does_not_overwrite_original(self):
        async def scenario():
            device = SimulatedDeviceDriver("motion")
            core = RobotCore(self.store, {"motion": device})
            await core.start()
            lease = core.leases.acquire("operator", "manual_local")
            first = self.command(lease.lease_id)
            await core.execute(first)
            core.safety.emergency = True
            duplicate = self.command(lease.lease_id, command_id="duplicate-rejected")
            result = await core.execute(duplicate)
            self.assertEqual(result.state, "duplicate")
            stored = self.store.command_by_idempotency_key("command-key")
            self.assertEqual(stored["state"], "succeeded")
            await core.stop()
        asyncio.run(scenario())

    def test_emergency_stop_actively_stops_motion(self):
        async def scenario():
            motion = SimulatedDeviceDriver("motion")
            lift = SimulatedDeviceDriver("lift")
            core = RobotCore(self.store, {"motion": motion, "lift": lift})
            await core.start()
            lease = core.leases.acquire("operator", "manual_local")
            motion.values["moving"] = True
            lift.values["moving"] = True
            await core.emergency_stop("test", "trace-emergency")
            self.assertFalse(motion.values["moving"])
            self.assertFalse(lift.values["moving"])
            self.assertEqual(core.state, "emergency")
            self.assertNotEqual(core.leases.current.lease_id, lease.lease_id)
            await core.stop()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
