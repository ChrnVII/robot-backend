"""巡检任务执行引擎（inspection-worker 的唯一任务运行体）。

本模块原名 ``realtime_demo``——命名源于早期实时演示，实际上它是生产巡检
引擎：由 ``inspection_worker`` 注入 ``InspectionService``，并由 systemd 单元
``inspection-worker.service`` 拉起。更名以消除「演示代码混入生产路径」的误导。

职责：按配置装配多路相机读取器与推理运行时，循环采集 → 推理 → 标注 →
落盘 → 感知进程直接持久化，并输出 summary.json。
``main()`` 的 CLI 入口与非 headless 的 OpenCV 窗口仅用于本机调试。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.robot_core.common import new_id
from app.robot_core.common.config import PlatformConfig, load_config
from app.robot_core.contracts import InferenceRequest
from app.robot_core.inference import InferenceRuntime, build_configured_adapters, annotate_detections
from app.robot_core.inspection import InspectionOutput
from app.robot_core.sensing import MultiCameraManager, build_camera_reader
from app.robot_core.storage import PlatformStore
from app.robot_core.inspection.preview import PreviewFrames


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inspection-engine")
    parser.add_argument("--config")
    parser.add_argument("--mode", choices=("single", "four", "eight", "custom"))
    parser.add_argument("--camera-ids", nargs="+", metavar="CAMERA_ID",
                        help="选择 1 到 8 路摄像头，例如 --camera-ids cam02 cam05 cam08")
    parser.add_argument("--pipeline", choices=("inference", "passthrough"), default="inference")
    parser.add_argument("--task-run-id")
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--check", action="store_true")
    return parser


def _rooted(config: PlatformConfig, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (config.source.parent / path).resolve()


def _camera_config(
    config: PlatformConfig, mode: str, camera_ids: Sequence[str] | None = None,
) -> dict[str, Mapping[str, Any]]:
    devices = config.values.get("devices", {})
    cameras = devices.get("cameras", {})
    items = cameras.get("items", []) if isinstance(cameras, Mapping) else []
    values: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if item.get("enabled", True):
            camera_id = str(item.get("id", item.get("camera_id", ""))).strip()
            if camera_id:
                values[camera_id] = item
    if not values and isinstance(devices.get("camera"), Mapping):
        item = dict(devices["camera"])
        values[str(item.get("camera_id", "cam01"))] = item
    if mode not in {"single", "four", "eight", "custom"}:
        raise ValueError("mode must be single, four, eight, or custom")
    count = {"single": 1, "four": 4, "eight": 8}.get(mode, 0)
    if camera_ids:
        if not 1 <= len(camera_ids) <= 8:
            raise ValueError("camera_ids must contain between 1 and 8 cameras")
        if len(set(camera_ids)) != len(camera_ids):
            raise ValueError("camera_ids must not contain duplicates")
        missing = [camera_id for camera_id in camera_ids if camera_id not in values]
        if missing:
            raise ValueError(f"requested cameras are not enabled: {', '.join(missing)}")
        selected = [(camera_id, values[camera_id]) for camera_id in camera_ids]
    else:
        if mode == "custom":
            raise ValueError("mode=custom requires camera_ids")
        selected = list(values.items())[:count]
    if not camera_ids and len(selected) != count:
        raise ValueError(f"mode={mode} requires {count} enabled cameras, configured {len(selected)}")
    return dict(selected)


def _check(config: PlatformConfig, mode: str) -> None:
    cameras = _camera_config(config, mode)
    runtime = InferenceRuntime(build_configured_adapters(config))
    runtime.start()
    try:
        model_health = {key: value.health() for key, value in runtime.adapters.items()}
    finally:
        runtime.stop()
    print(json.dumps({
        "service": "inspection-engine", "status": "ok", "mode": mode,
        "cameras": list(cameras), "models": list(runtime.adapters),
        "modelHealth": model_health,
    }, ensure_ascii=False))


def run(
    config: PlatformConfig,
    *,
    mode: str,
    pipeline: str = "inference",
    task_run_id: str,
    duration_seconds: float,
    headless: bool,
    stop_event: threading.Event | None = None,
    status_callback: Any = None,
    frame_callback: Any = None,
    camera_ids: Sequence[str] | None = None,
    store: PlatformStore | None = None,
    camera_pool: Any = None,
    inference_runtime: InferenceRuntime | None = None,
    preview: PreviewFrames | None = None,
) -> dict[str, Any]:
    if pipeline not in {"inference", "passthrough"}:
        raise ValueError("pipeline must be inference or passthrough")
    camera_values = _camera_config(config, mode, camera_ids)
    readers = {camera_id: camera_pool.reader(values) if camera_pool is not None else build_camera_reader(values)
               for camera_id, values in camera_values.items()}
    settings = config.values.get("inspection", {})
    queue_size = int(settings.get("queue_size", 2))
    batch_size = int(settings.get("batch_size", len(readers)))
    output_root = _rooted(config, str(settings.get("output_root", config.values["storage"]["media_root"])))
    output = InspectionOutput(output_root, task_run_id) if pipeline == "inference" else None
    owns_store = store is None and pipeline == "inference"
    if owns_store:
        store = PlatformStore(config.database_url_for("perception"), pool_options=config.database_pool_options)
    owns_runtime = inference_runtime is None and pipeline == "inference"
    runtime = (inference_runtime or InferenceRuntime(build_configured_adapters(config))) if pipeline == "inference" else None
    configured_fps = settings.get("capture_fps")
    if configured_fps is None:
        configured_fps = 3.0 if mode == "single" else 2.0
    capture_fps = float(configured_fps)
    if capture_fps <= 0:
        raise ValueError("inspection.capture_fps must be greater than zero")
    startup_timeout = float(settings.get("camera_startup_timeout_seconds", 30.0))
    if startup_timeout <= 0 or startup_timeout > 300:
        raise ValueError("inspection.camera_startup_timeout_seconds must be greater than zero and at most 300")
    manager = MultiCameraManager(
        readers,
        queue_size=queue_size,
        task_run_id=task_run_id,
        capture_interval_seconds=1.0 / capture_fps,
    )
    started = time.monotonic()
    started_at = time.time()
    processed = 0
    positive = 0
    state = "starting"
    error_message: str | None = None
    last_camera_errors: dict[str, str] = {}
    try:
        if runtime is not None and owns_runtime:
            runtime.start()
        manager.start()
        state = "running"
        if status_callback:
            status_callback({"state": state, "pipeline": pipeline,
                             "processed": processed, "positive": positive})
        while True:
            if stop_event is not None and stop_event.is_set():
                state = "stopped"
                break
            if duration_seconds > 0 and time.monotonic() - started >= duration_seconds:
                state = "completed"
                break
            packets = list(manager.packets(max_packets=batch_size))
            if not packets:
                camera_errors = dict(manager.errors)
                if camera_errors != last_camera_errors and status_callback:
                    last_camera_errors = camera_errors
                    status_callback({"state": state, "processed": processed, "positive": positive,
                                     "camera_errors": camera_errors})
                if processed == 0 and time.monotonic() - started >= startup_timeout:
                    state = "failed"
                    failures = ", ".join(sorted(camera_errors)) or "selected cameras"
                    error_message = f"no frames received from {failures} before startup timeout"
                    break
                time.sleep(0.01)
                continue
            for packet in packets:
                if pipeline == "passthrough":
                    result = {"detections": [], "count_by_label": {}, "status": "passthrough"}
                    annotated = packet.image
                else:
                    assert runtime is not None
                    request = InferenceRequest(
                        capability="heat_stress_detection", asset_id=f"{packet.camera_id}-{packet.frame_id}",
                        task_run_id=task_run_id, context={"image": packet.image},
                    )
                    response = runtime.predict(request)
                    result = dict(response.result)
                    result.update({"model_id": response.model_id, "model_version": response.model_version,
                                   "model_sha256": response.model_sha256, "status": response.status,
                                   "latency_ms": response.latency_ms, "result_schema": response.result_schema,
                                   "trace_id": response.trace_id})
                    annotated = annotate_detections(packet.image, result)
                if frame_callback:
                    frame = {
                        "camera_id": packet.camera_id,
                        "frame_id": packet.frame_id,
                        "captured_at": packet.captured_at,
                        "captured_at_iso": datetime.fromtimestamp(packet.captured_at, timezone.utc).isoformat(),
                        "pipeline": pipeline,
                    }
                    if preview is None:
                        frame["jpeg_base64"] = _encode_jpeg_base64(annotated)
                    else:
                        frame["jpeg_path"] = preview.save(task_run_id, packet.camera_id, packet.frame_id, annotated)
                    frame_callback(frame)
                if output is not None:
                    paths = output.write(packet, result, annotated, persist_all=True)
                    if paths:
                        original_asset = _asset_from_image(paths["original_image"], config.robot_id, task_run_id, packet.camera_id, packet.captured_at)
                        result_asset = _asset_from_image(paths["result_image"], config.robot_id, task_run_id, packet.camera_id, packet.captured_at)
                        inference = {
                            "request_id": f"{task_run_id}:{packet.camera_id}:{packet.frame_id}",
                            "trace_id": result.get("trace_id") or f"{task_run_id}:{packet.camera_id}",
                            "capability": "heat_stress_detection", "model_version": result.get("model_version", "unknown"),
                            "model_sha256": result.get("model_sha256"), "status": result.get("status", "internal_error"),
                            "result": result, "camera_id": packet.camera_id, "frame_id": packet.frame_id,
                            "started_at": datetime.fromtimestamp(packet.captured_at, timezone.utc).isoformat(),
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        }
                        assert store is not None
                        store.persist_inference_result(original_asset=original_asset, result_asset=result_asset,
                            inference=inference, detections=result.get("detections", []))
                positive += int(bool(result.get("detections")))
                processed += 1
                if status_callback:
                    current_errors = dict(manager.errors)
                    last_camera_errors = current_errors
                    status_callback({"state": state, "processed": processed, "positive": positive,
                                     "camera_errors": current_errors})
                if not headless:
                    try:
                        import cv2
                        cv2.imshow(f"inspection:{packet.camera_id}", annotated)
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            return
                    except ImportError as error:
                        raise RuntimeError("opencv-python is required for non-headless display") from error
    except Exception as error:
        state = "failed"
        error_message = str(error)
        raise
    finally:
        manager.stop()
        for reader in readers.values():
            close = getattr(reader, "close", None)
            if close:
                close()
        if runtime is not None and owns_runtime:
            runtime.stop()
        if owns_store and store is not None:
            store.close()
        try:
            import cv2
            if not headless:
                cv2.destroyAllWindows()
        except ImportError:
            pass
        if state == "running":
            state = "stopped" if stop_event is not None and stop_event.is_set() else "completed"
        summary = {
            "task_run_id": task_run_id, "mode": mode, "pipeline": pipeline, "state": state,
            "processed": processed, "positive": positive,
            "camera_ids": list(camera_values), "camera_count": len(camera_values),
            "camera_errors": manager.errors, "started_at": started_at,
            "finished_at": time.time(), "error": error_message,
        }
        output_directory = output.root if output is not None else output_root / task_run_id
        output_directory.mkdir(parents=True, exist_ok=True)
        (output_directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if output is not None and state in {"completed", "stopped"}:
            output.freeze()
        if status_callback:
            status_callback(summary)
    return summary


def _encode_jpeg_base64(image: Any) -> str:
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError("opencv-python is required for realtime inspection frames") from error
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("failed to encode realtime inspection frame")
    return base64.b64encode(bytes(encoded)).decode("ascii")


def _asset_from_image(path: str, robot_id: str, task_run_id: str, camera_id: str, captured_at: float) -> dict[str, Any]:
    file_path = Path(path).resolve()
    data = file_path.read_bytes()
    return {"asset_id": f"asset-{task_run_id}-{camera_id}-{file_path.stem}", "robot_id": robot_id,
            "task_run_id": task_run_id, "waypoint_id": camera_id, "device_id": camera_id,
            "media_type": "image", "uri": file_path.as_uri(), "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data), "mime_type": "image/jpeg", "quality_state": "valid",
            "captured_at": datetime.fromtimestamp(captured_at, timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat()}


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config)
    mode = args.mode or str(config.values.get("inspection", {}).get("mode", "single"))
    if args.check:
        _check(config, mode)
        return
    task_run_id = args.task_run_id or new_id("inspection")
    run(config, mode=mode, pipeline=args.pipeline, task_run_id=task_run_id,
        duration_seconds=max(0.0, args.duration_seconds), headless=args.headless,
        camera_ids=args.camera_ids)


if __name__ == "__main__":
    main()
