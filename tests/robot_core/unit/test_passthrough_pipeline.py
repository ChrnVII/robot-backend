"""Exercise the production engine with encoded input, without cameras or a database."""
import json
import time
from pathlib import Path
from types import SimpleNamespace
import av
import pytest
from app.robot_core.common.config import PlatformConfig
from app.robot_core.processes import inspection_engine as engine
from app.video.reader import RtspStreamReader
from tests.robot_core.unit.test_realtime_recording import source

pytestmark = pytest.mark.no_database

@pytest.mark.parametrize('pipeline', ['inference', 'passthrough'])
def test_engine_records_and_finalizes(tmp_path, monkeypatch, pipeline):
    source_path = tmp_path / 'input.mkv'
    source(source_path)
    class FileReader(RtspStreamReader):
        def run(self):
            try:
                with av.open(str(source_path)) as container:
                    stream = container.streams.video[0]
                    for packet in container.demux(stream):
                        if self._stop_event.is_set() or not packet.size:
                            break
                        self.recorder.submit(packet, stream)
                        self.keyframes_rx += bool(packet.is_keyframe)
                        for frame in packet.decode():
                            self.slot.put(frame, round(time.monotonic() * 1000), round(float(frame.pts * frame.time_base) * 1000))
                        time.sleep(.02)
                    self.recorder.finish()
            except Exception as error:
                self.last_error = str(error)
    class Runtime:
        calls = 0
        def predict(self, request):
            self.calls += 1
            return SimpleNamespace(status='ok', result={'detections': [{'label': 'heat', 'bbox_xyxy': [1, 1, 10, 10]}]},
                                   model_id='test', model_version='1', model_sha256='abc', latency_ms=1)
    runtime = Runtime()
    monkeypatch.setattr(engine, 'RtspStreamReader', FileReader)
    config = PlatformConfig(tmp_path / 'config.yaml', {'devices': {'cameras': {'items': [
        {'id': 'cam01', 'transport': 'rtsp', 'host': '192.168.136.11'}]}}})
    result = engine.run(config, mode='single', task_run_id='test', duration_seconds=.4,
                        output_directory=tmp_path / 'result', pipeline=pipeline, inference_runtime=runtime)
    assert result['state'] == 'completed', result
    value = json.loads((tmp_path / 'result' / 'inspection.json').read_text())
    assert len(value['records']) > 0
    assert all(r['video_pts'] is not None for r in value['records'])
    assert (runtime.calls > 0) == (pipeline == 'inference')
    with av.open(str(tmp_path / 'result' / 'video.mkv')) as recorded:
        assert len(list(recorded.decode(video=0))) == 12


def test_rtsp_url_matches_configured_path():
    assert engine.camera_url(dict(transport='rtsp', host='192.168.136.11', username='admin', password='admin123')) ==         'rtsp://admin:admin123@192.168.136.11:554/avstream/channel=1/stream=0.sdp'
