"""One persistent reader per physical camera, shared by snapshots and inspection."""

from __future__ import annotations

import threading
import time
from typing import Mapping, Any

from .stream import build_camera_reader


class SharedReader:
    def __init__(self, reader):
        self.reader = reader
        self.lock = threading.Lock()
        self.latest = None
        self.captured_at = 0.0

    def __call__(self):
        with self.lock:
            self.latest = self.reader()
            self.captured_at = time.monotonic()
            return self.latest

    def snapshot(self):
        with self.lock:
            if self.latest is None or time.monotonic() - self.captured_at > 1.0:
                self.latest = self.reader()
                self.captured_at = time.monotonic()
            return self.latest

    def close(self):
        pass  # The process owns the reader; ending a task only releases its use.

    def shutdown(self):
        with self.lock:
            close = getattr(self.reader, "close", None)
            if close:
                close()


class CameraPool:
    def __init__(self, *, simulated: bool = False):
        self.simulated = simulated
        self._readers: dict[tuple, SharedReader] = {}
        self._lock = threading.Lock()

    def reader(self, values: Mapping[str, Any]) -> SharedReader:
        key = tuple(str(values.get(name, "")) for name in (
            "transport", "host", "port", "device", "base_url", "snapshot_path", "path", "stream",
            "username", "password", "width", "height",
        ))
        with self._lock:
            if key not in self._readers:
                if self.simulated:
                    import numpy as np
                    reader = lambda: np.zeros((int(values.get("height", 720)), int(values.get("width", 1280)), 3), dtype=np.uint8)
                else:
                    reader = build_camera_reader(values)
                self._readers[key] = SharedReader(reader)
            return self._readers[key]

    def capture_provider(self, values: Mapping[str, Any]):
        def capture(request):
            import cv2
            ok, encoded = cv2.imencode(".jpg", self.reader(values).snapshot(),
                                       [cv2.IMWRITE_JPEG_QUALITY, int(values.get("jpeg_quality", 90))])
            if not ok:
                raise RuntimeError("camera JPEG encoding failed")
            return bytes(encoded)
        return capture

    def close(self):
        for reader in self._readers.values():
            reader.shutdown()
        self._readers.clear()
