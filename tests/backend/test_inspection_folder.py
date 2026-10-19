import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_inspection_service
from app.api.endpoints import inspection as routes
from app.common.exceptions import AppError

pytestmark = pytest.mark.no_database


@pytest.mark.parametrize('opened', [True, False])
def test_open_task_folder_and_headless_fallback(tmp_path, monkeypatch, opened):
    app = FastAPI()
    app.include_router(routes.router)
    resolver = AsyncMock(return_value=tmp_path)
    monkeypatch.setattr(routes, '_task_directory', resolver)
    reveal = Mock(return_value=(opened, '' if opened else 'no desktop'))
    monkeypatch.setattr(routes, 'reveal_in_file_manager', reveal)
    service = object()
    app.dependency_overrides[get_inspection_service] = lambda: service
    with TestClient(app) as client:
        response = client.post('/inspections/current-task/open')
        assert response.status_code == 200
        payload = response.json()['data']
        assert payload['opened'] is opened
        assert payload['dir'] == str(tmp_path)
        if not opened:
            assert payload['detail'] == 'no desktop'
        resolver.assert_awaited_once_with(service, 'current-task')
        reveal.assert_called_once_with(str(tmp_path))
        # The replaced file-list and download API must no longer be exposed.
        assert client.get('/inspections/current-task/files').status_code == 404
        assert client.get('/inspections/current-task/files/video.mkv').status_code == 404


def test_folder_resolver_uses_current_task_directory(tmp_path, monkeypatch):
    # Relocate only the module path so the production root validation is exercised.
    monkeypatch.setattr(routes, '__file__', str(tmp_path / 'app/api/endpoints/inspection.py'))
    target = tmp_path / 'data/cams/2026.10.19-本次任务'
    target.mkdir(parents=True)
    service = SimpleNamespace(status=AsyncMock(return_value={'outputDir': str(target)}))
    assert asyncio.run(routes._task_directory(service, 'current-task')) == target
    service.status.assert_awaited_once_with('current-task')


@pytest.mark.parametrize('target', [None, 'data/cams', 'data/cams/missing', 'outside'])
def test_folder_resolver_rejects_missing_or_outside_directory(tmp_path, monkeypatch, target):
    monkeypatch.setattr(routes, '__file__', str(tmp_path / 'app/api/endpoints/inspection.py'))
    (tmp_path / 'data/cams').mkdir(parents=True)
    (tmp_path / 'outside').mkdir()
    value = str(tmp_path / target) if target else None
    service = SimpleNamespace(status=AsyncMock(return_value={'outputDir': value}))
    with pytest.raises(AppError):
        asyncio.run(routes._task_directory(service, 'run'))
