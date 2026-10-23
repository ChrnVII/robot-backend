import asyncio
import json
import unittest

from app.robot_core.common import utc_now
from app.robot_core.contracts import DeviceCommand
from app.robot_core.devices.network_camera import NetworkCameraControlSettings, NetworkCameraDriver


class FakeResponse:
    status = 200

    def __init__(self, body=b'{"ok":true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class FakeOpener:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append((req, timeout))
        return FakeResponse()


def command(action, payload):
    now = utc_now()
    from datetime import datetime, timedelta
    issued = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return DeviceCommand(
        target="camera", action=action, payload=payload, issuer="test", lease_id="lease",
        issued_at=now, expires_at=(issued + timedelta(seconds=5)).isoformat(),
    )


class NetworkCameraDriverTest(unittest.TestCase):
    def test_hikvision_isapi_ptz_uses_put_xml_and_digest_profile(self):
        opener = FakeOpener()
        settings = NetworkCameraControlSettings.from_mapping({
            "protocol": "hikvision_isapi", "base_url": "http://camera",
            "username": "admin", "password": "secret",
        })
        driver = NetworkCameraDriver(settings, opener=opener)

        async def run():
            await driver.open()
            return await driver.execute(command("camera.ptz", {"direct": "left", "speed": 1}))

        result = asyncio.run(run())
        self.assertEqual(result.state, "succeeded")
        req, _ = opener.requests[0]
        self.assertEqual(req.full_url, "http://camera/ISAPI/PTZCtrl/channels/1/continuous")
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.get_header("Content-type"), "application/xml")
        self.assertIn(b'<PTZData version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">', req.data)
        self.assertIn(b"<pan>-100</pan>", req.data)

    def test_hikvision_state_endpoint_uses_get(self):
        opener = FakeOpener()
        settings = NetworkCameraControlSettings.from_mapping({
            "protocol": "hikvision_isapi", "base_url": "http://camera",
            "state_path": "/ISAPI/System/deviceInfo",
        })
        driver = NetworkCameraDriver(settings, opener=opener)

        async def run():
            await driver.open()
            return await driver.read_state()

        self.assertEqual(asyncio.run(run()).health.value, "ok")
        req, _ = opener.requests[0]
        self.assertEqual(req.full_url, "http://camera/ISAPI/System/deviceInfo")
        self.assertEqual(req.get_method(), "GET")

    def test_hikvision_preset_path_is_expanded(self):
        opener = FakeOpener()
        settings = NetworkCameraControlSettings.from_mapping({
            "protocol": "hikvision_isapi", "base_url": "http://camera",
        })
        driver = NetworkCameraDriver(settings, opener=opener)

        async def run():
            await driver.open()
            return await driver.execute(command("camera.goto_preset", {"preset": 7}))

        self.assertEqual(asyncio.run(run()).state, "succeeded")
        req, _ = opener.requests[0]
        self.assertEqual(req.full_url, "http://camera/ISAPI/PTZCtrl/channels/1/presets/7/goto")

    def test_hikvision_save_and_delete_preset(self):
        opener = FakeOpener()
        settings = NetworkCameraControlSettings.from_mapping({"protocol": "hikvision_isapi", "base_url": "http://camera"})
        driver = NetworkCameraDriver(settings, opener=opener)

        async def run():
            await driver.open()
            saved = await driver.execute(command("camera.save_preset", {"preset": 3, "name": "入口"}))
            deleted = await driver.execute(command("camera.delete_preset", {"preset": 3}))
            return saved, deleted

        saved, deleted = asyncio.run(run())
        self.assertEqual(saved.state, "succeeded")
        self.assertEqual(deleted.state, "succeeded")
        self.assertEqual(opener.requests[0][0].get_method(), "PUT")
        self.assertIn(b"<id>3</id>", opener.requests[0][0].data)
        self.assertEqual(opener.requests[1][0].get_method(), "DELETE")

    def test_ptz_uses_configured_endpoint_and_json(self):
        opener = FakeOpener()
        driver = NetworkCameraDriver(NetworkCameraControlSettings(
            base_url="http://camera", ptz_path="/ptz", method="POST"), opener=opener)

        async def run():
            await driver.open()
            return await driver.execute(command("camera.ptz", {"direct": "left", "speed": 0.5}))

        result = asyncio.run(run())
        self.assertEqual(result.state, "succeeded")
        req, timeout = opener.requests[0]
        self.assertEqual(req.full_url, "http://camera/ptz")
        self.assertEqual(json.loads(req.data), {"action": "ptz", "direct": "left", "speed": 0.5})
        self.assertEqual(timeout, 5.0)

    def test_missing_endpoint_is_rejected_without_fake_success(self):
        driver = NetworkCameraDriver(NetworkCameraControlSettings(base_url="http://camera"), opener=FakeOpener())

        async def run():
            await driver.open()
            return await driver.execute(command("camera.reboot", {}))

        result = asyncio.run(run())
        self.assertEqual(result.state, "rejected")
        self.assertIn("not configured", result.reject_reason)

    def test_query_format(self):
        opener = FakeOpener()
        driver = NetworkCameraDriver(NetworkCameraControlSettings(
            base_url="http://camera", ptz_path="/ptz", request_format="query"), opener=opener)

        async def run():
            await driver.open()
            return await driver.execute(command("camera.ptz", {"direct": "right", "speed": 1}))

        asyncio.run(run())
        req, _ = opener.requests[0]
        self.assertIn("direct=right", req.full_url)
        self.assertIsNone(req.data)


    def test_presets_are_read_from_camera_xml(self):
        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<PTZPresetList xmlns="http://www.isapi.org/ver20/XMLSchema" version="2.0">'
            b'<PTZPreset><enabled>true</enabled><id>1</id><presetName>Gate</presetName></PTZPreset>'
            b'<PTZPreset><enabled>false</enabled><id>7</id><presetName>\xe5\x85\xa5\xe5\x8f\xa3</presetName></PTZPreset>'
            b"</PTZPresetList>"
        )
        requests = []

        class XmlOpener:
            def __call__(self, req, timeout):
                requests.append(req)
                return FakeResponse(xml)

        settings = NetworkCameraControlSettings.from_mapping(
            {"protocol": "hikvision_isapi", "base_url": "http://camera"}
        )
        driver = NetworkCameraDriver(settings, opener=XmlOpener())

        async def run():
            await driver.open()
            return await driver.query("presets")

        result = asyncio.run(run())
        self.assertEqual([item["presetId"] for item in result["presets"]], [1, 7])
        self.assertEqual(result["presets"][1]["name"], "入口")
        self.assertFalse(result["presets"][1]["enabled"])
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertEqual(requests[0].full_url, "http://camera/ISAPI/PTZCtrl/channels/1/presets")


if __name__ == "__main__":
    unittest.main()
