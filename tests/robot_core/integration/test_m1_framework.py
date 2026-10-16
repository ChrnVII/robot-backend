import os
import tempfile
import unittest
from pathlib import Path

import yaml

from app.robot_core.common.config import load_config
from app.robot_core.inference import InferenceRuntime, TestModelAdapter
from app.robot_core.contracts import InferenceRequest
from app.robot_core.processes.robot_core import build

TEST_DSN = os.environ["ROBOT_TEST_DATABASE_URL"]


class M1FrameworkTest(unittest.TestCase):
    def test_dev_configuration_and_core_build(self):
        """以 configs/dev.yaml 为模板、测试库 DSN，验证核心可装配。"""
        source = Path(__file__).parents[3] / "configs" / "dev.yaml"
        values = yaml.safe_load(source.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            values["storage"]["database_url"] = TEST_DSN
            values["storage"]["database"] = TEST_DSN
            temp_config = Path(tmp) / "dev.yaml"
            temp_config.write_text(yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
            config = load_config(str(temp_config))
            core = build(config)
            try:
                self.assertIn("motion", core.devices)
                self.assertEqual(config.values["runtime"]["mode"], "simulated")
            finally:
                core.store.close()

    def test_test_model_is_explicitly_non_production(self):
        adapter = TestModelAdapter()
        runtime = InferenceRuntime({adapter.capability: adapter})
        runtime.start()
        response = runtime.predict(InferenceRequest(adapter.capability, "asset-1", "run-1"))
        self.assertEqual(response.status, "ok")
        self.assertTrue(response.result["testAdapter"])
        runtime.stop()


if __name__ == "__main__":
    unittest.main()
