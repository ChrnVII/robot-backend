"""相机目录：唯一编号来源、capture 编号映射与巡检 cameraId 校验。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.endpoints import cameras as cameras_endpoint
from app.common.config import Settings
from app.common.exceptions import ValidationFailed
from app.schemas.inspection import StartInspectionRequest
from app.schemas.media import MediaCaptureRequest
from app.services.cameras import CameraDirectoryService
from app.services.inspection import InspectionService
from app.services.media import MediaService

pytestmark = pytest.mark.no_database

PLATFORM_YAML = """
devices:
  camera:
    camera_id: VAR-1
    host: 192.168.137.64
  cameras:
    items:
      - id: cam01
        host: 192.168.136.11
      - id: cam02
        host: 192.168.136.12
      - id: cam03
        host: 192.168.136.13
"""


def directory(tmp_path, configured=True) -> CameraDirectoryService:
    settings = Settings()
    if configured:
        path = tmp_path / "platform.yaml"
        path.write_text(PLATFORM_YAML, encoding="utf-8")
        settings = Settings(platform_config=str(path))
    return CameraDirectoryService(settings)


def test_directory_lists_ptz_and_inspection_cameras(tmp_path):
    service = directory(tmp_path)
    entries = [entry.as_dict() for entry in service.entries()]
    assert [entry["id"] for entry in entries] == ["VAR-1", "cam01", "cam02", "cam03"]
    assert entries[0]["kind"] == "ptz"
    assert entries[0]["deviceId"] == "camera"
    assert entries[1]["kind"] == "inspection"
    assert entries[1]["deviceId"] == "cam01"
    assert service.source.endswith("platform.yaml")
    assert service.inspection_ids() == ["cam01", "cam02", "cam03"]


def test_endpoint_returns_directory_payload(tmp_path):
    request = SimpleNamespace(state=SimpleNamespace(trace_id="trace-camera"))
    payload = cameras_endpoint.list_cameras(request, directory(tmp_path))
    assert payload["code"] == "OK"
    assert payload["data"]["cameras"][0]["id"] == "VAR-1"
    assert payload["traceId"] == "trace-camera"


def test_require_ptz_rejects_inspection_camera_with_readable_error(tmp_path):
    with pytest.raises(ValidationFailed) as error:
        directory(tmp_path).require_ptz("cam02")
    assert "cam02" in str(error.value)
    assert "球形摄像头 VAR-1" in str(error.value)


def test_require_ptz_rejects_unknown_camera_id(tmp_path):
    with pytest.raises(ValidationFailed) as error:
        directory(tmp_path).require_ptz("cam99")
    assert "cam99" in str(error.value)
    assert "可用编号" in str(error.value)


class _CaptureGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call(self, request):
        self.calls.append(dict(request))
        return {"assetId": "asset_1"}


def capture_request(device_id: str, media_type: str = "image") -> MediaCaptureRequest:
    return MediaCaptureRequest(taskRunId="run-1", waypointId="p1", deviceId=device_id,
                               mediaType=media_type, parameters={})


@pytest.mark.parametrize("device_id,provider", [
    ("camera", "camera"),   # 旧写法：球形设备名
    ("VAR-1", "camera"),    # 球形相机编号 → 同一个 provider
    ("cam03", "cam03"),     # 多路编号 → 自身 provider
])
def test_media_capture_resolves_device_id_to_provider(tmp_path, device_id, provider):
    import asyncio

    gateway = _CaptureGateway()
    service = MediaService(gateway, directory(tmp_path))
    asyncio.run(service.capture(capture_request(device_id)))
    assert gateway.calls[0]["device_id"] == provider


def test_media_capture_rejects_unknown_camera_id(tmp_path):
    import asyncio

    service = MediaService(_CaptureGateway(), directory(tmp_path))
    with pytest.raises(ValidationFailed) as error:
        asyncio.run(service.capture(capture_request("cam99")))
    assert "cam99" in str(error.value)


class _InspectionGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def call(self, request):
        self.calls.append(dict(request))
        return {"taskRunId": "run-1"}


def test_inspection_rejects_camera_ids_missing_from_config(tmp_path):
    import asyncio

    gateway = _InspectionGateway()
    service = InspectionService(gateway, directory(tmp_path))
    request = StartInspectionRequest(mode="custom", cameraIds=["cam01", "cam09"])
    with pytest.raises(ValidationFailed) as error:
        asyncio.run(service.start(request))
    assert "cam09" in str(error.value)
    assert "cam01" in str(error.value)
    assert gateway.calls == []


def test_inspection_reports_too_few_enabled_cameras(tmp_path):
    import asyncio

    service = InspectionService(_InspectionGateway(), directory(tmp_path))
    with pytest.raises(ValidationFailed) as error:
        asyncio.run(service.start(StartInspectionRequest(mode="eight")))
    assert "需要 8 路" in str(error.value)


def test_inspection_skips_camera_validation_without_platform_config(tmp_path):
    import asyncio

    gateway = _InspectionGateway()
    service = InspectionService(gateway, directory(tmp_path, configured=False))
    asyncio.run(service.start(StartInspectionRequest(mode="custom", cameraIds=["cam09"])))
    assert gateway.calls, "未配置 YAML 时应交给 inspection-worker 兜底"


# ------------------------- 单例接口的 cameraId 参数 -------------------------


def _request():
    return SimpleNamespace(state=SimpleNamespace(trace_id="trace-camera"))


def test_presets_endpoint_rejects_non_ptz_camera(tmp_path):
    from app.api.endpoints import workbench

    with pytest.raises(ValidationFailed) as error:
        workbench.list_presets(_request(), cameraId="cam01", service=SimpleNamespace(), directory=directory(tmp_path))
    assert "cam01" in str(error.value)


def test_presets_endpoint_accepts_ptz_camera(tmp_path):
    from app.api.endpoints import workbench

    service = SimpleNamespace(list_presets=lambda: [{"presetId": 1, "name": "大门"}])
    payload = workbench.list_presets(_request(), cameraId="VAR-1", service=service, directory=directory(tmp_path))
    assert payload["data"] == [{"presetId": 1, "name": "大门"}]


def test_stream_status_endpoint_accepts_default_and_ptz(tmp_path, monkeypatch):
    from app.api.endpoints import system

    class _Stream:
        def status(self):
            return {"connected": True}

    monkeypatch.setattr(system, "get_camera_stream_service", lambda: _Stream())
    assert system.camera_stream_status(_request(), cameraId=None, directory=directory(tmp_path))["data"] == {"connected": True}
    assert system.camera_stream_status(_request(), cameraId="VAR-1", directory=directory(tmp_path))["code"] == "OK"
    with pytest.raises(ValidationFailed):
        system.camera_stream_status(_request(), cameraId="cam02", directory=directory(tmp_path))
