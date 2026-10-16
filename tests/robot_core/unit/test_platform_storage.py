import io
import os
import tempfile
import unittest
from pathlib import Path

from app.robot_core.storage import MediaStore, PlatformStore
from app.robot_core.storage.database import SCHEMA_VERSION
from app.robot_core.sync import OutboxDispatcher
from app.robot_core.common import utc_now

TEST_DSN = os.environ["ROBOT_TEST_DATABASE_URL"]


class PlatformStorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = PlatformStore(TEST_DSN)
        self.addCleanup(self.store.close)

    def tearDown(self):
        self.temp.cleanup()

    def test_m1_schema_is_complete(self):
        expected = {
            "control_leases", "commands", "events", "media_assets",
            "inference_runs", "alerts", "reviews", "outbox", "schema_revisions",
            "device_states", "device_state_history",
            "inference_detections",
        }
        self.assertTrue(expected.issubset(self.store.table_names()))

    def test_schema_revision_is_gated_and_repeated_init_keeps_data(self):
        """版本已是最新时 initialize 走快路径：不重建表、不丢已有数据。"""
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO robots(robot_id, name, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("robot-1", "一号机", 1, utc_now(), utc_now()),
            )
            revision = connection.execute(
                "SELECT max(version) AS version FROM schema_revisions"
            ).fetchone()["version"]
        self.assertEqual(int(revision), SCHEMA_VERSION)

        second = PlatformStore(TEST_DSN)
        self.addCleanup(second.close)
        with second.connect() as connection:
            remaining = connection.execute("SELECT count(*) AS count FROM robots").fetchone()["count"]
        self.assertEqual(remaining, 1)

    def test_index_layout_drops_redundant_unique_and_covers_alert_queries(self):
        with self.store.connect() as connection:
            indexes = {
                row["indexname"] for row in connection.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
                ).fetchall()
            }
        # detection_id 已是主键，(request_id, detection_id) 组合唯一纯属冗余写入开销
        self.assertNotIn("inference_detections_request_id_detection_id_key", indexes)
        # alerts / reviews 此前完全没有索引
        self.assertIn("idx_alerts_state_time", indexes)
        self.assertIn("idx_alerts_trace", indexes)
        self.assertIn("idx_reviews_alert", indexes)

    def test_media_is_atomically_written_and_registered(self):
        media = MediaStore(self.root / "media", self.store, "robot-test")
        asset = media.save(
            io.BytesIO(b"sample-image"), task_run_id="run-1", waypoint_id="wp-1",
            device_id="camera", media_type="image", extension=".jpg", mime_type="image/jpeg",
        )
        path = Path(asset["uri"].replace("file:///", "")) if asset["uri"].startswith("file:///") else None
        self.assertEqual(asset["size_bytes"], 12)
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM media_assets WHERE asset_id=?", (asset["asset_id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertFalse(any(item.name.startswith(".") for item in (self.root / "media" / "image").iterdir()))

    def test_inference_result_persists_assets_boxes_and_is_idempotent(self):
        base = {"asset_id": "asset-original", "robot_id": "robot-test", "task_run_id": "run-infer",
                "waypoint_id": "cam01", "device_id": "cam01", "media_type": "image",
                "uri": "file:///tmp/original.jpg", "sha256": "a", "size_bytes": 10,
                "mime_type": "image/jpeg", "quality_state": "valid", "captured_at": utc_now(), "created_at": utc_now()}
        result = dict(base, asset_id="asset-result", uri="file:///tmp/result.jpg", sha256="b")
        inference = {"request_id": "run-infer:cam01:1", "trace_id": "trace-1", "camera_id": "cam01", "frame_id": 1,
                     "capability": "heat_stress_detection", "model_version": "1", "status": "ok",
                     "result": {"detections": [{"label": "hot", "confidence": .9, "bbox_xyxy": [1, 2, 3, 4]}]}}
        first = self.store.persist_inference_result(original_asset=base, result_asset=result, inference=inference, detections=inference["result"]["detections"])
        second = self.store.persist_inference_result(original_asset=base, result_asset=result, inference=inference, detections=inference["result"]["detections"])
        self.assertFalse(first["duplicate"]); self.assertTrue(second["duplicate"])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) AS count FROM media_assets").fetchone()["count"], 2)
            self.assertEqual(connection.execute("SELECT count(*) AS count FROM inference_runs").fetchone()["count"], 1)
            self.assertEqual(connection.execute("SELECT count(*) AS count FROM inference_detections").fetchone()["count"], 1)

    def test_outbox_enqueue_is_idempotent(self):
        first = self.store.enqueue_outbox("task", "run-1", "task.completed", {"ok": True}, "same-key")
        second = self.store.enqueue_outbox("task", "run-1", "task.completed", {"ok": True}, "same-key")
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.store.pending_outbox())), 1)

    def test_outbox_claim_ack_and_retry_state(self):
        outbox_id = self.store.enqueue_outbox("task", "run-1", "task.completed", {"ok": True}, "claim-key")
        claimed = self.store.claim_outbox()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["outbox_id"], outbox_id)
        self.assertEqual(claimed[0]["state"], "claimed")
        self.assertEqual(list(self.store.pending_outbox()), [])
        self.assertEqual(self.store.claim_outbox(), [])  # an active lease is not immediately duplicated

        state = self.store.mark_outbox_failure(
            outbox_id, claimed[0]["claim_token"], "temporary network failure", max_attempts=1, jitter_ratio=0
        )
        self.assertEqual(state, "dead_letter")
        self.assertTrue(self.store.retry_outbox(outbox_id))
        retry_claim = self.store.claim_outbox()
        self.assertEqual(len(retry_claim), 1)
        self.assertTrue(self.store.mark_outbox_sent(outbox_id, retry_claim[0]["claim_token"]))
        with self.store.connect() as connection:
            row = connection.execute("SELECT state, delivery_count FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["delivery_count"], 2)

    def test_inbox_idempotency_returns_original_message(self):
        first = self.store.record_inbox_message("remote-1", "command", {"action": "stop"}, "idem-1")
        second = self.store.record_inbox_message("remote-1", "command", {"action": "stop"}, "idem-1")
        self.assertEqual(first["inbox_id"], second["inbox_id"])
        self.assertTrue(self.store.complete_inbox(first["inbox_id"], {"state": "accepted"}))
        self.assertFalse(self.store.complete_inbox(first["inbox_id"], {"state": "accepted"}))

    def test_dispatcher_acknowledges_sender_and_requeues_failures(self):
        outbox_id = self.store.enqueue_outbox("task", "run-1", "task.completed", {"ok": True}, "dispatch-key")
        sent = []

        def sender(item):
            sent.append(item["outbox_id"])
            return {"acknowledged_at": "2026-09-18T00:00:00+00:00"}

        self.assertEqual(OutboxDispatcher(self.store, sender).drain_once(), 1)
        self.assertEqual(sent, [outbox_id])
        with self.store.connect() as connection:
            row = connection.execute("SELECT state, remote_ack_at FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["remote_ack_at"], "2026-09-18T00:00:00+00:00")

    def test_sensor_can_write_file_without_writing_business_database(self):
        media = MediaStore(self.root / "media", None, "robot-test")
        asset = media.save(
            io.BytesIO(b"audio"), task_run_id="run-1", waypoint_id="wp-1",
            device_id="audio", media_type="audio", extension=".wav", mime_type="audio/wav",
        )
        with self.store.connect() as connection:
            count = connection.execute("SELECT count(*) AS count FROM media_assets").fetchone()["count"]
        self.assertEqual(count, 0)
        self.store.register_asset(asset)
        with self.store.connect() as connection:
            count = connection.execute("SELECT count(*) AS count FROM media_assets").fetchone()["count"]
        self.assertEqual(count, 1)

    def test_unregistered_media_can_be_discarded(self):
        media = MediaStore(self.root / "media", None, "robot-test")
        asset = media.save(
            io.BytesIO(b"orphan"), task_run_id="run-1", waypoint_id="wp-1",
            device_id="camera", media_type="image", extension=".jpg", mime_type="image/jpeg",
        )
        media.discard(asset)
        self.assertEqual(list((self.root / "media" / "image").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
