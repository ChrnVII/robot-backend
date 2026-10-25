"""Main-stream remux recording; sub-stream preview and sampled CPU inference."""
from __future__ import annotations
import argparse
import logging
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

logger = logging.getLogger(__name__)

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
    # 全部路都掉线后的宽限期：0 表示立刻失败（单路任务等于「唯一一路挂了」）。
    grace = float(settings.get('camera_all_failed_grace_seconds', 60))
    if not math.isfinite(grace) or not 0 <= grace <= 3600:
        raise ValueError('inspection.camera_all_failed_grace_seconds must be >= 0 and <= 3600')
    result['camera_all_failed_grace_seconds'] = grace
    threads = settings.get('decode_threads', 1)
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 8:
        raise ValueError('inspection.decode_threads must be an integer between 1 and 8')
    result['decode_threads'] = threads
    batch = settings.get('inference_batch_size', 4)
    if isinstance(batch, bool) or not isinstance(batch, int) or not 1 <= batch <= 8:
        raise ValueError('inspection.inference_batch_size must be an integer between 1 and 8')
    result['inference_batch_size'] = batch
    return result


def _cleanup_failed(status, message):
    status['state'] = 'failed'
    if status.get('error'):
        status.setdefault('cleanup_errors', []).append(message)
    else:
        status['error'] = message


def _reader_progress(reader):
    """该 reader 的「有效进展」时刻：录像看写入、解码看帧、其余看收包。"""
    return (reader.last_recorded_at if reader.recorder is not None else
            reader.last_frame_at if reader.slot is not None else reader.last_packet_at)


def _reader_alive(reader, now, window=2.0):
    """是否还在收数据（用于故障恢复判定，比「有效进展」宽松）。"""
    latest = max(reader.last_packet_at, reader.last_frame_at, reader.last_recorded_at)
    return bool(latest) and now - latest <= window


def _camera_recovered(main, sub, now, window=2.0):
    """恢复判定要看到真的出帧：只收到包、解不出帧的流仍然不能用于检测。"""
    if not (main.keyframes_rx and sub.keyframes_rx):
        return False
    if sub.slot is not None:
        if not sub.decoded_frames or not sub.last_frame_at or now - sub.last_frame_at > window:
            return False
    return _reader_alive(main, now, window)


def _camera_ready(main, sub):
    """主、子码流都解出关键帧（子码流还要出帧）才算这一路可用。"""
    if not main.keyframes_rx or not sub.keyframes_rx:
        return False
    return not (sub.slot is not None and not sub.decoded_frames)


def _missing_roles(main, sub):
    roles = []
    if not main.keyframes_rx:
        roles.append('main')
    if not sub.keyframes_rx or (sub.slot is not None and not sub.decoded_frames):
        roles.append('sub')
    return roles


