"""配置期相机编号校验：唯一、非空、字符集合法（P0）。"""

from __future__ import annotations

import pytest

from app.common.cameras import (
    DEFAULT_PTZ_CAMERA_ID,
    PTZ_DEVICE_ID,
    cameras_from_values,
    inspection_camera_ids,
    provider_ids,
    ptz_camera_id,
    validate_cameras,
)
from app.common.deployment import ConfigurationError

pytestmark = pytest.mark.no_database


def config_with(items=None, camera=None):
    devices = {}
    if camera is not None:
        devices["camera"] = camera
    if items is not None:
        devices["cameras"] = {"items": items}
    return {"devices": devices}


def test_ptz_and_inspection_cameras_are_listed_with_distinct_identities():
    values = config_with(
        camera={"camera_id": "VAR-1", "host": "192.168.137.64"},
        items=[{"id": "cam01"}, {"id": "cam02"}, {"id": "cam03"}],
    )
    validate_cameras(values)
    entries = cameras_from_values(values)
    assert [entry.id for entry in entries] == ["VAR-1", "cam01", "cam02", "cam03"]
    assert entries[0].kind == "ptz"
    assert entries[0].device_id == PTZ_DEVICE_ID
    assert entries[0].as_dict()["deviceId"] == "camera"
    assert entries[1].device_id == "cam01"
    assert entries[1].label == "摄像头 01"
    assert provider_ids(values) == ["camera", "cam01", "cam02", "cam03"]
    assert inspection_camera_ids(values) == ["cam01", "cam02", "cam03"]


def test_ptz_id_falls_back_to_legacy_default():
    assert ptz_camera_id(config_with(camera={"host": "x"})) == DEFAULT_PTZ_CAMERA_ID
    validate_cameras(config_with(camera={"camera_id": ""}))


def test_duplicate_inspection_ids_are_rejected():
    values = config_with(items=[{"id": "cam01"}, {"id": "cam01"}])
    with pytest.raises(ConfigurationError, match="duplicate camera id 'cam01'"):
        validate_cameras(values)


def test_inspection_id_colliding_with_ptz_id_is_rejected():
    values = config_with(camera={"camera_id": "cam02"}, items=[{"id": "cam02"}])
    with pytest.raises(ConfigurationError, match="duplicate camera id 'cam02'"):
        validate_cameras(values)


def test_missing_inspection_id_is_rejected():
    values = config_with(items=[{"host": "192.168.136.11"}])
    with pytest.raises(ConfigurationError, match="must not be empty"):
        validate_cameras(values)


def test_invalid_inspection_id_characters_are_rejected():
    values = config_with(items=[{"id": "cam 01"}])
    with pytest.raises(ConfigurationError, match="must match"):
        validate_cameras(values)


def test_config_without_devices_is_tolerated():
    validate_cameras({})
    assert cameras_from_values({}) == []
