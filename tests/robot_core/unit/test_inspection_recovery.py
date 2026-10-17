import json
import os

from app.robot_core.common.config import PlatformConfig
from app.robot_core.inspection.service import InspectionService
from app.robot_core.storage import PlatformStore
from app.robot_core.storage.runs import InspectionRunStore


def test_restart_recovers_database_and_file_states(tmp_path, monkeypatch):
    monkeypatch.setattr("app.robot_core.inspection.service.data_root", lambda _: tmp_path)
    config = PlatformConfig(tmp_path / "config.yaml", {
        "storage": {"media_root": str(tmp_path / "media")},
        "inspection": {"output_root": str(tmp_path / "inspection")},
    })
    store = PlatformStore(os.environ["ROBOT_TEST_DATABASE_URL"])
    try:
        runs = InspectionRunStore(store)
        runs.save({"task_run_id": "run-interrupted", "state": "running", "processed": 7})
        old = tmp_path / "inspection-state" / "run-legacy"
        old.mkdir(parents=True)
        (old / "status.json").write_text(json.dumps({"task_run_id": "run-legacy", "state": "starting"}))
        service = InspectionService(config, lambda *_args, **_kwargs: None, runs)
        service.recover()
        assert service.status("run-interrupted")["state"] == "failed"
        assert service.status("run-interrupted")["processed"] == 7
        assert service.status("run-legacy")["state"] == "failed"
        assert runs.status("run-legacy")["state"] == "failed"
        service.recover()
        assert runs.status("run-legacy")["state"] == "failed"
    finally:
        store.close()
