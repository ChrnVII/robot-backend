import unittest

from app.robot_core.contracts import CaptureRequest
from app.robot_core.sensing.network_camera import HttpJpegCameraProvider, NetworkCameraSettings


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.closed = False

    def read(self):
        return self.content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class FakeOpener:
    def __init__(self):
        self.request = None
        self.timeout = None

    def __call__(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse(b"\xff\xd8jpeg\xff\xd9")


class NetworkCameraProviderTest(unittest.TestCase):
    def test_http_provider_fetches_robot_jpeg(self):
        opener = FakeOpener()
        provider = HttpJpegCameraProvider(
            NetworkCameraSettings(host="192.168.136.11", width=1280, height=720),
            opener=opener,
        )
        request = CaptureRequest("run-1", "point-1", "camera", "image")

        content = provider.capture(request)

        self.assertEqual(content, b"\xff\xd8jpeg\xff\xd9")
        self.assertIn("http://192.168.136.11:8080/API/picture?", opener.request.full_url)
        self.assertIn("width=1280", opener.request.full_url)
        self.assertIn("height=720", opener.request.full_url)
        self.assertEqual(opener.timeout, 5.0)

    def test_request_parameters_override_network_camera(self):
        opener = FakeOpener()
        provider = HttpJpegCameraProvider(opener=opener)
        request = CaptureRequest(
            "run-1", "point-1", "camera", "image",
            parameters={"host": "192.168.136.24", "port": 8090, "path": "/snapshot", "width": 640},
        )

        provider.capture(request)

        self.assertTrue(opener.request.full_url.startswith("http://192.168.136.24:8090/snapshot?"))
        self.assertIn("width=640", opener.request.full_url)

    def test_hikvision_snapshot_uses_isapi_query_and_no_basic_header(self):
        opener = FakeOpener()
        provider = HttpJpegCameraProvider(
            NetworkCameraSettings(
                host="192.168.137.64", port=80,
                path="/ISAPI/Streaming/channels/101/picture",
                username="admin", password="secret", auth_scheme="digest",
            ), opener=opener,
        )

        provider.capture(CaptureRequest("run-1", "point-1", "camera", "image"))

        self.assertIn("videoResolutionWidth=1280", opener.request.full_url)
        self.assertIn("videoResolutionHeight=720", opener.request.full_url)
        self.assertIsNone(opener.request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
