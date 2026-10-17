import json
import time
from fractions import Fraction
import av
import numpy as np
import pytest
from app.video.inspection_recorder import InspectionRecorder
from app.robot_core.inspection.output import InspectionOutput

pytestmark = pytest.mark.no_database


def source(path):
    with av.open(str(path), 'w') as out:
        stream = out.add_stream('libx264', rate=25)
        stream.width, stream.height = 64, 48
        stream.pix_fmt = 'yuv420p'
        stream.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'g': '5'}
        for i in range(12):
            frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 10, dtype=np.uint8), format='rgb24')
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


def test_missing_camera_fails_recording(tmp_path):
    path = tmp_path / 'input.mkv'
    source(path)
    recorder = InspectionRecorder(tmp_path / 'video.mkv', ['cam01', 'cam02'])
    with av.open(str(path)) as container:
        recorder.channels['cam01'].submit(next(container.demux(video=0)), container.streams.video[0])
        recorder.channels['cam01'].finish()
        with pytest.raises(RuntimeError, match='未收到全部'):
            recorder.close()
