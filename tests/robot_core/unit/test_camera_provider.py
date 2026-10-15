import unittest

from app.robot_core.contracts import CaptureRequest
from app.robot_core.sensing.camera import CameraSettings, OpenCVCameraProvider


class FakeCapture:
    def __init__(self, frame):
        self.frame = frame
        self.released = False
        self.settings = []

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.settings.append((prop, value))
        return True

    def read(self):
        return True, self.frame

    def release(self):
        self.released = True


class FakeCv2:
    CAP_V4L2 = 200
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    IMWRITE_JPEG_QUALITY = 1

    def __init__(self):
        self.capture = FakeCapture(object())
        self.calls = []

    def VideoCapture(self, source, backend):
        self.calls.append((source, backend))
        return self.capture

    def imencode(self, extension, frame, params):
        return True, bytearray(b"jpeg-bytes")


class CameraProviderTest(unittest.TestCase):
    def test_capture_encodes_and_releases_camera(self):
        cv2 = FakeCv2()
        provider = OpenCVCameraProvider(CameraSettings(device="0", warmup_frames=2), cv2_module=cv2)
        request = CaptureRequest("run-1", "point-1", "camera", "image")

        self.assertEqual(provider.capture(request), b"jpeg-bytes")
        self.assertEqual(cv2.calls, [(0, cv2.CAP_V4L2)])
        self.assertTrue(cv2.capture.released)
        self.assertEqual(len(cv2.capture.settings), 2)

    def test_request_parameters_override_defaults(self):
        cv2 = FakeCv2()
        provider = OpenCVCameraProvider(CameraSettings(device="/dev/video0"), cv2_module=cv2)
        request = CaptureRequest("run-1", "point-1", "camera", "image", parameters={"device": "/dev/video2", "width": 640})

        provider.capture(request)
        self.assertEqual(cv2.calls[0][0], "/dev/video2")
        self.assertIn((cv2.CAP_PROP_FRAME_WIDTH, 640), cv2.capture.settings)


if __name__ == "__main__":
    unittest.main()
