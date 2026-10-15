from __future__ import annotations

from typing import Optional

from app.robot_core.common import new_id, utc_now
from app.robot_core.storage import PlatformStore


class AlertService:
    def __init__(self, store: PlatformStore):
        self.store = store

    def create(self, *, trace_id: str, severity: str, message: str,
               source_run_id: Optional[str] = None, evidence_asset_id: Optional[str] = None) -> str:
        alert_id = new_id("alert")
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO alerts(alert_id,trace_id,source_run_id,severity,state,evidence_asset_id,message,created_at) VALUES (?,?,?,?,'open',?,?,?)",
                (alert_id, trace_id, source_run_id, severity, evidence_asset_id, message, utc_now()),
            )
        return alert_id

    def review(self, alert_id: str, reviewer: str, conclusion: str, notes: str = "") -> str:
        review_id = new_id("review")
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO reviews(review_id,alert_id,reviewer,conclusion,notes,reviewed_at) VALUES (?,?,?,?,?,?)",
                (review_id, alert_id, reviewer, conclusion, notes, utc_now()),
            )
            connection.execute(
                "UPDATE alerts SET state='acknowledged', acknowledged_by=?, acknowledged_at=? WHERE alert_id=?",
                (reviewer, utc_now(), alert_id),
            )
        return review_id

