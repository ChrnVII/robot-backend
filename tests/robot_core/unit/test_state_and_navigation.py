import tempfile
import unittest
from pathlib import Path

from app.robot_core.mission import MissionStateMachine
from app.robot_core.mission.state_machine import InvalidTransition
from app.robot_core.navigation import Topology
from app.robot_core.storage import PlatformStore


class StateAndNavigationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PlatformStore(Path(self.temp.name) / "platform.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_state_transition_is_audited(self):
        machine = MissionStateMachine(self.store)
        machine.transition("self_checking", reason="boot", actor="robot-core", trace_id="trace")
        machine.transition("idle", reason="healthy", actor="robot-core", trace_id="trace")
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM events").fetchone()[0], 2)

    def test_invalid_transition_is_rejected(self):
        with self.assertRaises(InvalidTransition):
            MissionStateMachine(self.store).transition("executing", reason="skip", actor="test", trace_id="trace")

    def test_topology_plans_shortest_distance_without_commands(self):
        topology = Topology("v1", [{"id": "A"}, {"id": "B"}, {"id": "C"}], [
            {"from": "A", "to": "B", "distance": 1},
            {"from": "B", "to": "C", "distance": 1},
            {"from": "A", "to": "C", "distance": 5},
        ])
        self.assertEqual(topology.plan("A", "C"), ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()

