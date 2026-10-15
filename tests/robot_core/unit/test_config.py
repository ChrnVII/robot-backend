import os
import unittest
from pathlib import Path

from app.robot_core.common.config import ConfigurationError, _expand_environment, load_config, redact_database_url


class ConfigurationTest(unittest.TestCase):
    def test_environment_values_expand_and_missing_secrets_fail(self):
        previous = os.environ.get("ROBOT_TEST_SECRET")
        os.environ["ROBOT_TEST_SECRET"] = "value-from-environment"
        try:
            self.assertEqual(_expand_environment("prefix-${ROBOT_TEST_SECRET}"), "prefix-value-from-environment")
            self.assertEqual(_expand_environment("${ROBOT_UNKNOWN:-fallback}"), "fallback")
            with self.assertRaises(ConfigurationError):
                _expand_environment("${ROBOT_REQUIRED_SECRET}")
        finally:
            if previous is None:
                os.environ.pop("ROBOT_TEST_SECRET", None)
            else:
                os.environ["ROBOT_TEST_SECRET"] = previous

    def test_database_url_is_redacted(self):
        self.assertEqual(
            redact_database_url("postgresql+psycopg://robot:secret@localhost:5432/robot_platform"),
            "postgresql+psycopg://robot:***@localhost:5432/robot_platform",
        )

    def test_base_config_uses_safe_dev_default_and_pool_options(self):
        config = load_config(Path(__file__).parents[3] / "configs" / "base.yaml")
        self.assertEqual(config.database_pool_options["max_size"], 8)
        self.assertIn("robot:***@", config.database_display_url)


if __name__ == "__main__":
    unittest.main()
