import json
import unittest
from unittest.mock import patch

from app.robot_core.sync.transport import HttpsOutboxSender


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class HttpsTransportTest(unittest.TestCase):
    def test_sender_posts_versioned_idempotent_event(self):
        sender = HttpsOutboxSender("https://cloud.example/events", "robot-1")
        item = {
            "outbox_id": "outbox-1",
            "event_type": "task.completed",
            "aggregate_type": "task",
            "aggregate_id": "run-1",
            "idempotency_key": "idem-1",
            "created_at": "2026-09-18T00:00:00+00:00",
            "payload": {"ok": True},
        }
        with patch("app.robot_core.sync.transport.urlopen", return_value=_Response({"acknowledgedAt": "now"})) as opened:
            result = sender(item)
        self.assertEqual(result, {"acknowledged_at": "now"})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Idempotency-key"), "idem-1")
        self.assertEqual(json.loads(request.data)["contractVersion"], "robot.event.v1")

    def test_sender_requires_https_and_matching_client_credentials(self):
        with self.assertRaises(ValueError):
            HttpsOutboxSender("http://cloud.example/events", "robot-1")
        with self.assertRaises(ValueError):
            HttpsOutboxSender("https://cloud.example/events", "robot-1", certificate_file="cert.pem")


if __name__ == "__main__":
    unittest.main()
