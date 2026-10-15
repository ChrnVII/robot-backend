from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.robot_core.contracts import ContractError, InferenceRequest, InferenceResponse
from app.robot_core.inference import InferenceRuntime, ModelPackage, TestModelAdapter


class InferenceArchitectureTest(unittest.TestCase):
    def test_request_and_response_are_versioned_and_round_trip(self):
        request = InferenceRequest("platform_smoke_test", "asset-1", "run-1")
        restored = InferenceRequest.from_dict(request.as_dict())
        self.assertEqual(request, restored)
        response = InferenceRuntime({"platform_smoke_test": TestModelAdapter()})
        response.start()
        result = response.predict(request)
        self.assertEqual(result.status, "ok")
        self.assertEqual(InferenceResponse.from_dict(result.as_dict()), result)
        response.stop()

    def test_idempotency_replays_the_same_response(self):
        adapter = TestModelAdapter()
        runtime = InferenceRuntime({adapter.capability: adapter})
        runtime.start()
        request = InferenceRequest(adapter.capability, "asset-1", "run-1", idempotency_key="same")
        first = runtime.predict(request)
        second = runtime.predict(InferenceRequest(adapter.capability, "asset-1", "run-1", idempotency_key="same"))
        self.assertEqual(first, second)
        runtime.stop()

    def test_model_package_is_frozen_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "best.pt"
            artifact.write_bytes(b"model-v1")
            package_root = root / "heat-stress-v1"
            package = ModelPackage.create(package_root, artifact, model_id="heat_stress", version="1.0.0")
            loaded = ModelPackage.load(package_root, expected_model_id="heat_stress")
            self.assertEqual(package.sha256, loaded.sha256)
            with self.assertRaises((PermissionError, OSError)):
                (package_root / "best.pt").write_bytes(b"changed")

    def test_expired_request_has_fixed_timeout_response(self):
        adapter = TestModelAdapter()
        runtime = InferenceRuntime({adapter.capability: adapter})
        runtime.start()
        request = InferenceRequest(adapter.capability, "asset", "run", deadline_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        self.assertEqual(runtime.predict(request).status, "timeout")
        runtime.stop()


if __name__ == "__main__":
    unittest.main()
