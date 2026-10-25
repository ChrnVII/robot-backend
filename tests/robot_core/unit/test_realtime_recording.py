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


def test_eight_tracks_decode_and_reconnect(tmp_path):
    path = tmp_path / 'input.mkv'
    source(path)
    ids = [f'cam{i:02}' for i in range(1, 9)]
    recorder = InspectionRecorder(tmp_path / 'video.mkv', ids)
    containers = [av.open(str(path)) for _ in ids]
    try:
        packets = [list(c.demux(video=0)) for c in containers]
        for index in range(12):
            for key, container, items in zip(ids, containers, packets):
                recorder.channels[key].submit(items[index], container.streams.video[0])
        for channel in recorder.channels.values():
            channel.finish()
        # A real reconnect occurs after prior source duration on the task clock.
        recorder.started -= 1
        for index in range(12):
            for key, container, items in zip(ids, containers, packets):
                recorder.channels[key].submit(items[index], container.streams.video[0])
        for channel in recorder.channels.values():
            channel.finish()
    finally:
        recorder.close()
        for container in containers:
            container.close()
    with av.open(str(tmp_path / 'video.mkv')) as result:
        assert len(result.streams.video) == 8
        assert {s.metadata['title'] for s in result.streams.video} == set(ids)
    for index in range(8):
        with av.open(str(tmp_path / 'video.mkv')) as result:
            frames = list(result.decode(video=index))
            assert len(frames) == 24
            assert all(b.pts > a.pts for a, b in zip(frames, frames[1:]))


def test_json_keeps_all_records_and_final_state(tmp_path):
    output = InspectionOutput(tmp_path, {'task_run_id': 'test'})
    assert json.loads(output.path.read_text())['records'] == []
    for index in range(300):
        output.add(dict(frame_id=index, video_pts=index * 40, camera_id='cam01'))
    output.close({'state': 'completed'})
    value = json.loads(output.path.read_text())
    assert len(value['records']) == 300
    assert value['metadata']['state'] == 'completed'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['inspection.json']


def test_running_snapshot_holds_metadata_only_and_journal_holds_records(tmp_path):
    """运行期快照只写元数据+计数，记录留在 journal：写入量与记录数无关。"""
    output = InspectionOutput(tmp_path, {'task_run_id': 'test'}, snapshot_interval=.05)
    for index in range(500):
        output.add(dict(frame_id=index))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = json.loads(output.path.read_text())
        if value.get('records_total'):
            break
        time.sleep(.02)
    assert value['records'] == []                 # 快照不含记录
    assert value['records_complete'] is False
    assert value['records_total'] > 0
    assert (tmp_path / '.inspection.records').exists()
    assert (tmp_path / 'inspection.json').stat().st_size < 2048   # 体积恒定，不随记录增长

    output.close({'state': 'completed'})
    value = json.loads(output.path.read_text())
    assert value['records_complete'] is True
    assert len(value['records']) == 500
    assert sorted(p.name for p in tmp_path.iterdir()) == ['inspection.json']


def test_missing_camera_fails_recording(tmp_path):
    path = tmp_path / 'input.mkv'
    source(path)
    recorder = InspectionRecorder(tmp_path / 'video.mkv', ['cam01', 'cam02'])
    with av.open(str(path)) as container:
        recorder.channels['cam01'].submit(next(container.demux(video=0)), container.streams.video[0])
        recorder.channels['cam01'].finish()
        with pytest.raises(RuntimeError, match='未收到全部'):
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
