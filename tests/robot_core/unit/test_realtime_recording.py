import json
import time
from fractions import Fraction
import av
import numpy as np
import pytest
from app.video.inspection_recorder import InspectionRecorder
from app.robot_core.inspection.output import InspectionOutput

pytestmark = pytest.mark.no_database


def source(path, width=64, height=48):
    with av.open(str(path), 'w') as out:
        stream = out.add_stream('libx264', rate=25)
        stream.width, stream.height = width, height
        stream.pix_fmt = 'yuv420p'
        stream.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'g': '5'}
        for i in range(12):
            frame = av.VideoFrame.from_ndarray(np.full((height, width, 3), i * 10, dtype=np.uint8), format='rgb24')
            frame.pts = i
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


def test_single_camera_recording_decodes_and_reconnects(tmp_path):
    path = tmp_path / 'input.mkv'
    source(path)
    recorder = InspectionRecorder(tmp_path / 'cam01' / 'video.mkv', ['cam01'])
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        packets = [packet for packet in container.demux(stream) if packet.size]
        for packet in packets:
            recorder.channels['cam01'].submit(packet, stream)
        recorder.channels['cam01'].finish()
        # A real reconnect occurs after prior source duration on the task clock.
        recorder.started -= 1
        for packet in packets:
            recorder.channels['cam01'].submit(packet, stream)
        recorder.channels['cam01'].finish()
    finally:
        # 输入容器持有编码模板，必须在关容器之前收尾录像。
        recorder.close()
        container.close()
    with av.open(str(tmp_path / 'cam01' / 'video.mkv')) as result:
        assert len(result.streams.video) == 1
        assert result.streams.video[0].metadata['title'] == 'cam01'
        frames = list(result.decode(video=0))
        assert len(frames) == 24
        assert all(b.pts > a.pts for a, b in zip(frames, frames[1:]))


def test_camera_files_are_independent(tmp_path):
    """一路没关键帧只让自己的文件收尾失败，另一路照常出片。"""
    path = tmp_path / 'input.mkv'
    source(path)
    ok = InspectionRecorder(tmp_path / 'cam01' / 'video.mkv', ['cam01'])
    broken = InspectionRecorder(tmp_path / 'cam02' / 'video.mkv', ['cam02'])
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
        for packet in container.demux(stream):
            if packet.size:
                ok.channels['cam01'].submit(packet, stream)
        ok.channels['cam01'].finish()
    finally:
        ok.close()
        with pytest.raises(RuntimeError, match='未收到关键帧'):
            broken.close()
        container.close()
    with av.open(str(tmp_path / 'cam01' / 'video.mkv')) as result:
        assert len(list(result.decode(video=0))) == 12


def test_json_keeps_all_records_and_final_state(tmp_path):
    output = InspectionOutput(tmp_path, {'task_run_id': 'test'}, ['cam01'])
    path = output.paths['cam01']
    assert json.loads(path.read_text())['records'] == []
    for index in range(300):
        output.add(dict(frame_id=index, video_pts=index * 40, camera_id='cam01'))
    output.close({'state': 'completed'})
    value = json.loads(path.read_text())
    assert len(value['records']) == 300
    assert value['metadata']['state'] == 'completed'
    assert value['metadata']['camera_id'] == 'cam01'
    assert sorted(p.name for p in (tmp_path / 'cam01').iterdir()) == ['inspection.json']


def test_records_are_split_per_camera(tmp_path):
    """多路记录各写各的目录，互不混在一起。"""
    output = InspectionOutput(tmp_path, {'task_run_id': 'test'}, ['cam01', 'cam02'])
    for index in range(3):
        output.add(dict(frame_id=index, camera_id='cam01'))
    output.add(dict(frame_id=99, camera_id='cam02'))
    output.close({'state': 'completed'})
    assert len(json.loads(output.paths['cam01'].read_text())['records']) == 3
    assert len(json.loads(output.paths['cam02'].read_text())['records']) == 1
    assert output.counts == {'cam01': 3, 'cam02': 1}
    with pytest.raises(RuntimeError, match='未知的摄像头编号'):
        InspectionOutput(tmp_path / 'other', {}, ['cam01']).add(dict(camera_id='cam09'))


def test_running_snapshot_holds_metadata_only_and_journal_holds_records(tmp_path):
    """运行期快照只写元数据+计数，记录留在 journal：写入量与记录数无关。"""
    output = InspectionOutput(tmp_path, {'task_run_id': 'test'}, ['cam01'], snapshot_interval=.05)
    path = output.paths['cam01']
    for index in range(500):
        output.add(dict(frame_id=index, camera_id='cam01'))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = json.loads(path.read_text())
        if value.get('records_total'):
            break
        time.sleep(.02)
    assert value['records'] == []                 # 快照不含记录
    assert value['records_complete'] is False
    assert value['records_total'] > 0
    assert (tmp_path / 'cam01' / '.inspection.records').exists()
    assert path.stat().st_size < 2048             # 体积恒定，不随记录增长

    output.close({'state': 'completed'})
    value = json.loads(path.read_text())
    assert value['records_complete'] is True
    assert len(value['records']) == 500
    assert sorted(p.name for p in (tmp_path / 'cam01').iterdir()) == ['inspection.json']


def test_missing_camera_fails_recording(tmp_path):
    path = tmp_path / 'input.mkv'
    source(path)
    recorder = InspectionRecorder(tmp_path / 'video.mkv', ['cam01', 'cam02'])
    with av.open(str(path)) as container:
        recorder.channels['cam01'].submit(next(container.demux(video=0)), container.streams.video[0])
        recorder.channels['cam01'].finish()
        with pytest.raises(RuntimeError, match='未收到关键帧'):
            recorder.close()


def test_cross_stream_clock_mapping_and_gap(monkeypatch):
    from types import SimpleNamespace
    from app.video import inspection_recorder as module
    clock = [100.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    class Owner:
        started = 99.0
        track_indices = {'cam01': 3}
        def offer(self, item):
            if item[0] == 'barrier':
                item[1].set()
        def check(self):
            pass
    channel = module.RecordingChannel(Owner(), 'cam01')
    packet = av.Packet(b'data')
    packet.pts, packet.dts = 500000, 499960
    packet.time_base = Fraction(1, 1000)
    packet.is_keyframe = True
    template = SimpleNamespace(time_base=packet.time_base)
    assert channel.arrival_timestamp(100)['video_pts'] is None
    assert channel.submit(packet, template)
    snapshot = channel.arrival_timestamp(100.02)
    assert snapshot['video_pts'] == 1060  # 1s task offset + 40ms B-frame delta + 20ms arrival
    assert snapshot['session'] == 1
    assert snapshot['track_index'] == 3
    assert channel.arrival_timestamp(102)['video_pts'] is None
    channel.finish()
    assert channel.arrival_timestamp(100.02)['video_pts'] is None
    clock[0] = 103
    packet.pts, packet.dts = 7000000, 7000000
    channel.submit(packet, template)
    assert channel.arrival_timestamp(103.02)['video_pts'] == 4020
    assert channel.arrival_timestamp(103.02)['session'] == 2
    assert snapshot['video_pts'] == 1060  # old captured detection must not be reinterpreted
