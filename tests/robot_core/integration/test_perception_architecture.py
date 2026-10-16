"""Real PostgreSQL + simulated camera + deterministic model integration."""

import base64
import json
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, event

from app.robot_core.common.config import PlatformConfig
from app.robot_core.contracts import CaptureRequest, InferenceResponse
from app.robot_core.inference.runtime import TestModelAdapter as ModelStub
from app.robot_core.inspection.service import InspectionService
from app.robot_core.processes.inspection_worker import PerceptionWorker
from app.services.inspection import InferenceQueryService, _render_frames


class CountingModel(ModelStub):
    capability = "heat_stress_detection"

    def __init__(self):
        self.loads = self.unloads = 0

    def load(self):
        self.loads += 1
        super().load()

    def unload(self):
        self.unloads += 1
        super().unload()

    def predict(self, request):
        return InferenceResponse(request.request_id, self.capability, "ok", self.model_version,
            result={"detections": [{"label": "heat", "class_id": 0, "confidence": 0.9,
                                    "bbox_xyxy": [1, 1, 10, 10]}], "count_by_label": {"heat": 1}},
            model_id=self.model_id, result_schema=self.result_schema)


def test_capture_inspection_reuse_and_public_evidence(tmp_path, monkeypatch):
    adapter = CountingModel()
    monkeypatch.setattr("app.robot_core.processes.inspection_worker.build_configured_adapters",
                        lambda _config: {adapter.capability: adapter})
    config = PlatformConfig(tmp_path / "config.yaml", {
        "robot": {"robot_id": "robot-test"}, "runtime": {"mode": "simulated"},
        "storage": {"database_url": os.environ["ROBOT_TEST_DATABASE_URL"], "media_root": str(tmp_path / "media")},
        "devices": {"camera": {"transport": "v4l2", "device": "0", "width": 32, "height": 24}},
        "inspection": {"output_root": str(tmp_path / "inspection"), "capture_fps": 20},
    })
    worker = PerceptionWorker(config)
    engine = create_engine(os.environ["ROBOT_TEST_DATABASE_URL"])
    try:
        worker.inspection.recover()
        for run_id in ("first", "second"):
            worker.inspection.start({"task_run_id": run_id, "duration_seconds": 0.3})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = worker.inspection.status(run_id)
                if status["state"] in InspectionService.TERMINAL_STATES:
                    break
                time.sleep(0.02)
            assert status["state"] == "completed", status
            assert status["processed"] > 0
            # Both APIs share one reader and inspection keeps its model loaded.
            asset = worker.sensor.capture(CaptureRequest(run_id, "point", "camera", "image"))
            assert asset["mime_type"] == "image/jpeg"
            assert len(worker.cameras._readers) == 1
            internal = worker.inspection.snapshot(run_id)
            public = _render_frames(internal)
            assert "jpeg_path" not in public["frame"]
            assert base64.b64decode(public["frame"]["jpeg_base64"]).startswith(b"\xff\xd8")
            page = worker.inspection.detections(run_id)
            log = tmp_path / "inspection" / run_id / "inspection.jsonl"
            events = [InspectionService._public_event(json.loads(line)) for line in log.read_text().splitlines()]
            assert page["items"] == events[:50]  # exact old detection payload
            image = worker.inspection.resolve_image(run_id, page["items"][0]["original_image_id"])
            assert Path(image["path"]).is_file()
        assert adapter.loads == 1
        queries = []
        event.listen(engine, "before_cursor_execute", lambda _c, _u, sql, *_args: queries.append(sql))
        result = InferenceQueryService(engine).results("first", 0, 50)
        assert len(queries) == 3  # count, result page, all boxes
        assert result["total"] == worker.inspection.status("first")["processed"]
        assert result["items"][0]["detections"][0]["label"] == "heat"
    finally:
        worker.close()
        engine.dispose()
    assert adapter.unloads == 1


def test_failed_database_write_keeps_recoverable_checkpoint(tmp_path):
    class OfflineRuns:
        def status(self, _run_id):
            return None

        def save(self, _value):
            raise RuntimeError("database unavailable")

    config = PlatformConfig(tmp_path / "config.yaml", {
        "storage": {"media_root": str(tmp_path / "inspection")}})
    service = InspectionService(config, lambda *_args, **_kwargs: None, OfflineRuns())
    import pytest
    with pytest.raises(RuntimeError, match="database unavailable"):
        service.start({"task_run_id": "checkpoint"})
    checkpoint = tmp_path / "inspection" / "checkpoint" / "status.json"
    assert json.loads(checkpoint.read_text())["state"] == "starting"
    recovered = InspectionService(config, lambda *_args, **_kwargs: None)
    recovered.recover()
    assert recovered.status("checkpoint")["state"] == "failed"


def test_emergency_audit_replay_is_idempotent(tmp_path):
    import asyncio
    from app.robot_core.core import RobotCore
    from app.robot_core.devices import SimulatedDeviceDriver
    from app.robot_core.storage import PlatformStore

    async def scenario():
        with PlatformStore(os.environ["ROBOT_TEST_DATABASE_URL"]) as store:
            devices = {name: SimulatedDeviceDriver(name) for name in ("motion", "lift")}
            for device in devices.values():
                await device.open()
            core = RobotCore(store, devices, audit_directory=tmp_path)
            await core.emergency_stop("test", "audit-replay")
            payload = json.loads(next(tmp_path.glob("*.json")).read_text())
            store.persist_emergency(payload)
            store.persist_emergency(payload)
            with store.connect() as connection:
                assert connection.execute("SELECT count(*) AS n FROM commands").fetchone()["n"] == 2
                assert connection.execute("SELECT count(*) AS n FROM events WHERE trace_id='audit-replay'").fetchone()["n"] == 1
                assert connection.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1
    asyncio.run(scenario())
