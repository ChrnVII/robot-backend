from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from app.robot_core.common import new_id
from app.robot_core.common.config import PlatformConfig


class InspectionService:
    """Controls one realtime inspection run and exposes its persisted evidence."""

    TERMINAL_STATES = {"completed", "stopped", "failed"}
    PIPELINES = {"inference", "passthrough"}

    def __init__(self, config: PlatformConfig, run_function: Callable[..., Any]) -> None:
        self.config = config
        settings = config.values.get("inspection", {})
        configured_root = Path(str(settings.get("output_root", config.values["storage"]["media_root"])))
        self.output_root = (
            configured_root if configured_root.is_absolute()
            else (config.source.parent / configured_root).resolve()
        )
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._run_function = run_function
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._active: dict[str, Any] | None = None
        self._latest_frame: dict[str, Any] | None = None
        self._latest_frames: dict[tuple[str, str], dict[str, Any]] = {}
        self._camera_errors: dict[str, dict[str, str]] = {}

    def start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        mode = str(request.get("mode", "single"))
        if mode not in {"single", "four", "eight", "custom"}:
            raise ValueError("mode must be single, four, eight, or custom")
        camera_ids = tuple(str(value) for value in request.get("camera_ids", ()))
        expected_count = {"single": 1, "four": 4, "eight": 8}.get(mode)
        if camera_ids:
            if not 1 <= len(camera_ids) <= 8:
                raise ValueError("camera_ids must contain between 1 and 8 cameras")
            if len(set(camera_ids)) != len(camera_ids):
                raise ValueError("camera_ids must not contain duplicates")
            # An explicit selection is authoritative. This makes it possible
            # to run any subset (for example cameras 2, 5 and 8) while keeping
            # single/four/eight requests backward compatible. ``mode`` is
            # retained as the caller's layout/capacity hint in the status.
        elif mode == "custom":
            raise ValueError("mode=custom requires camera_ids")
        elif expected_count is None:
            raise ValueError("mode must be single, four, eight, or custom")
        for camera_id in camera_ids:
            self._validate_id(camera_id, "camera_id")
        camera_count = len(camera_ids) or int(expected_count or 0)
        duration_seconds = max(0.0, float(request.get("duration_seconds", 0.0)))
        pipeline = str(request.get("pipeline", "inference"))
        if pipeline not in self.PIPELINES:
            raise ValueError("pipeline must be inference or passthrough")
        task_run_id = str(request.get("task_run_id") or new_id("inspection"))
        self._validate_id(task_run_id, "task_run_id")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("an inspection task is already running")
            if (self.output_root / task_run_id).exists():
                raise ValueError("task_run_id already exists")
            self._stop_event = threading.Event()
            self._active = {
                "task_run_id": task_run_id,
                "mode": mode,
                "pipeline": pipeline,
                "state": "starting",
                "processed": 0,
                "positive": 0,
                "camera_ids": list(camera_ids),
                "camera_count": camera_count,
            }
            self._write_status(self._active)
            self._thread = threading.Thread(
                target=self._run,
                args=(task_run_id, mode, pipeline, duration_seconds, camera_ids),
                daemon=True,
                name=f"inspection-{task_run_id}",
            )
            self._thread.start()
            return dict(self._active)

    def stop(self, task_run_id: str) -> dict[str, Any]:
        self._validate_id(task_run_id, "task_run_id")
        with self._lock:
            if self._active is None or self._active.get("task_run_id") != task_run_id:
                status = self.status(task_run_id)
                if status.get("state") in self.TERMINAL_STATES:
                    return status
                raise KeyError("inspection task is not active")
            if self._stop_event is not None:
                self._stop_event.set()
            self._active["state"] = "stopping"
            self._write_status(self._active)
            return dict(self._active)

    def status(self, task_run_id: str | None = None) -> dict[str, Any]:
        if task_run_id:
            self._validate_id(task_run_id, "task_run_id")
        with self._lock:
            if self._active is not None and (
                task_run_id is None or self._active.get("task_run_id") == task_run_id
            ):
                return dict(self._active)
        if task_run_id is None:
            return {"state": "idle"}
        task_root = self.output_root / task_run_id
        for name in ("summary.json", "status.json"):
            path = task_root / name
            if path.is_file():
                return dict(json.loads(path.read_text(encoding="utf-8")))
        raise KeyError("inspection task not found")

    def detections(self, task_run_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        self._validate_id(task_run_id, "task_run_id")
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        log_path = self.output_root / task_run_id / "inspection.jsonl"
        items: list[dict[str, Any]] = []
        total = 0
        if log_path.is_file():
            with log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    event = json.loads(line)
                    if event.get("event_type") not in {"detection", "inference"}:
                        continue
                    if offset <= total < offset + limit:
                        items.append(self._public_event(event))
                    total += 1
        elif not (self.output_root / task_run_id).is_dir():
            raise KeyError("inspection task not found")
        return {"items": items, "offset": offset, "limit": limit, "total": total}

    def resolve_image(self, task_run_id: str, image_id: str) -> dict[str, Any]:
        self._validate_id(task_run_id, "task_run_id")
        parts = image_id.split(":", 1)
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise ValueError("invalid image_id")
        if any(
            len(part) > limit
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in part)
            for part, limit in zip(parts, (128, 160))
        ):
            raise ValueError("invalid image_id")
        if not image_id.lower().endswith(".jpg"):
            raise ValueError("only JPEG evidence images are supported")
        task_root = (self.output_root / task_run_id).resolve()
        image_path = (task_root / parts[0] / parts[1]).resolve()
        if task_root not in image_path.parents or not image_path.is_file():
            raise FileNotFoundError("inspection image not found")
        return {"path": str(image_path), "content_type": "image/jpeg"}

    def health(self) -> dict[str, Any]:
        return {"state": "ok", "active": self.status()}

    def latest_frame(self, task_run_id: str, camera_id: str | None = None) -> dict[str, Any]:
        self._validate_id(task_run_id, "task_run_id")
        with self._lock:
            if camera_id is not None:
                self._validate_id(camera_id, "camera_id")
                frame = dict(self._latest_frames.get((task_run_id, camera_id), {}))
            else:
                frame = dict(self._latest_frame or {})
        if frame.get("task_run_id") != task_run_id:
            result = {"available": False, "task_run_id": task_run_id}
            if camera_id is not None:
                result["camera_id"] = camera_id
            return result
        return frame

    def latest_frames(self, task_run_id: str) -> dict[str, Any]:
        """Return the latest available frame for every camera in a task."""
        self._validate_id(task_run_id, "task_run_id")
        with self._lock:
            frames = {
                camera_id: dict(frame)
                for (run_id, camera_id), frame in self._latest_frames.items()
                if run_id == task_run_id and frame.get("task_run_id") == task_run_id
            }
        return {"task_run_id": task_run_id, "frames": frames}

    def _run(
        self, task_run_id: str, mode: str, pipeline: str, duration_seconds: float,
        camera_ids: tuple[str, ...],
    ) -> None:
        try:
            result = self._run_function(
                self.config,
                mode=mode,
                pipeline=pipeline,
                task_run_id=task_run_id,
                duration_seconds=duration_seconds,
                headless=True,
                stop_event=self._stop_event,
                status_callback=lambda value: self._update(task_run_id, mode, value),
                frame_callback=lambda value: self._update_latest_frame(task_run_id, value),
                camera_ids=camera_ids or None,
            )
            if isinstance(result, Mapping):
                self._update(task_run_id, mode, result)
            with self._lock:
                if self._active is not None and self._active.get("state") not in self.TERMINAL_STATES:
                    self._update(task_run_id, mode, {"state": "completed"})
        except Exception as error:
            self._update(task_run_id, mode, {
                "state": "failed", "error": str(error),
                "camera_errors": dict(self._camera_errors.get(task_run_id, {})),
            })
        finally:
            with self._lock:
                self._camera_errors.pop(task_run_id, None)
                if self._active is not None and self._active.get("task_run_id") == task_run_id:
                    self._active = None
                    self._stop_event = None
                if self._thread is threading.current_thread():
                    self._thread = None

    def _update(self, task_run_id: str, mode: str, values: Mapping[str, Any]) -> None:
        with self._lock:
            current = dict(self._active or {})
            if current and current.get("task_run_id") != task_run_id:
                return
            current.update(values)
            if "camera_errors" in values:
                self._camera_errors[task_run_id] = dict(values.get("camera_errors") or {})
            current["task_run_id"] = task_run_id
            current["mode"] = mode
            self._active = current
            self._write_status(current)

    def _update_latest_frame(self, task_run_id: str, values: Mapping[str, Any]) -> None:
        with self._lock:
            frame = {"task_run_id": task_run_id, "available": True, **dict(values)}
            self._latest_frame = frame
            camera_id = str(frame.get("camera_id", ""))
            if camera_id:
                self._latest_frames[(task_run_id, camera_id)] = frame

    def _write_status(self, status: Mapping[str, Any]) -> None:
        task_run_id = str(status["task_run_id"])
        task_root = self.output_root / task_run_id
        task_root.mkdir(parents=True, exist_ok=True)
        temporary = task_root / ".status.json.tmp"
        temporary.write_text(json.dumps(dict(status), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(task_root / "status.json")

    @staticmethod
    def _public_event(event: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(event)
        camera_id = str(value.get("camera_id", ""))
        frame_id = int(value.get("frame_id", 0))
        value["detection_id"] = f"{camera_id}-{frame_id:08d}"
        for source_key, public_key in (
            ("original_image", "original_image_id"),
            ("result_image", "result_image_id"),
        ):
            source = value.pop(source_key, None)
            if source:
                value[public_key] = f"{camera_id}:{Path(str(source)).name}"
        value["detections"] = list(value.get("detections") or [])
        value["latency_ms"] = value.get("latency_ms")
        value["captured_at"] = value.get("captured_at")
        return value

    @staticmethod
    def _validate_id(value: str, name: str) -> None:
        if not value or len(value) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value):
            raise ValueError(f"invalid {name}")
