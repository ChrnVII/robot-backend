"""Production demux/decoder/engine with paced encoded files replacing RTSP I/O."""
import json
import threading
import time
from types import SimpleNamespace
import av
import pytest
from app.robot_core.common.config import PlatformConfig
from app.robot_core.processes import inspection_engine as engine
from app.video.reader import RtspStreamReader
from tests.robot_core.unit.test_realtime_recording import source

pytestmark = pytest.mark.no_database


class Runtime:
    def __init__(self, delay=0):
        self.calls = []
        self.delay = delay

    def predict(self, request):
        assert request.context['image'].shape == (48, 64, 3)
        self.calls.append((request.asset_id, time.monotonic()))
        time.sleep(self.delay)
        return SimpleNamespace(status='ok', result={'detections': [{'label': 'heat', 'bbox_xyxy': [1, 1, 10, 10]}]},
                               model_id='test', model_version='1', model_sha256='abc', latency_ms=1)


def setup_streams(tmp_path, monkeypatch, *, missing=None, reconnect=None, dead=()):
    main, sub = tmp_path / 'main.mkv', tmp_path / 'sub.mkv'
    source(main, width=128, height=96)
    source(sub)
    instances = []

    class RecordPacket:
        def __init__(self, packet):
            self.packet = packet
        def __getattr__(self, key):
            return getattr(self.packet, key)
        def __bytes__(self):
            return bytes(self.packet)
        def decode(self):
            raise AssertionError('main stream must never decode')

    class FileReader(RtspStreamReader):
        def __init__(self, *args, **kwargs):
            kwargs['reconnect_delay'] = .2
            super().__init__(*args, **kwargs)
            instances.append(self)

        def _session(self, av_module):
            reader = self
            is_main = 'stream=0.sdp' in self.url
            if any(host in self.url for host in dead):
                # 摄像头能连上但一个包都不给：等停止信号，模拟「装死」的那一路。
                self._stop_event.wait(30)
                raise RuntimeError('simulated silent stream')
            if (missing == 'main' and is_main) or (missing == 'sub' and not is_main):
                raise RuntimeError('simulated unavailable stream')

            class Input:
                def __enter__(self):
                    self.container = av.open(str(main if is_main else sub))
                    self.streams = self.container.streams
                    return self
                def __exit__(self, *args):
                    self.container.close()
                def demux(self, stream):
                    if not is_main and reader.slot is not None:
                        assert stream.codec_context.thread_count == 1
                    for index, packet in enumerate(self.container.demux(stream)):
                        if not packet.size:
                            continue
                        if not is_main:
                            # Substream has a totally different RTP/PTS origin.
                            packet.pts += 90000
                            packet.dts += 90000
                        yield RecordPacket(packet) if is_main else packet
                        if reader._stop_event.wait(.04):
                            return
                        if reader.sessions == 1 and index == 5 and reconnect == ('main' if is_main else 'sub'):
                            return
                    reader._stop_event.wait(5)
            super()._session(SimpleNamespace(open=lambda *a, **k: Input(), codec=av_module.codec))

    monkeypatch.setattr(engine, 'RtspStreamReader', FileReader)
    return instances


def config_for(tmp_path, count=1, **settings):
    return PlatformConfig(tmp_path / 'config.yaml', {'inspection': settings, 'devices': {'cameras': {'items': [
        {'id': f'cam{i:02}', 'transport': 'rtsp', 'host': f'192.168.136.{10+i}'} for i in range(1, count+1)]}}})


@pytest.mark.parametrize('pipeline', ['inference', 'passthrough'])
def test_engine_records_and_finalizes(tmp_path, monkeypatch, pipeline):
    readers = setup_streams(tmp_path, monkeypatch)
    runtime = Runtime()
    result = engine.run(config_for(tmp_path), mode='single', task_run_id='test', duration_seconds=.8,
                        output_directory=tmp_path / 'result', pipeline=pipeline, inference_runtime=runtime)
    assert result['state'] == 'completed', result
    value = json.loads((tmp_path / 'result' / 'cam01' / 'inspection.json').read_text())
    records = value['records']
    if pipeline == 'inference':
        assert 1 <= len(records) <= 2
        assert any(r['video_pts'] is not None for r in records)
        for r in records:
            assert r['src_pts'] >= 90000
            assert r['video_pts'] is None or 0 <= r['video_pts'] < 1500
            assert (r['w'], r['h']) == (64, 48)
            assert r['recording_size'] == [128, 96]
            assert r['video_time_basis'] == 'host_arrival_estimate'
        assert all(b[1] - a[1] >= .49 for a, b in zip(runtime.calls, runtime.calls[1:]))
    else:
        assert records == []
        assert all(r.decoded_frames == 0 for r in readers)
    assert bool(runtime.calls) == (pipeline == 'inference')
    assert all(not r.is_alive() for r in readers)
    with av.open(str(tmp_path / 'result' / 'cam01' / 'video.mkv')) as recorded:
        frames = list(recorded.decode(video=0))
        assert len(frames) == 12
        assert (frames[0].width, frames[0].height) == (128, 96)
    assert result['recorded_packets'] == 12


