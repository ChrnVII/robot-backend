"""Recording regression tests using real PyAV packets, without camera/database."""
import threading
from fractions import Fraction
from types import SimpleNamespace

import pytest

from app.common.exceptions import AppError
from app.video.recorder import CameraRecorder

pytestmark = pytest.mark.no_database


@pytest.fixture
def encoded_video(tmp_path):
    av = pytest.importorskip('av')
    source = tmp_path / 'source.mkv'
    with av.open(str(source), 'w') as container:
        stream = container.add_stream('libx264', rate=25)
        stream.width, stream.height = 64, 48
        stream.pix_fmt = 'yuv420p'
        stream.options = {'g': '5', 'bf': '0'}
        for index in range(15):
            frame = av.VideoFrame(64, 48, 'yuv420p')
            for plane in frame.planes:
                plane.update(bytes([index * 10]) * plane.buffer_size)
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return source


@pytest.mark.parametrize("skip_first_packet, expected_frames", [(False, 15), (True, 10)])
def test_real_remux_and_reconnect_segments(tmp_path, encoded_video, skip_first_packet, expected_frames):
    import av
    recorder = CameraRecorder(str(tmp_path / 'recordings'), lambda _: None)
    recorder.start_worker()
    try:
        recorder.start()
        # Simulate RTSP sessions: original input is kept alive until drain finishes.
        for _ in range(2):
            with av.open(str(encoded_video)) as container:
                stream = container.streams.video[0]
                packets = container.demux(stream)
                if skip_first_packet:
                    next(packets)  # start between keyframes: wait until the next GOP
                for packet in packets:
                    if packet.size:
                        original = (packet.stream, packet.pts, packet.dts, packet.time_base)
                        recorder.submit(packet, stream)
                        assert original == (packet.stream, packet.pts, packet.dts, packet.time_base)
                recorder.finish()
        state = recorder.finish(stop=True)
        assert not state['recording'] and not state['error']
        assert state['segments'] == 2 and state['written'] == 2 * expected_frames
        for row in recorder.files():
            assert not row['active']
            with av.open(str(recorder.file(row['name']))) as result:
                frames = list(result.decode(video=0))
                assert len(frames) == expected_frames
                assert frames[0].width == 64
                times = [float(frame.pts * frame.time_base) for frame in frames]
                assert times[-1] - times[0] == pytest.approx((expected_frames - 1) / 25)
    finally:
        recorder.finish(stop=True, shutdown=True)


def test_stop_before_keyframe_creates_no_file(tmp_path):
    recorder = CameraRecorder(str(tmp_path), lambda _: None)
    recorder.start_worker()
    try:
        recorder.start()
        assert recorder.finish(stop=True)['currentFile'] is None
        assert recorder.files() == []
    finally:
        recorder.finish(stop=True, shutdown=True)


def test_file_rejects_traversal_and_active_recording(tmp_path):
    recorder = CameraRecorder(str(tmp_path), lambda _: None)
    path = tmp_path / 'clip.mkv'
    path.write_bytes(b'test')
    assert recorder.file('clip.mkv') == path
    with pytest.raises(AppError) as error:
        recorder.file('../clip.mkv')
    assert error.value.status_code == 404
    recorder.current_file = path.name
    with pytest.raises(AppError) as error:
        recorder.file(path.name)
    assert error.value.status_code == 409


def test_queue_overflow_stops_instead_of_dropping_reference_frames(tmp_path):
    recorder = CameraRecorder(str(tmp_path), lambda _: None, max_queue=1)
    recorder.recording = True
    assert recorder._slots.acquire(blocking=False)
    recorder.submit(None, None)  # overflow is checked before touching the input packet
    assert not recorder.status()['recording']
    assert '积压' in recorder.status()['error']


