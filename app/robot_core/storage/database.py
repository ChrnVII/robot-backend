from __future__ import annotations

import json
import random
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Union

from app.robot_core.common import new_id, parse_utc, utc_now
from app.robot_core.contracts import CommandResult, ControlLease, DeviceCommand, DeviceState, Event
from .db import PostgresConnectionPool, is_integrity_error, is_postgres_target


class PlatformStore:
    """Robot business store backed by PostgreSQL."""

    def __init__(self, database_url: Union[str, Path], *, pool_options: Optional[Mapping[str, Any]] = None):
        if not is_postgres_target(database_url):
            raise ValueError(
                "PlatformStore requires a PostgreSQL DSN, e.g. "
                "postgresql+psycopg://robot:***@127.0.0.1:5432/robot_platform"
            )
        self.database_url = str(database_url)
        self._lock = threading.RLock()
        self._postgres_pool = PostgresConnectionPool(self.database_url, **dict(pool_options or {}))
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._postgres_pool is not None:
            self._postgres_pool.close()
            self._postgres_pool = None

    def __enter__(self) -> "PlatformStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def connect(self):
        if self._postgres_pool is None:  # pragma: no cover - defensive guard
            raise RuntimeError("PostgreSQL connection pool is not initialized")
        return self._postgres_pool.connection()

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._lock, self.connect() as connection:
            # Serialize first-time initialization across robot-core and
            # sync-agent processes on the same PostgreSQL instance.
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('app.robot_core_schema_v1'))")
            connection.executescript(schema)
            self._upgrade_existing_tables(connection)
            connection.execute(
                "INSERT INTO schema_revisions(version, applied_at) VALUES (5, ?) ON CONFLICT DO NOTHING",
                (utc_now(),),
            )

    @staticmethod
    def _upgrade_existing_tables(connection) -> None:
        """为既有数据库补齐完整目标结构所需的新增字段。"""
        additions = {
            "commands": {"task_run_id": "TEXT"},
            "events": {"task_run_id": "TEXT", "aggregate_type": "TEXT", "aggregate_id": "TEXT"},
            "inference_runs": {"model_sha256": "TEXT"},
            "media_assets": {"cage_id": "TEXT"},
            "outbox": {
                "claim_token": "TEXT",
                "claimed_at": "TEXT",
                "last_attempt_at": "TEXT",
                "remote_ack_at": "TEXT",
                "delivery_count": "INTEGER NOT NULL DEFAULT 0",
            },
            "inbox": {"idempotency_key": "TEXT"},
        }
        for table, columns in additions.items():
            existing = {
                row["column_name"] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ?", (table,)
                ).fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_task_time ON events(task_run_id, occurred_at)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_aggregate_time "
            "ON events(aggregate_type, aggregate_id, occurred_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_outbox_claims "
            "ON outbox(state, next_retry_at, claimed_at, created_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_idempotency "
            "ON inbox(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )

    @staticmethod
    def _json(value: Mapping[str, Any]) -> str:
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)

    def save_lease(self, lease: ControlLease) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO control_leases(lease_id, holder, mode, priority, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (lease.lease_id, lease.holder, lease.mode, lease.priority, lease.issued_at, lease.expires_at),
            )

    def release_lease(self, lease_id: str, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE control_leases SET released_at=?, release_reason=? WHERE lease_id=? AND released_at IS NULL",
                (utc_now(), reason, lease_id),
            )

    def save_command(self, command: DeviceCommand, state: str = "accepted", reject_reason: Optional[str] = None) -> bool:
        with self.connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO commands(command_id,idempotency_key,trace_id,issuer,lease_id,target,action,payload,state,issued_at,expires_at,reject_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (command.command_id, command.idempotency_key, command.trace_id, command.issuer, command.lease_id,
                     command.target, command.action, self._json(command.payload), state, command.issued_at,
                     command.expires_at, reject_reason),
                )
                return True
            except Exception as error:
                if not is_integrity_error(error):
                    raise
                existing = connection.execute(
                    "SELECT command_id FROM commands WHERE idempotency_key=?", (command.idempotency_key,)
                ).fetchone()
                if existing:
                    return False
                raise error

    def finish_command(self, result: CommandResult) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE commands SET state=?, completed_at=?, result=?, reject_reason=? WHERE command_id=?",
                (result.state, result.completed_at, self._json(result.details), result.reject_reason, result.command_id),
            )

    def command_by_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM commands WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["result"] = json.loads(item["result"]) if item["result"] else {}
        return item

    def append_event(self, event: Event) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(event_id,event_type,source,trace_id,data,occurred_at) VALUES (?,?,?,?,?,?)",
                (event.event_id, event.event_type, event.source, event.trace_id, self._json(event.data), event.occurred_at),
            )

    def upsert_device_state(self, state: DeviceState) -> None:
        """Record a state sample and update the latest snapshot atomically."""
        received_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO device_state_history
                   (sample_id, device_id, health, quality, values_json, raw_fault_code,
                    sampled_at, received_at, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id("state"), state.device_id, state.health.value, state.quality,
                 self._json(state.values), state.raw_fault_code, state.sampled_at,
                 received_at, state.source),
            )
            connection.execute(
                """INSERT INTO device_states
                   (device_id, health, quality, values_json, raw_fault_code,
                    sampled_at, received_at, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                    health=excluded.health, quality=excluded.quality,
                    values_json=excluded.values_json,
                    raw_fault_code=excluded.raw_fault_code,
                    sampled_at=excluded.sampled_at,
                    received_at=excluded.received_at, source=excluded.source""",
                (state.device_id, state.health.value, state.quality, self._json(state.values),
                 state.raw_fault_code, state.sampled_at, received_at, state.source),
            )

    def device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_states WHERE device_id=?", (device_id,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["values"] = json.loads(value.pop("values_json"))
        return value

    def device_state_history(self, device_id: str, limit: int = 100) -> list[Dict[str, Any]]:
        """Return recent append-only state samples, newest first."""
        bounded_limit = max(1, min(int(limit), 1000))
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM device_state_history
                   WHERE device_id=? ORDER BY sampled_at DESC, received_at DESC LIMIT ?""",
                (device_id, bounded_limit),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["values"] = json.loads(item.pop("values_json"))
            values.append(item)
        return values

    def register_asset(self, asset: Mapping[str, Any]) -> None:
        columns = ("asset_id", "robot_id", "task_run_id", "waypoint_id", "device_id", "media_type", "uri", "sha256", "size_bytes", "mime_type", "quality_state", "captured_at", "created_at", "cage_id")
        with self.connect() as connection:
            connection.execute(
                f"INSERT INTO media_assets({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(asset.get(key) if key == "cage_id" else asset[key] for key in columns),
            )

    def persist_inference_result(self, *, original_asset: Mapping[str, Any], result_asset: Optional[Mapping[str, Any]],
                                 inference: Mapping[str, Any], detections: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        """Atomically persist frame assets, inference metadata and normalized boxes."""
        request_id = str(inference["request_id"])
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute("SELECT run_id FROM inference_runs WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None:
                count = connection.execute("SELECT count(*) AS count FROM inference_detections WHERE request_id=?", (request_id,)).fetchone()["count"]
                return {"request_id": request_id, "run_id": str(existing["run_id"]), "duplicate": True, "detection_count": int(count)}
            assets = [original_asset] + ([result_asset] if result_asset else [])
            for asset in assets:
                columns = ("asset_id", "robot_id", "task_run_id", "waypoint_id", "device_id", "media_type", "uri", "sha256", "size_bytes", "mime_type", "quality_state", "captured_at", "created_at", "cage_id")
                connection.execute(f"INSERT INTO media_assets({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    tuple(asset.get(key) if key == "cage_id" else asset[key] for key in columns))
            run_id = new_id("infer")
            result = dict(inference.get("result", {}))
            connection.execute("""INSERT INTO inference_runs
                (run_id,request_id,trace_id,capability,model_version,asset_id,task_run_id,status,result,model_sha256,error_stage,suggested_action,started_at,finished_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (run_id, request_id, inference.get("trace_id", new_id("trace")),
                inference.get("capability", "unknown"), inference.get("model_version", "unknown"), original_asset["asset_id"],
                original_asset["task_run_id"], inference.get("status", "internal_error"), self._json(result), inference.get("model_sha256"),
                inference.get("error_stage"), inference.get("suggested_action"), inference.get("started_at", now), inference.get("finished_at", now)))
            count = 0
            for index, item in enumerate(detections):
                bbox = list(item.get("bbox_xyxy", ()))
                if len(bbox) < 4:
                    continue
                detection_id = f"{request_id}:{index}"
                connection.execute("""INSERT INTO inference_detections
                    (detection_id,request_id,task_run_id,camera_id,frame_id,label,class_id,confidence,x0,y0,x1,y1,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (detection_id, request_id, original_asset["task_run_id"],
                    inference.get("camera_id", original_asset.get("device_id", "camera")), int(inference.get("frame_id", 0)),
                    str(item.get("label", "unknown")), item.get("class_id"), float(item.get("confidence", 0)),
                    float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]), now))
                count += 1
            return {"request_id": request_id, "run_id": run_id, "duplicate": False, "detection_count": count}

    def enqueue_outbox(self, aggregate_type: str, aggregate_id: str, event_type: str, payload: Mapping[str, Any], idempotency_key: str) -> str:
        outbox_id = new_id("outbox")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO outbox(outbox_id,aggregate_type,aggregate_id,event_type,payload,idempotency_key,state,created_at) VALUES (?,?,?,?,?,?,'pending',?) ON CONFLICT DO NOTHING",
                (outbox_id, aggregate_type, aggregate_id, event_type, self._json(payload), idempotency_key, utc_now()),
            )
            row = connection.execute("SELECT outbox_id FROM outbox WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return str(row["outbox_id"])

    @staticmethod
    def _decode_outbox(row: Mapping[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def claim_outbox(self, limit: int = 50, lease_seconds: float = 60.0) -> list[Dict[str, Any]]:
        """Atomically claim deliverable events for one sync worker.

        ``FOR UPDATE SKIP LOCKED`` lets multiple workers drain the same queue
        without sending the same event concurrently.
        """
        bounded_limit = max(1, min(int(limit), 500))
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = utc_now()
        stale_before = (parse_utc(now) - timedelta(seconds=lease_seconds)).isoformat()
        token = new_id("claim")
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    SELECT outbox_id
                    FROM outbox
                    WHERE (
                        state IN ('pending', 'retry')
                        AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ) OR (
                        state = 'claimed'
                        AND claimed_at IS NOT NULL
                        AND claimed_at < ?
                    )
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT ?
                )
                UPDATE outbox AS item
                SET state='claimed', claim_token=?, claimed_at=?,
                    last_attempt_at=?, attempts=item.attempts + 1,
                    delivery_count=item.delivery_count + 1
                FROM candidates
                WHERE item.outbox_id = candidates.outbox_id
                RETURNING item.*
                """,
                (now, stale_before, bounded_limit, token, now, now),
            ).fetchall()
        return [self._decode_outbox(row) for row in rows]

    def mark_outbox_sent(
        self, outbox_id: str, claim_token: Optional[str] = None, remote_ack_at: Optional[str] = None
    ) -> bool:
        """Acknowledge one claimed event only when its lease still belongs to the worker."""
        acknowledged_at = remote_ack_at or utc_now()
        with self.connect() as connection:
            if claim_token is None:
                updated = connection.execute(
                    """
                    UPDATE outbox SET state='sent', sent_at=?, remote_ack_at=?,
                        claim_token=NULL, claimed_at=NULL, last_error=NULL
                    WHERE outbox_id=? AND state='claimed'
                    """,
                    (acknowledged_at, acknowledged_at, outbox_id),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE outbox SET state='sent', sent_at=?, remote_ack_at=?,
                        claim_token=NULL, claimed_at=NULL, last_error=NULL
                    WHERE outbox_id=? AND state='claimed' AND claim_token=?
                    """,
                    (acknowledged_at, acknowledged_at, outbox_id, claim_token),
                )
        return bool(getattr(updated, "rowcount", 0))

    def mark_outbox_failure(
        self,
        outbox_id: str,
        claim_token: str,
        error: str,
        *,
        retry_base_seconds: float = 2.0,
        max_attempts: int = 10,
        jitter_ratio: float = 0.1,
    ) -> str:
        """Release a claim with exponential backoff or move it to dead-letter."""
        if retry_base_seconds <= 0 or max_attempts < 1 or jitter_ratio < 0:
            raise ValueError("invalid outbox retry settings")
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM outbox WHERE outbox_id=? AND state='claimed' AND claim_token=?",
                (outbox_id, claim_token),
            ).fetchone()
            if row is None:
                return "ignored"
            attempts = int(row["attempts"])
            if attempts >= max_attempts:
                state = "dead_letter"
                next_retry_at = None
            else:
                state = "pending"
                delay = retry_base_seconds * (2 ** max(0, attempts - 1))
                delay *= 1.0 + (random.random() * jitter_ratio)
                next_retry_at = (parse_utc(now) + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """
                UPDATE outbox
                SET state=?, next_retry_at=?, last_error=?, claim_token=NULL, claimed_at=NULL
                WHERE outbox_id=? AND state='claimed' AND claim_token=?
                """,
                (state, next_retry_at, str(error)[:2000], outbox_id, claim_token),
            )
        return state

    def retry_outbox(self, outbox_id: str) -> bool:
        """Manually requeue a failed or dead-letter event for operator recovery."""
        with self.connect() as connection:
            updated = connection.execute(
                """
                UPDATE outbox
                SET state='pending', next_retry_at=NULL, last_error=NULL,
                    claim_token=NULL, claimed_at=NULL
                WHERE outbox_id=? AND state IN ('dead_letter', 'failed')
                """,
                (outbox_id,),
            )
        return bool(getattr(updated, "rowcount", 0))

    def record_inbox_message(
        self,
        remote_message_id: str,
        message_type: str,
        payload: Mapping[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert an inbound message once and return the existing row on replay."""
        inbox_id = new_id("inbox")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO inbox(inbox_id, remote_message_id, idempotency_key,
                    message_type, payload, state, received_at)
                VALUES (?, ?, ?, ?, ?, 'received', ?)
                ON CONFLICT DO NOTHING
                """,
                (inbox_id, remote_message_id, idempotency_key, message_type, self._json(payload), utc_now()),
            )
            row = connection.execute(
                """
                SELECT * FROM inbox
                WHERE remote_message_id=? OR (? IS NOT NULL AND idempotency_key=?)
                ORDER BY received_at LIMIT 1
                """,
                (remote_message_id, idempotency_key, idempotency_key),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive guard for a broken database
            raise RuntimeError("inbox insert did not return a row")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        if item.get("response_payload"):
            item["response_payload"] = json.loads(item["response_payload"])
        return item

    def complete_inbox(self, inbox_id: str, response_payload: Mapping[str, Any]) -> bool:
        with self.connect() as connection:
            updated = connection.execute(
                """
                UPDATE inbox SET state='processed', processed_at=?,
                    response_payload=?, last_error=NULL
                WHERE inbox_id=? AND state IN ('received', 'processing')
                """,
                (utc_now(), self._json(response_payload), inbox_id),
            )
        return bool(getattr(updated, "rowcount", 0))

    def pending_outbox(self, limit: int = 50) -> Iterable[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox WHERE state='pending' AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY created_at LIMIT ?",
                (utc_now(), max(1, min(limit, 500))),
            ).fetchall()
        for row in rows:
            yield self._decode_outbox(row)

    def table_names(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        return {str(row["name"]) for row in rows}