def test_slow_cpu_keeps_all_eight_main_tracks(tmp_path, monkeypatch):
    readers = setup_streams(tmp_path, monkeypatch)
    runtime = Runtime(delay=.11)
    result = engine.run(config_for(tmp_path, 8), mode='eight', task_run_id='test', duration_seconds=1.4,
                        output_directory=tmp_path / 'result', inference_runtime=runtime)
    assert result['state'] == 'completed', result
    assert all(count > 0 for count in result['processed_by_camera'].values())
    assert result['recorded_packets'] == 96
    assert all(not r.is_alive() for r in readers)
    for index in range(1, 9):
        with av.open(str(tmp_path / 'result' / f'cam{index:02}' / 'video.mkv')) as recorded:
            assert len(recorded.streams.video) == 1
            assert len(list(recorded.decode(video=0))) == 12
    assert sum(s['overwritten_frames'] for s in result['camera_stats'].values()) > 0


@pytest.mark.parametrize('role', ['main', 'sub'])
def test_independent_reconnect_and_detection_timeline(tmp_path, monkeypatch, role):
    readers = setup_streams(tmp_path, monkeypatch, reconnect=role)
    result = engine.run(config_for(tmp_path, capture_fps=20), mode='single', task_run_id='test',
                        duration_seconds=1.2, output_directory=tmp_path / 'result', inference_runtime=Runtime())
    assert result['state'] == 'completed', result
    stats = result['camera_stats']['cam01']
    assert stats[role]['sessions'] == 2
    assert stats['sub' if role == 'main' else 'main']['sessions'] == 1
    assert all(not r.is_alive() for r in readers)
    records = json.loads((tmp_path / 'result' / 'cam01' / 'inspection.json').read_text())['records']
    assert all(b['pts'] > a['pts'] for a, b in zip(records, records[1:]))
    if role == 'main':
        assert any(r['video_pts'] is None for r in records)
        assert any(r['session'] == 2 for r in records)
    else:
        assert any(r['detection_session'] == 2 for r in records)
    with av.open(str(tmp_path / 'result' / 'cam01' / 'video.mkv')) as recorded:
        frames = list(recorded.decode(video=0))
        assert len(frames) == (18 if role == 'main' else 12)
        assert all(b.pts > a.pts for a, b in zip(frames, frames[1:]))


@pytest.mark.parametrize('missing', ['main', 'sub'])
def test_missing_stream_fails_with_role_and_closes(tmp_path, monkeypatch, missing):
    readers = setup_streams(tmp_path, monkeypatch, missing=missing)
    result = engine.run(config_for(tmp_path, camera_startup_timeout_seconds=.12), mode='single',
                        task_run_id='test', duration_seconds=0, output_directory=tmp_path / 'result',
                        inference_runtime=Runtime())
    assert result['state'] == 'failed'
    # Preserve startup error even if the incomplete recorder also fails to close.
    assert missing in result['error']
    assert all(not r.is_alive() for r in readers)


def test_stalled_substream_fails_even_with_open_socket(tmp_path, monkeypatch):
    setup_streams(tmp_path, monkeypatch)
    # 单路任务：唯一一路断流等于「全部路都掉线」，宽限期设 0 即当场失败。
    # 多路任务下这路只会被摘掉，其余各路继续跑（见 test_one_failed_camera_does_not_kill_task）。
    result = engine.run(config_for(tmp_path, stream_stall_timeout_seconds=.15,
                                   camera_all_failed_grace_seconds=0), mode='single',
                        task_run_id='test', duration_seconds=2, output_directory=tmp_path / 'result',
                        inference_runtime=Runtime())
    assert result['state'] == 'failed'
    assert '重连超时' in result['error']


