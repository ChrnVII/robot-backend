from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from app.robot_core.storage import PlatformStore


class OutboxDispatcher:
    def __init__(
        self,
        store: PlatformStore,
        sender: Callable[[Mapping[str, object]], Optional[Mapping[str, Any]]],
        *,
        retry_base_seconds: float = 2.0,
        max_attempts: int = 10,
    ):
        self.store = store
        self.sender = sender
        self.retry_base_seconds = retry_base_seconds
        self.max_attempts = max_attempts

    def drain_once(self, limit: int = 50) -> int:
        sent = 0
        for item in self.store.claim_outbox(limit):
            try:
                acknowledgement = self.sender(item)
                remote_ack_at = acknowledgement.get("acknowledged_at") if acknowledgement else None
                if self.store.mark_outbox_sent(item["outbox_id"], item["claim_token"], remote_ack_at):
                    sent += 1
            except Exception as exc:
                self.store.mark_outbox_failure(
                    item["outbox_id"],
                    item["claim_token"],
                    str(exc),
                    retry_base_seconds=self.retry_base_seconds,
                    max_attempts=self.max_attempts,
                )
        return sent
