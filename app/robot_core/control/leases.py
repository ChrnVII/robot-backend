from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
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
        self._lock = Lock()

    def acquire(self, holder: str, mode: str) -> ControlLease:
        now = datetime.now(timezone.utc)
        priority = PRIORITIES[mode]
        with self._lock:
            previous = self.current
            if previous and parse_utc(previous.expires_at) > now and priority <= previous.priority:
                raise LeaseConflict(f"control held by {previous.holder}")
        if previous:
            self.store.release_lease(previous.lease_id, "preempted")
        lease = ControlLease(new_id("lease"), holder, mode, priority, now.isoformat(), (now + timedelta(milliseconds=self.ttl_ms)).isoformat())
        self.store.save_lease(lease)
        # A high-priority stop may preempt this acquisition while DB I/O runs.
        with self._lock:
            acquired = self.current is previous
            if acquired:
                self.current = lease
        if not acquired:
            self.store.release_lease(lease.lease_id, "preempted")
            raise LeaseConflict("control changed while acquiring lease")
        return lease

    def emergency_lease(self) -> tuple[ControlLease, Optional[ControlLease]]:
        """Preempt immediately in memory; persistence is handled by the audit spool."""
        now = datetime.now(timezone.utc)
        lease = ControlLease(
            new_id("lease"), "robot-core", "emergency", PRIORITIES["emergency"],
            now.isoformat(), (now + timedelta(milliseconds=self.ttl_ms)).isoformat(),
        )
        with self._lock:
            previous, self.current = self.current, lease
        return lease, previous

    def validate(self, lease_id: str) -> bool:
        with self._lock:
            current = self.current
        return bool(current and current.lease_id == lease_id and parse_utc(current.expires_at) > parse_utc(utc_now()))

    def release(self, lease_id: str, reason: str = "released") -> None:
        with self._lock:
            previous = self.current
        if previous and previous.lease_id == lease_id:
            self.store.release_lease(lease_id, reason)
            with self._lock:
                if self.current is previous:
                    self.current = None

