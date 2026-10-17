import json
import threading
import time
from pathlib import Path
import pytest
from app.robot_core.common.config import PlatformConfig
from app.robot_core.inspection.service import InspectionService

pytestmark = pytest.mark.no_database

@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr('app.robot_core.inspection.paths.data_root', lambda _: tmp_path)
    monkeypatch.setattr('app.robot_core.inspection.service.data_root', lambda _: tmp_path)
    return PlatformConfig(tmp_path / 'config.yaml', {})


def wait(service):
    service.close(timeout=3)


def test_task_directory_name_and_persisted_terminal_state(config, tmp_path):
    def run(_config, **kwargs):
        directory = Path(kwargs['output_directory'])
        assert directory.parent == tmp_path / 'cams'
        assert '鸡舍_第一组' in directory.name
        assert not (directory / 'status.json').exists()
        return dict(state='completed', processed=12)
    service = InspectionService(config, run)
    service.start(dict(task_run_id='run-1', task_name='鸡舍/第一组', camera_ids=['cam01']))
    wait(service)
    status = service.status('run-1')
    assert status['state'] == 'completed'
    assert status['processed'] == 12
    assert service.health()['active']['state'] == 'idle'
    with pytest.raises(ValueError, match='already exists'):
        service.start(dict(task_run_id='run-1'))


def test_stop_waits_for_finalization(config):
    entered, finalize = threading.Event(), threading.Event()
    def run(_config, **kwargs):
        entered.set()
        kwargs['stop_event'].wait(3)
        finalize.wait(3)
        return dict(state='stopped')
    service = InspectionService(config, run)
    service.start(dict(task_run_id='stop'))
    assert entered.wait(1)
    assert service.stop('stop')['state'] == 'stopping'
    with pytest.raises(RuntimeError, match='already running'):
        service.start(dict(task_run_id='new'))
    finalize.set()
    wait(service)
    assert service.status('stop')['state'] == 'stopped'


def test_failed_task_and_recovery(config):
    def fail(*_, **kwargs):
        raise RuntimeError('model unavailable')
    service = InspectionService(config, fail)
    service.start(dict(task_run_id='failed'))
    wait(service)
    assert service.status('failed')['error'] == 'model unavailable'
    service._write_status(dict(task_run_id='interrupted', state='running'), force=True)
    recovered = InspectionService(config, fail)
    recovered.recover()
    assert recovered.status('interrupted')['state'] == 'failed'


def test_detection_page_reads_task_json(config):
    def run(_config, **kwargs):
        Path(kwargs['output_directory'], 'inspection.json').write_text(json.dumps({'records': [{'frame_id': 1}, {'frame_id': 2}]}))
        return dict(state='completed')
    service = InspectionService(config, run)
    service.start(dict(task_run_id='page'))
    wait(service)
    page = service.detections('page', offset=1, limit=1)
    assert page['total'] == 2
    assert page['items'] == [{'frame_id': 2}]


def test_custom_selection(config):
    received = {}
    def run(_config, **kwargs):
        received.update(kwargs)
        return dict(state='completed')
    service = InspectionService(config, run)
    service.start(dict(task_run_id='custom', mode='custom', camera_ids=['cam02', 'cam05', 'cam08']))
    wait(service)
    assert received['camera_ids'] == ('cam02', 'cam05', 'cam08')
    with pytest.raises(ValueError):
        service.start(dict(camera_ids=['cam01', 'cam01']))
