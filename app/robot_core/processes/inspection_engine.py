"""Main-stream remux recording; sub-stream preview and sampled CPU inference."""
from __future__ import annotations
import argparse
import math
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
from app.video.codec import Timeline
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
    def __init__(self, channel, timeline):
        super().__init__()
        self.channel = channel
        self.timeline = timeline
        self.reader = None

    def put(self, frame, pts_ms, src_pts_ms=None):
        # Snapshot at capture, so inference/reconnect cannot reinterpret an old frame.
        arrived_at = self.timeline.monotonic_at(pts_ms)
        timing = self.channel.arrival_timestamp(arrived_at)
        timing.update(src_pts=src_pts_ms, detection_session=self.reader.sessions)
        super().put((frame, timing), pts_ms, src_pts_ms)


def camera_url(values, *, detection=False):
    if values.get('transport') != 'rtsp':
        raise ValueError('实时检测需要 RTSP 摄像头，请检查平台配置')
    user = quote(str(values.get('username') or ''), safe='')
    password = quote(str(values.get('password') or ''), safe='')
    auth = f'{user}:{password}@' if user else ''
    path = values.get('path') or '/avstream/channel=1/stream=0.sdp'
    if detection:
        # Unknown vendors must configure their real substream, never silently use main.
        subpath = values.get('detection_path')
        if not subpath and str(path).endswith('/stream=0.sdp'):
            subpath = str(path)[:-len('0.sdp')] + '1.sdp'
        if not subpath or subpath == path:
            raise ValueError('请为摄像头配置独立子码流 detection_path')
        path = subpath
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('摄像头码流路径必须以 / 开头')
    return f"rtsp://{auth}{values['host']}:{values.get('port', 554)}{path}"


def inspection_settings(config):
    settings = config.values.get('inspection', {})
    result = {}
    for key, default, maximum in [('capture_fps', 2, 60),
                                  ('camera_startup_timeout_seconds', 30, 300),
                                  ('stream_stall_timeout_seconds', 15, 300),
                                  ('max_frame_age_seconds', 1, 10)]:
        value = float(settings.get(key, default))
        if not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError(f'inspection.{key} must be > 0 and <= {maximum}')
        result[key] = value
    threads = settings.get('decode_threads', 1)
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 8:
        raise ValueError('inspection.decode_threads must be an integer between 1 and 8')
    result['decode_threads'] = threads
    return result


def _cleanup_failed(status, message):
    status['state'] = 'failed'
    if status.get('error'):
        status.setdefault('cleanup_errors', []).append(message)
    else:
        status['error'] = message


