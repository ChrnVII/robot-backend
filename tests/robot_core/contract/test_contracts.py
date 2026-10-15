import unittest
from datetime import datetime, timedelta, timezone

from app.robot_core.contracts import ContractError, DeviceCommand, InferenceResponse


class ContractsTest(unittest.TestCase):
    def test_command_requires_increasing_expiry(self):
        now = datetime.now(timezone.utc)
        with self.assertRaises(ContractError):
            DeviceCommand("motion", "stop", {}, "operator", "lease", now.isoformat(), (now - timedelta(seconds=1)).isoformat())

    def test_inference_failure_cannot_be_disguised_as_normal(self):
        response = InferenceResponse("req", "chicken_counting", "capability_unavailable", "none", suggested_action="install model")
        self.assertEqual(response.status, "capability_unavailable")
        with self.assertRaises(ContractError):
            InferenceResponse("req", "chicken_counting", "simulated", "none")


if __name__ == "__main__":
    unittest.main()

