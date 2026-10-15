from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.robot_core.common.config import PlatformConfig
from app.robot_core.inspection.service import InspectionService


class InspectionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = PlatformConfig(
            source=self.root / "config.yaml",
            values={
                "storage": {"media_root": str(self.root / "media")},
                "inspection": {"output_root": str(self.root / "inspections")},
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_completed_task_is_persisted_and_service_returns_to_idle(self) -> None:
        def run_function(_config, **kwargs):
            summary = {
                "state": "completed",
                "processed": 4,
                "positive": 1,
                "task_run_id": kwargs["task_run_id"],
            }
            task_root = self.root / "inspections" / kwargs["task_run_id"]
            (task_root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            kwargs["status_callback"](summary)
            return summary

        service = InspectionService(self.config, run_function)
        service.start({"task_run_id": "run-1", "mode": "four"})
        self._wait_until(lambda: service.health()["active"]["state"] == "idle")

        self.assertEqual("completed", service.status("run-1")["state"])
        self.assertEqual(4, service.status("run-1")["processed"])

    def test_stop_signals_running_task(self) -> None:
        entered = threading.Event()

        def run_function(_config, **kwargs):
            entered.set()
            kwargs["stop_event"].wait(2)
            summary = {"state": "stopped", "processed": 0, "positive": 0}
            kwargs["status_callback"](summary)
            return summary

        service = InspectionService(self.config, run_function)
        service.start({"task_run_id": "run-stop"})
        self.assertTrue(entered.wait(1))
        self.assertEqual("stopping", service.stop("run-stop")["state"])
        self._wait_until(lambda: service.health()["active"]["state"] == "idle")
        self.assertEqual("stopped", service.status("run-stop")["state"])

    def test_latest_frames_returns_each_camera_frame_for_task(self) -> None:
        service = InspectionService(self.config, lambda *_args, **_kwargs: None)
        with service._lock:
            service._update_latest_frame("run-frames", {
                "camera_id": "cam01", "frame_id": 1, "jpeg_base64": "one",
            })
            service._update_latest_frame("run-frames", {
                "camera_id": "cam05", "frame_id": 2, "jpeg_base64": "five",
            })

        result = service.latest_frames("run-frames")

        self.assertEqual({"cam01", "cam05"}, set(result["frames"]))
        self.assertEqual("five", result["frames"]["cam05"]["jpeg_base64"])

    def test_detection_paging_and_image_resolution(self) -> None:
        service = InspectionService(self.config, lambda *_args, **_kwargs: None)
        task_root = service.output_root / "run-log"
        camera_root = task_root / "cam01"
        camera_root.mkdir(parents=True)
        image = camera_root / "00000002_result.jpg"
        image.write_bytes(b"jpeg")
        events = [
                {"event_type": "inference", "camera_id": "cam01", "frame_id": 1,
                 "detections": [], "original_image": str(camera_root / "00000001_original.jpg")},
            {
                "event_type": "detection",
                "camera_id": "cam01",
                "frame_id": 2,
                "detections": [{"label": "heat_stress", "confidence": 0.9}],
                "result_image": str(image),
            },
            {"event_type": "detection", "camera_id": "cam02", "frame_id": 3, "detections": []},
        ]
        (task_root / "inspection.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        page = service.detections("run-log", offset=0, limit=1)
        self.assertEqual(3, page["total"])
        self.assertEqual("cam01-00000001", page["items"][0]["detection_id"])
        self.assertEqual("cam01:00000001_original.jpg", page["items"][0]["original_image_id"])
        self.assertEqual(str(image.resolve()), service.resolve_image(
            "run-log", "cam01:00000002_result.jpg"
        )["path"])
        all_results = service.detections("run-log", offset=0, limit=10)
        self.assertEqual(3, all_results["total"])
        self.assertEqual([], all_results["items"][0]["detections"])

        for invalid in ("../cam01:file.jpg", "cam01:../file.jpg", "cam01:file.png"):
            with self.assertRaises((ValueError, FileNotFoundError)):
                service.resolve_image("run-log", invalid)

    def test_failed_task_records_error(self) -> None:
        def fail(_config, **_kwargs):
            raise RuntimeError("model unavailable")

        service = InspectionService(self.config, fail)
        service.start({"task_run_id": "run-fail"})
        self._wait_until(lambda: service.health()["active"]["state"] == "idle")
        self.assertEqual("failed", service.status("run-fail")["state"])
        self.assertEqual("model unavailable", service.status("run-fail")["error"])

    def test_latest_frame_is_exposed_to_realtime_clients(self) -> None:
        def run_function(_config, **kwargs):
            kwargs["frame_callback"]({
                "camera_id": "cam01", "frame_id": 7, "jpeg_base64": "abc"
            })
            kwargs["status_callback"]({"state": "completed"})
            return {"state": "completed"}

        service = InspectionService(self.config, run_function)
        service.start({"task_run_id": "run-frame"})
        self._wait_until(lambda: service.health()["active"]["state"] == "idle")
        frame = service.latest_frame("run-frame")
        self.assertTrue(frame["available"])
        self.assertEqual("abc", frame["jpeg_base64"])

    def test_passthrough_pipeline_is_forwarded_without_changing_task_lifecycle(self) -> None:
        received = {}

        def run_function(_config, **kwargs):
            received.update(kwargs)
            kwargs["frame_callback"]({
                "camera_id": "cam01", "frame_id": 1, "pipeline": "passthrough",
                "jpeg_base64": "raw-jpeg",
            })
            kwargs["status_callback"]({"state": "completed", "pipeline": "passthrough"})
            return {"state": "completed", "pipeline": "passthrough", "processed": 1, "positive": 0}

        service = InspectionService(self.config, run_function)
        started = service.start({"task_run_id": "run-passthrough", "pipeline": "passthrough"})
        self.assertEqual("passthrough", started["pipeline"])
        self._wait_until(lambda: service.health()["active"]["state"] == "idle")
        self.assertEqual("passthrough", received["pipeline"])
        self.assertEqual("raw-jpeg", service.latest_frame("run-passthrough")["jpeg_base64"])

    def test_custom_camera_selection_accepts_any_subset_of_eight(self) -> None:
        received = {}

        def run_function(_config, **kwargs):
            received.update(kwargs)
            kwargs["status_callback"]({"state": "completed"})
            return {"state": "completed"}

        service = InspectionService(self.config, run_function)
        started = service.start({
            "task_run_id": "run-subset",
            "mode": "custom",
            "camera_ids": ["cam02", "cam05", "cam08"],
        })
        self.assertEqual(["cam02", "cam05", "cam08"], started["camera_ids"])
        self.assertEqual(3, started["camera_count"])
        self._wait_until(lambda: service.health()["active"]["state"] == "idle")
        self.assertEqual(("cam02", "cam05", "cam08"), received["camera_ids"])

    def test_custom_camera_selection_rejects_duplicates(self) -> None:
        service = InspectionService(self.config, lambda *_args, **_kwargs: None)
        with self.assertRaises(ValueError):
            service.start({"mode": "custom", "camera_ids": ["cam01", "cam01"]})

    def _wait_until(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not reached before timeout")


if __name__ == "__main__":
    unittest.main()
