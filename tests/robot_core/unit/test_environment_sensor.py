import asyncio
import json
import os
import unittest

from app.robot_core.contracts import DeviceState, HealthState
from app.robot_core.devices import LegacyEnvironmentSensorDriver
from app.robot_core.storage import PlatformStore

TEST_DSN = os.environ["ROBOT_TEST_DATABASE_URL"]


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class EnvironmentSensorTest(unittest.TestCase):
    def test_legacy_nested_response_is_normalized(self):
        requested_urls = []

        def opener(req, timeout):
            requested_urls.append(req.full_url)
            if req.full_url.endswith("/sensorData/sensorOpen"):
                return FakeResponse({"code": "200", "data": "串口打开成功"})
            return FakeResponse({
                "code": "200",
                "data": json.dumps({"temperature": "24.8", "humidity": "68.5", "ammonia": "12.3", "illuminance": "320"}),
            })

        driver = LegacyEnvironmentSensorDriver(opener=opener)
        state = asyncio.run(self._read(driver))
        self.assertEqual(state.health, HealthState.OK)
        self.assertEqual(state.values["temperature_c"], 24.8)
        self.assertEqual(state.values["illuminance_lux"], 320.0)
        self.assertEqual(requested_urls, [
            "http://127.0.0.1:9010/sensorData/sensorOpen",
            "http://127.0.0.1:9010/sensorData/detectLtwy",
        ])

    async def _read(self, driver):
        await driver.open()
        return await driver.read_state()

    def test_state_is_upserted_and_queryable(self):
        store = PlatformStore(TEST_DSN)
        self.addCleanup(store.close)
        driver = LegacyEnvironmentSensorDriver(opener=lambda req, timeout: FakeResponse({
            "code": 200,
            "data": json.dumps({"temperature": 24, "humidity": 60, "ammonia": 1, "illuminance": 100}),
        }))
        state = asyncio.run(self._read(driver))
        store.upsert_device_state(state)
        row = store.device_state("environment")
        self.assertEqual(row["values"]["humidity_percent"], 60.0)
        self.assertEqual(row["source"], "http://127.0.0.1:9010")
        history = store.device_state_history("environment")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["health"], "ok")
        self.assertEqual(history[0]["values"]["temperature_c"], 24.0)
        self.assertLessEqual(history[0]["sampled_at"], history[0]["received_at"])

    def test_each_state_update_keeps_history_and_latest_snapshot(self):
        store = PlatformStore(TEST_DSN)
        self.addCleanup(store.close)
        first = DeviceState("motion", HealthState.OK, "2026-01-01T00:00:00+00:00", "test", {"position_m": 1.0})
        second = DeviceState("motion", HealthState.OK, "2026-01-01T00:00:01+00:00", "test", {"position_m": 2.0})
        store.upsert_device_state(first)
        store.upsert_device_state(second)
        self.assertEqual(store.device_state("motion")["values"]["position_m"], 2.0)
        self.assertEqual(len(store.device_state_history("motion")), 2)


if __name__ == "__main__":
    unittest.main()