def test_write_failure_reported_and_start_can_recover(tmp_path, encoded_video, monkeypatch):
    import av
    recorder = CameraRecorder(str(tmp_path / 'recordings'), lambda _: None)
    recorder.start_worker()
    original_write = recorder._write
    failed = threading.Event()
    def fail(_):
        failed.set()
        raise OSError('disk full')
    try:
        monkeypatch.setattr(recorder, '_write', fail)
        recorder.start()
        with av.open(str(encoded_video)) as container:
            stream = container.streams.video[0]
            recorder.submit(next(container.demux(stream)), stream)
            assert failed.wait(2)
            recorder.finish()
        assert recorder.status()['error'] == 'disk full'
        assert not recorder.status()['recording']
        monkeypatch.setattr(recorder, '_write', original_write)
        recorder.start()
        with av.open(str(encoded_video)) as container:
            stream = container.streams.video[0]
            for packet in container.demux(stream):
                if packet.size:
                    recorder.submit(packet, stream)
            recorder.finish(stop=True)
        assert not recorder.status()['error']
        assert recorder.status()['written'] == 15
    finally:
        recorder.finish(stop=True, shutdown=True)

def test_recording_http_contract_and_validation(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import system
    from app.common.exceptions import register_exception_handlers

    recorder = CameraRecorder(str(tmp_path), lambda _: None)
    service = SimpleNamespace(recorder=recorder, started=False, status=lambda: {'connected': False})
    monkeypatch.setattr(system, 'get_camera_stream_service', lambda: service)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(system.router, prefix='/api/v1')
    recorder.start_worker()
    try:
        with TestClient(app) as client:
            assert client.post('/api/v1/cam/recordings', json={'action': 'invalid'}).status_code == 400
            assert client.post('/api/v1/cam/recordings', json={'action': 'start'}).status_code == 409
            service.started = True
            service.status = lambda: {'connected': True}
            assert client.post('/api/v1/cam/recordings', json={'action': 'start'}).json()['data']['recording']
            assert not client.post('/api/v1/cam/recordings', json={'action': 'stop'}).json()['data']['recording']
            (tmp_path / 'test.mkv').write_bytes(b'recorded-content')
            payload = client.get('/api/v1/cam/recordings').json()['data']
            # 列表与目录一起返回：前端「打开文件夹」直接用这个绝对路径，不再询问用户
            assert payload['rows'][0]['name'] == 'test.mkv'
            assert payload['dir'] == str(tmp_path.resolve())

            # 打开目录只在服务端执行；测试里替换掉实际拉起资源管理器的那一步
            monkeypatch.setattr(system, '_reveal_in_file_manager', lambda path: (True, ''))
            opened = client.post('/api/v1/cam/recordings/open').json()['data']
            assert opened['opened'] is True
            assert opened['dir'] == str(tmp_path.resolve())

            # 无桌面会话时不算失败：返回 opened=False，前端会退回复制路径
            monkeypatch.setattr(system, '_reveal_in_file_manager', lambda path: (False, 'no desktop'))
            fallback = client.post('/api/v1/cam/recordings/open').json()['data']
            assert fallback['opened'] is False
            assert fallback['detail'] == 'no desktop'
            recorder.current_file = 'test.mkv'
            assert client.get('/api/v1/cam/recordings/test.mkv').status_code == 409
            assert client.delete('/api/v1/cam/recordings/test.mkv').status_code == 409
            recorder.current_file = None
            response = client.get('/api/v1/cam/recordings/test.mkv')
            assert response.content == b'recorded-content'
            assert 'attachment' in response.headers['content-disposition']
            assert client.delete('/api/v1/cam/recordings/test.mkv').status_code == 200
            assert client.get('/api/v1/cam/recordings/test.mkv').status_code == 404
    finally:
        recorder.finish(stop=True, shutdown=True)


def test_reader_thread_can_be_joined(monkeypatch):
    from app.video.reader import RtspStreamReader
    from app.video.pipeline import CameraBroadcaster
    reader = RtspStreamReader('rtsp://example/102', CameraBroadcaster(), stop_event=threading.Event())
    monkeypatch.setattr(reader, '_load_av', lambda: None)
    reader.start()
    reader.join(timeout=2)
    assert not reader.is_alive()
