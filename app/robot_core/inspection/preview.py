"""Bounded, atomically replaced preview files; IPC carries references only."""

from collections import deque
from pathlib import Path

from .output import InspectionOutput


class PreviewFrames:
    def __init__(self, root: Path, retain_per_camera: int = 16):
        self.root = root
        self.retain = max(2, retain_per_camera)
        self._paths: dict[str, deque[Path]] = {}

    def recover(self) -> None:
        """Called after acquiring both IPC sockets, before any task can start."""
        for path in self.root.glob("*/*/*.jpg"):
            path.unlink(missing_ok=True)
        for path in self.root.glob("*/*/.*.jpg-*"):
            path.unlink(missing_ok=True)
        self._paths.clear()

    def save(self, task_run_id: str, camera_id: str, frame_id: int, image) -> str:
        directory = self.root / task_run_id / camera_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{frame_id:08d}.jpg"
        InspectionOutput._write_image(path, image)
        paths = self._paths.setdefault(camera_id, deque())
        paths.append(path)
        while len(paths) > self.retain:
            paths.popleft().unlink(missing_ok=True)
        return str(path.resolve())
