from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.robot_core.common import new_id, parse_utc, utc_now
from app.robot_core.contracts import ControlLease
from app.robot_core.storage import PlatformStore


PRIORITIES = {"automatic": 10, "manual_remote": 20, "manual_local": 30, "maintenance": 40, "emergency": 100}


class LeaseConflict(RuntimeError):
    pass


class LeaseManager:
    def __init__(self, store: PlatformStore, ttl_ms: int = 3000):
        self.store = store
        self.ttl_ms = ttl_ms
        self.current: Optional[ControlLease] = None

    def acquire(self, holder: str, mode: str) -> ControlLease:
        now = datetime.now(timezone.utc)
        priority = PRIORITIES[mode]
        if self.current and parse_utc(self.current.expires_at) > now and priority <= self.current.priority:
            raise LeaseConflict(f"control held by {self.current.holder}")
        if self.current:
            self.store.release_lease(self.current.lease_id, "preempted")
        lease = ControlLease(new_id("lease"), holder, mode, priority, now.isoformat(), (now + timedelta(milliseconds=self.ttl_ms)).isoformat())
        self.store.save_lease(lease)
        self.current = lease
        return lease

    def validate(self, lease_id: str) -> bool:
        return bool(self.current and self.current.lease_id == lease_id and parse_utc(self.current.expires_at) > parse_utc(utc_now()))

    def release(self, lease_id: str, reason: str = "released") -> None:
        if self.current and self.current.lease_id == lease_id:
            self.store.release_lease(lease_id, reason)
            self.current = None

