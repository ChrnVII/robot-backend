import asyncio
from unittest.mock import Mock

import pytest

from app.contracts import InspectionRequestWire, Op, WireError, ipc_error_code, IpcErrorCode
from app.robot_core.processes.inspection_worker import handle_inspection_request

pytestmark = pytest.mark.no_database


@pytest.mark.parametrize("value", [
    {"operation": Op.TASK_STOP}, {"operation": Op.TASK_RESOLVE_IMAGE, "task_run_id": "run"},
    {"operation": Op.TASK_START, "duration_seconds": float("nan")},
    {"operation": Op.TASK_START, "duration_seconds": True},
    {"operation": Op.TASK_START, "mode": "custom"},
    {"operation": Op.TASK_START, "camera_ids": ["camera", "camera"]},
    {"operation": Op.TASK_START, "camera_ids": "camera"},
    {"operation": Op.TASK_START, "camera_ids": [{}]},
    {"operation": Op.TASK_LATEST_FRAME, "task_run_id": "../escape"},
    {"operation": Op.TASK_DETECTIONS, "task_run_id": "run", "limit": True},
    {"operation": Op.TASK_SNAPSHOT, "task_run_id": "run", "include_detections": "false"},
])
def test_bad_wire_requests_fail_before_service(value):
    service = Mock()
    with pytest.raises(WireError) as error:
        asyncio.run(handle_inspection_request(service, value))
    assert ipc_error_code(error.value) == IpcErrorCode.INVALID_REQUEST
    assert service.mock_calls == []


@pytest.mark.parametrize("value", [
    {"operation": Op.TASK_START}, {"operation": Op.TASK_STATUS},
    {"operation": Op.TASK_DETECTIONS, "task_run_id": "run", "offset": -1, "limit": 500},
    {"operation": Op.TASK_RESOLVE_IMAGE, "task_run_id": "run", "image_id": "camera:00000001_original.jpg"},
])
def test_valid_legacy_payload_preserved(value):
    assert InspectionRequestWire.from_mapping(value).to_payload() == value


def test_worker_defaults_preserved():
    service = Mock()
    asyncio.run(handle_inspection_request(service, {"operation": Op.TASK_SNAPSHOT, "task_run_id": "run"}))
    service.snapshot.assert_called_once_with("run", camera_id=None, include_detections=True)
    asyncio.run(handle_inspection_request(service, {"operation": Op.TASK_DETECTIONS, "task_run_id": "run"}))
    service.detections.assert_called_once_with("run", offset=0, limit=50)
