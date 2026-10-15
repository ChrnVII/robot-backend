from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.robot_core.common import freeze_directory, MANIFEST_NAME


class InspectionOutput:
    """Persists positive evidence images and one structured JSONL event per result."""

    def __init__(self, root: str | Path, task_run_id: str):
        self.root = Path(root).expanduser().resolve() / task_run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "inspection.jsonl"

    def write(self, packet: Any, result: Mapping[str, Any], annotated: Any = None, *, persist_all: bool = False) -> dict[str, str] | None:
        if (self.root / MANIFEST_NAME).exists():
            raise RuntimeError(f"inspection output is frozen: {self.root}")
        detections = list(result.get("detections", []))
        event = {
            "event_type": "detection" if detections else "inference",
            "task_run_id": packet.task_run_id,
            "camera_id": packet.camera_id,
            "frame_id": packet.frame_id,
            "captured_at": datetime.fromtimestamp(packet.captured_at, timezone.utc).isoformat(),
            "detections": detections,
            "count_by_label": dict(result.get("count_by_label", {})),
            "latency_ms": result.get("latency_ms"),
            "status": result.get("status"),
            "trace_id": result.get("trace_id"),
            "model_id": result.get("model_id"),
            "model_version": result.get("model_version"),
            "model_sha256": result.get("model_sha256"),
            "result_schema": result.get("result_schema"),
        }
        paths: dict[str, str] | None = None
        if (persist_all or detections) and annotated is not None:
            camera_dir = self.root / packet.camera_id
            camera_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{packet.frame_id:08d}"
            original = camera_dir / f"{stem}_original.jpg"
            result_path = camera_dir / f"{stem}_result.jpg"
            self._write_image(original, packet.image)
            self._write_image(result_path, annotated)
            paths = {"original_image": str(original), "result_image": str(result_path)}
            event.update(paths)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return paths

    def freeze(self) -> Path:
        return freeze_directory(
            self.root, kind="inspection_run", identifier=self.root.name, version="1",
            exclude_names={"summary.json", "status.json"},
        )

    @staticmethod
    def _write_image(path: Path, image: Any) -> None:
        try:
            import cv2
        except ImportError as error:
            raise RuntimeError("opencv-python is required to save inspection evidence") from error
        ok, encoded = cv2.imencode(".jpg", image)
        if not ok:
            raise RuntimeError(f"failed to encode inspection image: {path}")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(bytes(encoded))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
