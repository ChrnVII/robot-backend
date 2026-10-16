"""Inspection-owned status, checkpoints and event queries."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from app.robot_core.common import utc_now
from app.robot_core.contracts import Event
from .database import PlatformStore


class InspectionRunStore:
    def __init__(self, store: PlatformStore):
        self.store = store

    def status(self, task_run_id: str) -> dict | None:
        with self.store.connect() as connection:
            row = connection.execute("SELECT payload FROM inspection_runs WHERE task_run_id=?", (task_run_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save(self, payload: Mapping[str, Any]) -> None:
        now = utc_now()
        with self.store.connect() as connection:
            previous = connection.execute(
                "SELECT state FROM inspection_runs WHERE task_run_id=?", (payload["task_run_id"],)
            ).fetchone()
            connection.execute(
                """INSERT INTO inspection_runs(task_run_id,state,payload,updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(task_run_id) DO UPDATE SET state=excluded.state,
                   payload=excluded.payload, updated_at=excluded.updated_at""",
                (payload["task_run_id"], payload["state"], self.store._json(payload), now),
            )
            if previous is None or previous["state"] != payload["state"]:
                event = Event("inspection.state_changed", "inspection-worker", str(payload.get("trace_id", payload["task_run_id"])), dict(payload))
                connection.execute(
                    "INSERT INTO events(event_id,event_type,source,trace_id,data,occurred_at,task_run_id) VALUES (?,?,?,?,?,?,?)",
                    (event.event_id, event.event_type, event.source, event.trace_id,
                     self.store._json(event.data), event.occurred_at, payload["task_run_id"]),
                )
                self.store._enqueue_event(connection, event)
            self.store._notify(connection, {"topic": "inspection", "task_run_id": payload["task_run_id"]})

    def recover(self) -> int:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM inspection_runs WHERE state NOT IN ('completed','stopped','failed')"
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            self.save({**payload, "state": "failed", "finished_at": time.time(),
                       "error": "inspection worker restarted before task completion"})
        return len(rows)

    def detections(self, task_run_id: str, offset: int, limit: int) -> dict:
        with self.store.connect() as connection:
            total = connection.execute(
                "SELECT count(*) AS count FROM v_inference_run_results WHERE task_run_id=?", (task_run_id,)
            ).fetchone()["count"]
            rows = connection.execute(
                """SELECT * FROM v_inference_run_results WHERE task_run_id=?
                   ORDER BY finished_at,run_id LIMIT ? OFFSET ?""", (task_run_id, limit, offset)
            ).fetchall()
        items = []
        for row in rows:
            result = json.loads(row["result"])
            camera_id = row["camera_id"] or "camera"
            frame_id = row["frame_id"] or 0
            item = {
                "event_type": "detection" if result.get("detections") else "inference",
                "task_run_id": task_run_id, "camera_id": camera_id, "frame_id": frame_id,
                "captured_at": row["captured_at"], "detections": result.get("detections", []),
                "count_by_label": result.get("count_by_label", {}), "latency_ms": result.get("latency_ms"),
                "status": row["status"], "trace_id": row["trace_id"], "model_id": result.get("model_id"),
                "model_version": row["model_version"], "model_sha256": row["model_sha256"],
                "result_schema": result.get("result_schema"), "detection_id": f"{camera_id}-{frame_id:08d}",
            }
            for key, uri in (("original_image_id", row["uri"]), ("result_image_id", row["result_uri"])):
                if uri:
                    item[key] = f"{camera_id}:{Path(urlparse(uri).path).name}"
            items.append(item)
        return {"items": items, "offset": offset, "limit": limit, "total": int(total)}
