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


def test_failed_database_write_keeps_recoverable_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("app.robot_core.inspection.service.data_root", lambda _: tmp_path)
    monkeypatch.setattr("app.robot_core.inspection.paths.data_root", lambda _: tmp_path)
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
    checkpoint = tmp_path / "inspection-state" / "checkpoint" / "status.json"
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
