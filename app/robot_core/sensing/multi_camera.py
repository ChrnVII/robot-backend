from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


@dataclass(frozen=True)
class FramePacket:
    camera_id: str
    frame_id: int
    captured_at: float
    image: Any
    task_run_id: str = ""


class LatestFrameQueue:
    """Bounded queue that keeps the newest frame for low-latency inspection."""

    def __init__(self, maxsize: int = 2):
        self._queue: queue.Queue[FramePacket] = queue.Queue(maxsize=max(1, maxsize))
        self.dropped = 0

    def put_latest(self, packet: FramePacket) -> None:
        try:
            self._queue.put_nowait(packet)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            self._queue.put_nowait(packet)

    def get(self, timeout: float = 0.2) -> FramePacket:
        return self._queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self._queue.empty()


class MultiCameraManager:
    """Runs one reader thread per camera and isolates camera failures."""

    def __init__(
        self,
        cameras: Mapping[str, Callable[[], Any]],
        *,
        queue_size: int = 2,
        task_run_id: str = "",
        capture_interval_seconds: float = 0.0,
    ):
        self.cameras = dict(cameras)
        self.task_run_id = task_run_id
        self.capture_interval_seconds = max(0.0, float(capture_interval_seconds))
        self.queues = {camera_id: LatestFrameQueue(queue_size) for camera_id in self.cameras}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._frame_ids = {camera_id: 0 for camera_id in self.cameras}
        self.errors: dict[str, str] = {}
        self._packet_cursor = 0

    def start(self) -> None:
        for camera_id, reader in self.cameras.items():
            thread = threading.Thread(target=self._run, args=(camera_id, reader), daemon=True, name=f"camera-{camera_id}")
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()

    def _run(self, camera_id: str, reader: Callable[[], Any]) -> None:
        while not self._stop.is_set():
            cycle_started = time.monotonic()
            try:
                image = reader()
                if image is None:
                    raise RuntimeError("camera returned an empty frame")
                self._frame_ids[camera_id] += 1
                self.queues[camera_id].put_latest(FramePacket(
                    camera_id, self._frame_ids[camera_id], time.time(), image, self.task_run_id
                ))
                self.errors.pop(camera_id, None)
            except Exception as error:
                # Reader errors are surfaced in task status and can reach the UI.
                # Keep this boundary safe even if a provider accidentally includes
                # credentials in a URL or exception string.
                message = str(error)
                message = re.sub(r"(?i)(rtsp|rtsps)://[^/@\s]+@", r"\1://***@", message)
                message = re.sub(r"(?i)(password|passwd|pwd)=([^&\s]+)", r"\1=***", message)
                self.errors[camera_id] = message
                self._stop.wait(0.5)
            if self.capture_interval_seconds:
                remaining = self.capture_interval_seconds - (time.monotonic() - cycle_started)
                if remaining > 0:
                    self._stop.wait(remaining)

    def packets(self, max_packets: int | None = None) -> Iterable[FramePacket]:
        """Yield available packets fairly across cameras.

        A bounded read must not drain every queue and then truncate the result:
        doing so permanently favors cameras earlier in the mapping. The cursor
        advances after each camera is considered, so successive calls rotate
        service across all configured cameras.
        """
        camera_ids = list(self.queues)
        if not camera_ids:
            return
        limit = len(camera_ids) if max_packets is None else max(0, int(max_packets))
        if limit == 0:
            return
        yielded = 0
        checked = 0
        index = self._packet_cursor % len(camera_ids)
        while checked < len(camera_ids) and yielded < limit:
            camera_id = camera_ids[index]
            index = (index + 1) % len(camera_ids)
            checked += 1
            try:
                packet = self.queues[camera_id].get(timeout=0.01)
            except queue.Empty:
                continue
            self._packet_cursor = index
            yielded += 1
            yield packet
        self._packet_cursor = index
