"""Retryable emergency audit. Device stops happen before any audit I/O."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


class EmergencyAudit:
    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self._pending: dict[str, dict] = {}

    def enqueue(self, payload: dict) -> None:
        key = payload["event"]["event_id"]
        self._pending[key] = payload
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.directory / f".{key}.tmp"
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.directory / f"{key}.json")
            self._pending.pop(key, None)

    def drain_once(self, persist: Callable[[dict], None]) -> None:
        for key, payload in list(self._pending.items()):
            persist(payload)
            self._pending.pop(key, None)
        if self.directory is not None:
            paths = sorted(self.directory.glob("*.json"))[:50]
            for path in paths:
                persist(json.loads(path.read_text(encoding="utf-8")))
                path.unlink(missing_ok=True)
