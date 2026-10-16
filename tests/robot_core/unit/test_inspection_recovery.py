import json
import os

from app.robot_core.common.config import PlatformConfig
from app.robot_core.inspection.service import InspectionService
from app.robot_core.storage import PlatformStore
from app.robot_core.storage.runs import InspectionRunStore


def test_restart_recovers_database_and_file_states(tmp_path):
    config = PlatformConfig(tmp_path / "config.yaml", {
        "storage": {"media_root": str(tmp_path / "media")},
        "inspection": {"output_root": str(tmp_path / "inspection")},
    })
    store = PlatformStore(os.environ["ROBOT_TEST_DATABASE_URL"])
    try:
        runs = InspectionRunStore(store)
        runs.save({"task_run_id": "run-interrupted", "state": "running", "processed": 7})
        old = tmp_path / "inspection" / "run-legacy"
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


def test_partial_legacy_jsonl_is_only_read_when_complete(tmp_path):
    from app.robot_core.inspection.log_index import DetectionLogIndex
    path = tmp_path / "inspection.jsonl"
    first = {"event_type": "inference", "frame_id": 1}
    second = {"event_type": "detection", "frame_id": 2}
    path.write_text(json.dumps(first) + "\n" + json.dumps(second), encoding="utf-8")
    index = DetectionLogIndex()
    assert index.page(path, 0, 10) == ([first], 1)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert index.page(path, 1, 10) == ([second], 2)
