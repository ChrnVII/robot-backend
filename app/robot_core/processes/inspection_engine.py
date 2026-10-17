"""Realtime inspection: one RTSP input per camera, independent preview/inference/disk."""
from __future__ import annotations
import argparse
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from app.common.cameras import ptz_camera_id
from app.robot_core.common.config import PlatformConfig, load_config
from app.robot_core.common import new_id
from app.robot_core.contracts import InferenceRequest
from app.robot_core.inference import InferenceRuntime, build_configured_adapters
from app.robot_core.inspection.output import InspectionOutput
from app.robot_core.inspection.paths import task_directory
from app.video.pipeline import CameraBroadcaster, FrameSlot
from app.video.reader import RtspStreamReader
from app.video.inspection_recorder import InspectionRecorder

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
        # 编号与相机目录共用同一兜底，避免两处各写一份（旧代码 str(None) 会得到 "None"）
        values[ptz_camera_id(config.values)] = item
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



class InspectionFrameSlot(FrameSlot):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    def put(self, frame, pts_ms, src_pts_ms=None):
        # Capture mapping on the reader thread; reconnect must not reinterpret an old frame.
        super().put((frame, self.channel.timestamp(src_pts_ms)), pts_ms, src_pts_ms)


def camera_url(values):
    if values.get('transport') != 'rtsp':
        raise ValueError('实时检测需要 RTSP 摄像头，请检查平台配置')
    user = quote(str(values.get('username') or ''), safe='')
    password = quote(str(values.get('password') or ''), safe='')
    auth = f'{user}:{password}@' if user else ''
    path = values.get('path') or '/avstream/channel=1/stream=0.sdp'
    return f"rtsp://{auth}{values['host']}:{values.get('port', 554)}{path}"


def run(config, *, mode, task_run_id, duration_seconds, headless=True, pipeline='inference',
        stop_event=None, status_callback=None, camera_ids=None,
        inference_runtime=None, output_directory=None, preview=None, task_name='巡检'):
    cameras = _camera_config(config, mode, camera_ids)
    urls = {key: camera_url(value) for key, value in cameras.items()}
    directory = Path(output_directory) if output_directory else task_directory(config, task_name, task_run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    started = time.monotonic()
    status = dict(task_run_id=task_run_id, task_name=task_name, mode=mode, pipeline=pipeline, state='starting',
                  processed=0, positive=0, camera_ids=list(cameras), camera_count=len(cameras),
                  output_dir=str(directory), started_at=time.time(), camera_errors={})
    output = InspectionOutput(directory, status)
    recorder = None
    readers = {}
    channels = {key: CameraBroadcaster() for key in cameras}
    runtime = inference_runtime
    own_runtime = runtime is None and pipeline == 'inference'
    counts = dict.fromkeys(cameras, 0)
    last_status = 0
    try:
        if pipeline == 'inference' and own_runtime:
            runtime = InferenceRuntime(build_configured_adapters(config))
            runtime.start()
        started = time.monotonic()
        recorder = InspectionRecorder(directory / 'video.mkv', cameras)
        slots = {key: InspectionFrameSlot(recorder.channels[key]) for key in cameras}
        if preview:
            preview.bind(task_run_id, channels)
        for key in cameras:
            readers[key] = RtspStreamReader(urls[key], channels[key], stop_event=stop,
                                           slot=slots[key], recorder=recorder.channels[key])
            channels[key].bind_timeline(readers[key].timeline.t0_epoch_ms)
            readers[key].start()
        timeout = float(config.values.get('inspection', {}).get('camera_startup_timeout_seconds', 30))
        while True:
            recorder.check()
            if output.error:
                raise RuntimeError(output.error)
            if stop_event is not None and stop_event.is_set():
                status['state'] = 'stopped'
                break
            if duration_seconds > 0 and time.monotonic() - started >= duration_seconds:
                status['state'] = 'completed'
                break
            missing = [key for key, reader in readers.items() if not reader.keyframes_rx]
            if missing and time.monotonic() - started > timeout:
                raise RuntimeError('摄像头启动超时：' + ', '.join(missing))
            if not missing:
                status['state'] = 'running'
            consumed = False
            # Round robin; each camera keeps only its latest decoded frame.
            for key, slot in slots.items():
                item = slot.take(timeout=0)
                if item is None:
                    continue
                consumed = True
                (frame, timing), pts, source_pts = item
                if timing['video_pts'] is None:
                    continue
                timing['track_index'] = recorder.track_indices.get(key)
                image = frame.to_ndarray(format='bgr24')
                counts[key] += 1
                result = {}
                if pipeline == 'inference':
                    response = runtime.predict(InferenceRequest(capability='heat_stress_detection',
                        asset_id=f'{key}-{counts[key]}', task_run_id=task_run_id, context={'image': image}))
                    if response.status != 'ok':
                        raise RuntimeError(f'{key} 推理失败：{response.status}')
                    result = dict(response.result)
                    result.update(model_id=response.model_id, model_version=response.model_version,
                                  model_sha256=response.model_sha256, latency_ms=response.latency_ms)
                record = dict(camera_id=key, frame_id=counts[key], pts=pts, **timing,
                              w=image.shape[1], h=image.shape[0], **result)
                output.add(record)
                channels[key].publish(dict(t='det', **record))
                status['processed'] += 1
                status['positive'] += bool(result.get('detections'))
            if time.monotonic() - last_status >= .5:
                status['camera_errors'] = {key: reader.last_error or reader.fatal
                                          for key, reader in readers.items() if reader.last_error or reader.fatal}
                status['recorded_packets'] = recorder.written
                if status_callback:
                    status_callback(dict(status))
                last_status = time.monotonic()
            if not consumed:
                stop.wait(.01)
    except Exception as exc:
        status.update(state='failed', error=str(exc))
    finally:
        stop.set()
        # Input containers own codec templates: drain the recorder before closing them.
        for reader in readers.values():
            reader.join(timeout=20)
            if reader.is_alive():
                status.update(state='failed', error='摄像头拉流线程未能及时退出')
        if preview:
            preview.clear()
        if recorder:
            try:
                recorder.close()
            except Exception as exc:
                status.update(state='failed', error=str(exc))
        if own_runtime and runtime:
            try:
                runtime.stop()
            except Exception as exc:
                status.update(state='failed', error=str(exc))
        status['finished_at'] = time.time()
        status['tracks'] = recorder.track_indices if recorder else {}
        try:
            output.close(status)
        except Exception as exc:
            status.update(state='failed', error=str(exc))
        if status_callback:
            status_callback(dict(status))
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--mode', default='single')
    parser.add_argument('--camera-ids', nargs='+')
    parser.add_argument('--duration-seconds', type=float, default=0)
    args = parser.parse_args()
    run(load_config(args.config), mode=args.mode, camera_ids=args.camera_ids,
        task_run_id=new_id('inspection'), duration_seconds=args.duration_seconds)


if __name__ == '__main__':
    main()