def run(config, *, mode, task_run_id, duration_seconds, headless=True, pipeline='inference',
        stop_event=None, status_callback=None, camera_ids=None,
        inference_runtime=None, output_directory=None, preview=None, task_name='巡检'):
    if pipeline not in {'inference', 'passthrough'}:
        raise ValueError('pipeline must be inference or passthrough')
    cameras = _camera_config(config, mode, camera_ids)
    settings = inspection_settings(config)
    urls = {key: (camera_url(value), camera_url(value, detection=True)) for key, value in cameras.items()}
    directory = Path(output_directory) if output_directory else task_directory(config, task_name, task_run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    started = time.monotonic()
    status = dict(task_run_id=task_run_id, task_name=task_name, mode=mode, pipeline=pipeline, state='starting',
                  processed=0, positive=0, camera_ids=list(cameras), camera_count=len(cameras),
                  output_dir=str(directory), started_at=time.time(), camera_errors={})
    # camera_waiting 与 camera_errors 分开：前者只是"还没收到数据"，后者才是真实故障。
    status.update(camera_waiting={}, camera_total=len(cameras) * 2, camera_ready=0)
    status.update(recording_stream='main', detection_stream='sub', capture_fps=settings['capture_fps'],
                  video_time_basis='host_arrival_estimate', bbox_coordinate_space='substream_pixels')
    output = InspectionOutput(directory, status)
    recorder = None
    readers = {}
    main_readers, sub_readers = {}, {}
    slots = {}
    channels = {key: CameraBroadcaster() for key in cameras}
    runtime = inference_runtime
    own_runtime = runtime is None and pipeline == 'inference'
    counts = dict.fromkeys(cameras, 0)
    next_detection = dict.fromkeys(cameras, 0.0)
    stale_frames = dict.fromkeys(cameras, 0)
    stalled_since = {}
    last_status = 0
    try:
        if pipeline == 'inference' and own_runtime:
            runtime = InferenceRuntime(build_configured_adapters(config))
            runtime.start()
        started = time.monotonic()
        recorder = InspectionRecorder(directory / 'video.mkv', cameras)
        timeline = Timeline()
        slots = {key: InspectionFrameSlot(recorder.channels[key], timeline) for key in cameras}
        if preview:
            preview.bind(task_run_id, channels)
        for key in cameras:
            timeout_us = int(float(cameras[key].get('timeout_seconds', 5)) * 1_000_000)
            main_readers[key] = RtspStreamReader(urls[key][0], None, stop_event=stop,
                recorder=recorder.channels[key], timeout_us=timeout_us)
            sub_readers[key] = RtspStreamReader(urls[key][1], channels[key], stop_event=stop,
                slot=slots[key] if pipeline == 'inference' else None, timeout_us=timeout_us,
                decode_threads=settings['decode_threads'], timeline=timeline)
            slots[key].reader = sub_readers[key]
            readers[f'{key}/main'] = main_readers[key]
            readers[f'{key}/sub'] = sub_readers[key]
            channels[key].bind_timeline(timeline.t0_epoch_ms)
        for reader in [*main_readers.values(), *sub_readers.values()]:
            reader.start()
        timeout = settings['camera_startup_timeout_seconds']
        missing = list(readers)
        while True:
            recorder.check()
            if output.error:
                raise RuntimeError(output.error)
            if stop_event is not None and stop_event.is_set():
                status['state'] = 'stopped'
                break
            if duration_seconds > 0 and time.monotonic() - started >= duration_seconds:
                if missing:
                    raise RuntimeError('任务结束前部分码流未就绪：' + ', '.join(missing))
                status['state'] = 'completed'
                break
            missing = [key for key, reader in readers.items() if not reader.keyframes_rx or
                       (reader.slot is not None and not reader.decoded_frames)]
            if missing and time.monotonic() - started > timeout:
                raise RuntimeError('摄像头码流启动超时：' + ', '.join(missing))
            for key, reader in readers.items():
                if reader.fatal:
                    raise RuntimeError(f'{key}：{reader.fatal}')
                progress = (reader.last_recorded_at if reader.recorder is not None else
                            reader.last_frame_at if reader.slot is not None else reader.last_packet_at)
                if progress and time.monotonic() - progress > settings['stream_stall_timeout_seconds']:
                    raise RuntimeError(f'{key} 持续无有效视频，重连超时')
            if not missing:
                status['state'] = 'running'
            consumed = False
            # Round robin; each camera keeps only its latest decoded frame.
            for key, slot in (slots.items() if not missing else ()):
                if (stop_event is not None and stop_event.is_set()) or (
                        duration_seconds > 0 and time.monotonic() - started >= duration_seconds):
                    break
                if time.monotonic() < next_detection[key]:
                    continue
                item = slot.take(timeout=0)
                if item is None:
                    continue
                consumed = True
                (frame, timing), pts, source_pts = item
                if (time.monotonic() - timeline.monotonic_at(pts) > settings['max_frame_age_seconds'] or
                        timing['detection_session'] != sub_readers[key].sessions):
                    stale_frames[key] += 1
                    continue
                # Rate limit before expensive conversion/inference. Never catch up old work.
                next_detection[key] = time.monotonic() + 1 / settings['capture_fps']
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
                              w=image.shape[1], h=image.shape[0],
                              recording_size=list(main_readers[key].size), **result)
                output.add(record)
                channels[key].publish(dict(t='det', **record))
                status['processed'] += 1
                status['positive'] += bool(result.get('detections'))
            if time.monotonic() - last_status >= .5:
                # camera_errors 只放真实故障（拒绝连接/鉴权失败/路径不存在/断流），
                # "还没收到数据"另放 camera_waiting 交给前端做"连接中"提示 ——
                # 否则每次启动都要刷出一屏与故障同级的黄条，真正有问题的那路反而被淹没。
                now = time.monotonic()
                errors, waiting = {}, {}
                for key, reader in readers.items():
                    reason = reader.last_error or reader.fatal
                    if reason:
                        errors[key] = reason
                        stalled_since.pop(key, None)
                        continue
                    progress = (reader.last_recorded_at if reader.recorder is not None else
                                reader.last_frame_at if reader.slot is not None else reader.last_packet_at)
                    if progress and now - progress <= 2:
                        stalled_since.pop(key, None)
                        continue
                    waiting[key] = round(now - stalled_since.setdefault(key, now), 1)
                status['camera_errors'] = errors
                status['camera_waiting'] = waiting
                status['camera_total'] = len(readers)
                status['camera_ready'] = len(readers) - len(errors) - len(waiting)
                status['recorded_packets'] = recorder.written
                status['camera_stats'] = {key: dict(main=main_readers[key].stats(), sub=sub_readers[key].stats(),
                    processed=counts[key], overwritten_frames=slots[key].dropped,
                    stale_frames=stale_frames[key]) for key in cameras}
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
        join_deadline = time.monotonic() + max((r._io_timeout for r in readers.values()), default=5) + 5
        for reader in readers.values():
            if reader.ident is None:
                continue
            reader.join(timeout=max(0, join_deadline - time.monotonic()))
            if reader.is_alive():
                _cleanup_failed(status, '摄像头拉流线程未能及时退出')
        if preview:
            preview.clear()
        if recorder:
            try:
                recorder.close()
            except Exception as exc:
                _cleanup_failed(status, str(exc))
        if own_runtime and runtime:
            try:
                runtime.stop()
            except Exception as exc:
                _cleanup_failed(status, str(exc))
        status['finished_at'] = time.time()
        status['tracks'] = recorder.track_indices if recorder else {}
        status['recorded_packets'] = recorder.written if recorder else 0
        status['processed_by_camera'] = counts
        # 收尾时拉流线程必然报"流已结束/正在重连"，那是我们自己停的，不该留在终态里。
        status['camera_errors'] = {}
        status['camera_waiting'] = {}
        status['camera_stats'] = {key: dict(main=main_readers[key].stats(), sub=sub_readers[key].stats(),
            processed=counts[key], overwritten_frames=slots[key].dropped,
            stale_frames=stale_frames[key]) for key in sub_readers}
        try:
            output.close(status)
        except Exception as exc:
            _cleanup_failed(status, str(exc))
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
