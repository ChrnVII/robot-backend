"""检测分页联调：robot-core 持久化的推理记录 → 后端 InferenceQueryService 读取。

用真实 PostgreSQL 测试库走「robot_core.storage.PlatformStore 写入 → 后端只读查询」
契约，验证 camelCase 载荷、证据图 imageId 推导与空库降级。
"""

import os

from app.robot_core.storage import PlatformStore
from app.services.inspection import InferenceQueryService

TEST_DSN = os.environ["ROBOT_TEST_DATABASE_URL"]
_NOW = "2026-10-15T06:00:00.000000+00:00"


def test_results_reads_persisted_entries():
    store = PlatformStore(TEST_DSN)
    try:
        original = {
            "asset_id": "a1", "robot_id": "robot-dev-001", "task_run_id": "run-1",
            "waypoint_id": "w1", "device_id": "cam01", "media_type": "image",
            "uri": "file:///data/inspection/run-1__cam01_00000001_original.jpg",
            "sha256": "s1", "size_bytes": 10, "mime_type": "image/jpeg",
            "quality_state": "valid", "captured_at": _NOW, "created_at": _NOW, "cage_id": None,
        }
        result_asset = {**original, "asset_id": "a2", "uri": "file:///data/inspection/run-1__cam01_00000001_result.jpg"}
        inference = {
            "request_id": "run-1:cam01:1", "trace_id": "tr-1", "capability": "heat_stress",
            "model_version": "v1", "status": "ok",
            "result": {"count_by_label": {"heat": 1}, "latency_ms": 12.5},
            "started_at": _NOW, "finished_at": _NOW,
        }
        detections = [{"bbox_xyxy": [1.0, 2.0, 3.0, 4.0], "label": "heat", "class_id": 2, "confidence": 0.91}]
        outcome = store.persist_inference_result(
            original_asset=original, result_asset=result_asset, inference=inference, detections=detections,
        )
        assert outcome["duplicate"] is False
        assert outcome["detection_count"] == 1
    finally:
        store.close()

    service = InferenceQueryService(TEST_DSN)
    try:
        page = service.results("run-1", 0, 50)
        assert page["total"] == 1
        item = page["items"][0]
        assert item["cameraId"] == "cam01"
        assert item["frameId"] == 1
        assert item["countByLabel"] == {"heat": 1}
        assert item["latencyMs"] == 12.5
        assert item["originalImageId"] == "cam01:run-1__cam01_00000001_original.jpg"
        assert item["resultImageId"] == "cam01:run-1__cam01_00000001_result.jpg"
        assert item["resultAssetId"] == "a2"
        assert item["detections"][0]["label"] == "heat"
        assert item["detections"][0]["confidence"] == 0.91

        assert service.results("run-missing", 0, 50) == {"items": [], "offset": 0, "limit": 50, "total": 0}
    finally:
        service.dispose()


def test_results_degrade_to_empty_page_without_database():
    # 本用例开始前测试库已重建为空 schema（无表结构）：按契约降级为空页
    service = InferenceQueryService(TEST_DSN)
    try:
        page = service.results("run-1", 0, 50)
        assert page["total"] == 0 and page["items"] == []
    finally:
        service.dispose()