def _predict(runtime, requests, stop_event):
    """批量推理；运行时没有批量能力（含测试替身）时退化为逐帧，且可被停止打断。"""
    batch_predict = getattr(runtime, 'predict_batch', None)
    if callable(batch_predict):
        return list(batch_predict(requests))
    responses = []
    for request in requests:
        if stop_event is not None and stop_event.is_set():
            break
        responses.append(runtime.predict(request))
    return responses


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
    status.update(camera_waiting={}, camera_disabled={}, camera_total=len(cameras) * 2, camera_ready=0)
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
    inference_failures = dict.fromkeys(cameras, 0)
    stalled_since = {}
    # 单路故障隔离：disabled 里的相机只停自己的推理/录像，其余各照常跑。
    disabled: dict[str, str] = {}
    ready_cameras: set[str] = set()
    recovered_since: dict[str, float] = {}
    all_down_since = None
    last_status = 0
    batch_size = settings['inference_batch_size']
    startup_deadline = 0.0
    stall_timeout = settings['stream_stall_timeout_seconds']
    # 恢复判定所需的连续健康时长：抖动的流不要反复启停。
    recovery_dwell = max(2.0, min(stall_timeout, 10.0))

    def disable_camera(key, reason):
        """把一路从推理/预览/录像里摘掉，其余各路继续跑。

        MKV 的轨道必须在开始编码前建好，所以这一路也被永久剔出本场录像；
        之后即使码流恢复，也只恢复检测与预览（见循环里的恢复分支）。
        """
        if key in disabled:
            return
        disabled[key] = reason
        next_detection[key] = 0.0
        if recorder is not None:
            recorder.drop_camera(key)
        logger.warning('摄像头已停用（%s）：%s', key, reason)

    def progress_of(key, reader):
        """该 reader 的有效进展。

        被剔除出录像的主码流只收包不入库，``last_recorded_at`` 会停住，这时改用
        收包时刻判断，避免恢复后的相机立刻又被判成断流。
        """
        if reader.recorder is not None and recorder is not None and recorder.is_blocked(key):
            return reader.last_packet_at
        return _reader_progress(reader)

    def camera_stats():
        """每路诊断指标（前端「每路指标」面板直接消费）。"""
        stats = {}
        for key in cameras:
            main, sub, slot = main_readers.get(key), sub_readers.get(key), slots.get(key)
            stats[key] = dict(
                main=main.stats() if main is not None else {},
                sub=sub.stats() if sub is not None else {},
                processed=counts[key],
                overwritten_frames=slot.dropped if slot is not None else 0,
                stale_frames=stale_frames[key],
                state=('down' if key in disabled else 'live' if key in ready_cameras else 'waiting'),
                error=disabled.get(key, ''),
            )
        return stats

    try:
        if pipeline == 'inference' and own_runtime:
            runtime = InferenceRuntime(build_configured_adapters(config))
            runtime.start()
        started = time.monotonic()
        startup_deadline = started + settings['camera_startup_timeout_seconds']
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

        while True:
            recorder.check()
            if output.error:
                raise RuntimeError(output.error)
            if stop_event is not None and stop_event.is_set():
                status['state'] = 'stopped'
                break
            now = time.monotonic()
            if duration_seconds > 0 and now - started >= duration_seconds:
                # 到期按正常完成收尾；掉线的路已记在 camera_disabled 里。
                status['state'] = 'completed'
                break

            # ---------- 每路健康判定：就绪 / 恢复 / 掉线 ---------- #
            for key in cameras:
                main, sub = main_readers[key], sub_readers[key]
                if key in disabled:
                    # 拉流线程会一直重连；连续健康一小段时间后才允许回到检测。
                    # 要求「持续」是为了避免抖动流反复启用/停用刷日志。
                    if _camera_recovered(main, sub, now):
                        since = recovered_since.setdefault(key, now)
                        if now - since >= recovery_dwell:
                            disabled.pop(key, None)
                            recovered_since.pop(key, None)
                            next_detection[key] = 0.0
                            inference_failures[key] = 0
                            status['camera_recoveries'] = int(status.get('camera_recoveries', 0)) + 1
                            logger.info('摄像头已恢复（%s），重新参与检测', key)
                    else:
                        recovered_since.pop(key, None)
                    continue
                if main.fatal or sub.fatal:
                    disable_camera(key, f'拉流不可用（{main.fatal or sub.fatal}）')
                    continue
                if key not in ready_cameras:
                    if _camera_ready(main, sub):
                        ready_cameras.add(key)
                        continue
                    if now > startup_deadline:
                        roles = '/'.join(_missing_roles(main, sub)) or 'main/sub'
                        disable_camera(key, f'启动超时（{roles} 无数据）')
                    continue
                stalled = [role for role, reader in (('main', main), ('sub', sub))
                           if (progress := progress_of(key, reader)) and now - progress > stall_timeout]
                if stalled:
                    disable_camera(key, f'{"/".join(stalled)} 持续无有效视频，重连超时')

            active = [key for key in cameras if key not in disabled]
            # ---------- 全部路都掉了才算失败（且给在线任务一个宽限期） ---------- #
            if not active:
                summary = '；'.join(f'{key} {reason}' for key, reason in disabled.items())
                if not ready_cameras:
                    raise RuntimeError('所有摄像头均不可用：' + summary)
                if all_down_since is None:
                    all_down_since = now
                if now - all_down_since >= settings['camera_all_failed_grace_seconds']:
                    raise RuntimeError(f'所有摄像头均不可用（已持续 {now - all_down_since:.0f}s）：{summary}')
            else:
                all_down_since = None
                if status['state'] in {'starting', 'running'}:
                    status['state'] = 'running' if all(key in ready_cameras for key in active) else 'starting'

            consumed = False
            # ---------- 攒一批到期相机的最新帧，一次模型调用 ---------- #
            due = [key for key in active if now >= next_detection[key]]
            batch = []
            for key in due[:batch_size]:
                if (stop_event is not None and stop_event.is_set()) or (
                        duration_seconds > 0 and time.monotonic() - started >= duration_seconds):
                    break
                item = slots[key].take(timeout=0)
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
                batch.append((key, image, timing, pts))

            if batch:
                requests = [InferenceRequest(capability='heat_stress_detection',
                                             asset_id=f'{key}-{counts[key]}', task_run_id=task_run_id,
                                             context={'image': image})
                            for key, image, _, _ in batch] if pipeline == 'inference' else []
                responses = _predict(runtime, requests, stop_event) if requests else []
                for index, (key, image, timing, pts) in enumerate(batch):
                    result = {}
                    if requests:
                        response = responses[index] if index < len(responses) else None
                        if response is None:      # 停止打断时留下的空位
                            continue
                        if response.status != 'ok':
                            # 单次推理失败不摘相机（瞬抖很常见），连续失败才摘。
                            inference_failures[key] += 1
                            if inference_failures[key] >= 3:
                                disable_camera(key, f'推理失败：{response.status}')
                            continue
                        inference_failures[key] = 0
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
                stamp = time.monotonic()
                errors, waiting = {}, {}
                for key, reader in readers.items():
                    if key.split('/')[0] in disabled:
                        continue          # 已停用的路单独放在 camera_disabled
                    reason = reader.last_error or reader.fatal
                    if reason:
                        errors[key] = reason
                        stalled_since.pop(key, None)
                        continue
                    progress = progress_of(key, reader)
                    if progress and stamp - progress <= 2:
                        stalled_since.pop(key, None)
                        continue
                    waiting[key] = round(stamp - stalled_since.setdefault(key, stamp), 1)
                status['camera_errors'] = errors
                status['camera_waiting'] = waiting
                status['camera_disabled'] = dict(disabled)
                status['camera_total'] = len(readers)
                status['camera_ready'] = sum(1 for key in readers
                                             if key.split('/')[0] not in disabled
                                             and key not in errors and key not in waiting)
                status['recorded_packets'] = recorder.written
                status['records_total'] = output.count
                status['camera_stats'] = camera_stats()
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
                # 有超时的收尾：磁盘挂起时宁可报清理错误，也不能永远卡在 stopping。
                recorder.close(timeout=15)
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
        # camera_disabled 保留：那是本次任务里真实被摘掉的相机，排障要用。
        status['camera_disabled'] = dict(disabled)
        status['camera_stats'] = camera_stats()
        status['records_total'] = output.count
        try:
            output.close(status, timeout=15)
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
