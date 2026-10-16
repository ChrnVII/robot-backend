"""Incremental index for legacy JSONL evidence (new tasks query PostgreSQL)."""

import json
from pathlib import Path


class DetectionLogIndex:
    def __init__(self):
        self._identity = None
        self._position = 0
        self._offsets: list[int] = []

    def page(self, path: Path, offset: int, limit: int) -> tuple[list[dict], int]:
        stat = path.stat()
        identity = (str(path), stat.st_dev, stat.st_ino)
        if identity != self._identity or stat.st_size < self._position:
            self._identity, self._position, self._offsets = identity, 0, []
        with path.open("rb") as handle:
            handle.seek(self._position)
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line.endswith(b"\n"):
                    break  # Writer has not finished the next record yet.
                value = json.loads(line)
                if value.get("event_type") in {"inference", "detection"}:
                    self._offsets.append(start)
                self._position = handle.tell()
            items = []
            for start in self._offsets[offset:offset + limit]:
                handle.seek(start)
                items.append(json.loads(handle.readline()))
        return items, len(self._offsets)
