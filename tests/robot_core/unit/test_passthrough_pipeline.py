import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.robot_core.common.config import PlatformConfig
from app.robot_core.processes import inspection_engine


class _Reader:
    def __init__(self):
        self.closed = False

    def __call__(self):
        return object()

    def close(self):
        self.closed = True


class PassthroughPipelineTest(unittest.TestCase):
    def test_passthrough_sends_raw_frames_without_constructing_inference_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = PlatformConfig(
                source=root / "config.yaml",
                values={
                    "devices": {"camera": {"transport": "v4l2", "device": "0"}},
                    "inspection": {
                        "output_root": str(root / "inspection"),
                        "capture_fps": 30,
                        "batch_size": 1,
                        "queue_size": 1,
                    },
                    "storage": {"media_root": str(root / "media")},
                    "models": {},
                },
            )
            reader = _Reader()
            frames = []
            with patch.object(inspection_engine, "build_camera_reader", return_value=reader), \
                    patch.object(inspection_engine, "_encode_jpeg_base64", return_value="raw-jpeg"), \
                    patch.object(inspection_engine, "InferenceRuntime", side_effect=AssertionError("inference must be bypassed")):
                summary = inspection_engine.run(
                    config,
                    mode="single",
                    pipeline="passthrough",
                    task_run_id="stream-1",
                    duration_seconds=0.05,
                    headless=True,
                    frame_callback=frames.append,
                )

            self.assertEqual("passthrough", summary["pipeline"])
            self.assertGreater(summary["processed"], 0)
            self.assertTrue(frames)
            self.assertTrue(all(frame["pipeline"] == "passthrough" for frame in frames))
            self.assertTrue(all(frame["jpeg_base64"] == "raw-jpeg" for frame in frames))
            self.assertFalse((root / "inspection" / "stream-1" / "inspection.jsonl").exists())
            self.assertTrue(reader.closed)

    def test_no_camera_frames_fail_after_startup_timeout_and_redact_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = PlatformConfig(
                source=root / "config.yaml",
                values={
                    "devices": {"cameras": {"items": [{"id": "cam01", "enabled": True,
                        "transport": "rtsp", "host": "192.168.137.64", "port": 554,
                        "username": "operator", "password": "private", "stream": 101}]}},
                    "inspection": {"output_root": str(root / "inspection"), "capture_fps": 30,
                        "batch_size": 1, "queue_size": 1, "camera_startup_timeout_seconds": 0.1},
                    "storage": {"media_root": str(root / "media")},
                    "models": {},
                },
            )

            def fail_to_read():
                raise RuntimeError("stream unavailable: rtsp://operator:private@192.168.137.64:554/Streaming/Channels/101")

            with patch.object(inspection_engine, "build_camera_reader", return_value=fail_to_read):
                summary = inspection_engine.run(
                    config, mode="single", pipeline="passthrough", task_run_id="stream-fail",
                    duration_seconds=0, headless=True,
                )

        self.assertEqual("failed", summary["state"])
        self.assertEqual(0, summary["processed"])
        self.assertIn("192.168.137.64", summary["camera_errors"]["cam01"])
        self.assertNotIn("private", str(summary))


if __name__ == "__main__":
    unittest.main()