def test_one_silent_camera_does_not_kill_the_task(tmp_path, monkeypatch):
    """单路故障隔离：一路装死只摘掉这一路，其余各路照常检测、照常录像。"""
    readers = setup_streams(tmp_path, monkeypatch, dead=('192.168.136.13',))   # cam03
    result = engine.run(config_for(tmp_path, 4, camera_startup_timeout_seconds=.6,
                                   camera_all_failed_grace_seconds=0), mode='four',
                        task_run_id='test', duration_seconds=2.0,
                        output_directory=tmp_path / 'result', inference_runtime=Runtime())
    assert result['state'] == 'completed', result
    assert 'cam03' in result['camera_disabled']
    assert result['processed_by_camera']['cam03'] == 0
    assert all(result['processed_by_camera'][f'cam0{index}'] > 0 for index in (1, 2, 4))
    # 录像按剩下的三路收尾，而不是整场作废
    assert set(result['camera_files']) == {'cam01', 'cam02', 'cam03', 'cam04'}
    assert [result['camera_files'][key]['packets'] for key in ('cam01', 'cam02', 'cam04')].count(0) == 0
    assert result['camera_files']['cam03']['packets'] == 0
    assert all(not reader.is_alive() for reader in readers)


def test_stop_between_predictions(tmp_path, monkeypatch):
    readers = setup_streams(tmp_path, monkeypatch)
    stop = threading.Event()
    runtime = Runtime()
    predict = runtime.predict
    def stop_after_first(request):
        response = predict(request)
        stop.set()
        return response
    runtime.predict = stop_after_first
    result = engine.run(config_for(tmp_path, 8), mode='eight', task_run_id='test', duration_seconds=0,
                        stop_event=stop, output_directory=tmp_path / 'result', inference_runtime=runtime)
    assert result['state'] == 'stopped', result
    assert len(runtime.calls) == 1
    assert all(not r.is_alive() for r in readers)


def test_rtsp_urls_and_explicit_vendor_path():
    values = dict(transport='rtsp', host='192.168.136.11', username='admin', password='admin123')
    assert engine.camera_url(values) == 'rtsp://admin:admin123@192.168.136.11:554/avstream/channel=1/stream=0.sdp'
    assert engine.camera_url(values, detection=True).endswith('/stream=1.sdp')
    values.update(path='/Streaming/Channels/101', detection_path='/Streaming/Channels/102')
    assert engine.camera_url(values, detection=True).endswith('/Streaming/Channels/102')
    values.pop('detection_path')
    with pytest.raises(ValueError, match='detection_path'):
        engine.camera_url(values, detection=True)


@pytest.mark.parametrize('key,value', [('capture_fps', 0), ('capture_fps', float('nan')),
    ('capture_fps', float('inf')), ('decode_threads', 0), ('decode_threads', 1.5),
    ('max_frame_age_seconds', -1), ('stream_stall_timeout_seconds', 0),
    ('inference_batch_size', 0), ('inference_batch_size', 9),
    ('camera_all_failed_grace_seconds', -1), ('camera_all_failed_grace_seconds', 4000)])
def test_invalid_cpu_settings(tmp_path, key, value):
    with pytest.raises(ValueError, match=key):
        engine.inspection_settings(config_for(tmp_path, **{key: value}))


class BatchRuntime(Runtime):
    """支持批量推理的假运行时：记录每次批量里的帧数。"""

    def __init__(self, delay=0):
        super().__init__(delay)
        self.batches = []

    def predict_batch(self, requests):
        self.batches.append(len(requests))
        return [self.predict(request) for request in requests]


def test_batches_frames_of_multiple_cameras(tmp_path, monkeypatch):
    setup_streams(tmp_path, monkeypatch)
    runtime = BatchRuntime()
    result = engine.run(config_for(tmp_path, 4, inference_batch_size=4), mode='four', task_run_id='test',
                        duration_seconds=1.6, output_directory=tmp_path / 'result',
                        inference_runtime=runtime)
    assert result['state'] == 'completed', result
    # 多路到期帧合并成一次模型调用（CPU 下这是最大的吞吐收益来源）
    assert runtime.batches and max(runtime.batches) >= 2
    assert all(count > 0 for count in result['processed_by_camera'].values())


class BatchRuntime(Runtime):
    """支持批量推理的假运行时：记录每次批量里的帧数。"""

    def __init__(self, delay=0):
        super().__init__(delay)
        self.batches = []

    def predict_batch(self, requests):
        self.batches.append(len(requests))
        return [self.predict(request) for request in requests]


def test_batches_frames_of_multiple_cameras(tmp_path, monkeypatch):
    setup_streams(tmp_path, monkeypatch)
    runtime = BatchRuntime()
    result = engine.run(config_for(tmp_path, 4, inference_batch_size=4), mode='four', task_run_id='test',
                        duration_seconds=1.6, output_directory=tmp_path / 'result',
                        inference_runtime=runtime)
    assert result['state'] == 'completed', result
    # 多路到期帧合并成一次模型调用（CPU 下这是最大的吞吐收益来源）
    assert runtime.batches and max(runtime.batches) >= 2
    assert all(count > 0 for count in result['processed_by_camera'].values())
