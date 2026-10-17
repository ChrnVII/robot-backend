import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.deps import get_inspection_service
from app.api.endpoints import inspection as routes
from app.common.exceptions import AppError

pytestmark = pytest.mark.no_database


def test_files_snapshot_download_and_active_video_lock(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    status = {'state': 'running'}
    async def directory(*_):
        return tmp_path, status
    monkeypatch.setattr(routes, '_task_directory', directory)
    app.dependency_overrides[get_inspection_service] = lambda: object()
    (tmp_path / 'inspection.json').write_text('{"records":[]}')
    (tmp_path / 'video.mkv').write_bytes(b'mkv')
    (tmp_path / 'private.txt').write_text('hidden')
    with TestClient(app) as client:
        response = client.get('/inspections/run/files')
        files = response.json()['data']['files']
        assert {item['name'] for item in files} == {'inspection.json', 'video.mkv'}
        assert files[0]['writing']
        assert client.get('/inspections/run/files/inspection.json').json() == {'records': []}
        with pytest.raises(AppError):
            client.get('/inspections/run/files/video.mkv')
        with pytest.raises(AppError):
            client.get('/inspections/run/files/private.txt')
        status['state'] = 'completed'
        assert client.get('/inspections/run/files/video.mkv').content == b'mkv'


def test_folder_resolver_rejects_paths_outside_cams(tmp_path):
    service = SimpleNamespace(status=AsyncMock(return_value={'outputDir': str(tmp_path)}))
    with pytest.raises(AppError):
        asyncio.run(routes._task_directory(service, 'run'))
