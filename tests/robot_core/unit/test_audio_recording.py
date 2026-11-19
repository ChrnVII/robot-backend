"""独立音频录制：开始/停止、目录围栏、设备失败时的收尾。

用假 provider 代替 sounddevice，跑真实 WAV 写入，断言文件可解析。
"""

from __future__ import annotations

import threading
import time
import wave

import pytest

from app.common.exceptions import AppError
from app.services import audio_recording as module

pytestmark = pytest.mark.no_database


class FakeProvider:
    """假的采集器：写 3 块静音后阻塞等停止，再收尾（与真实 capture_stream 语义一致）。"""

    def __init__(self, *, chunks: int = 3, fail: bool = False) -> None:
        self.chunks = chunks
        self.fail = fail
        self.closed = None

    def capture_stream(self, settings, *, continuous_writer=None, stop_event=None, **_kwargs):
        if self.fail:
            raise module.AudioCaptureError("audio device unavailable (fake)")
        payload = b"\x00\x00" * settings.block_frames * settings.channels
        try:
            for _ in range(self.chunks):
                continuous_writer.feed(payload)
            stop_event.wait(5.0)
        finally:
            self.closed = continuous_writer.close()
        return {"continuous": self.closed, "metrics": {"rmsDbfs": -70.0}}


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ALLOWED_ROOT", tmp_path)
    task_dir = tmp_path / "2026.11.19.120000.000000-手动采集"
    task_dir.mkdir()
    provider = FakeProvider()
    instance = module.AudioRecordingService(
        config_path=tmp_path / "missing.yaml",
        provider_factory=lambda _settings: provider,
    )
    return instance, task_dir, provider


def test_start_then_stop_writes_playable_wav(service):
    instance, task_dir, _provider = service

    started = instance.start(task_dir)
    assert started["state"] in {"starting", "recording"}
    assert module.Path(started["path"]).parent.name == "audio"
    assert module.Path(started["path"]).name == "session.wav"

    # 等线程真正进入采集，再停；否则可能停在 writer 创建之前
    deadline = time.monotonic() + 3
    while instance.active()["state"] != "recording" and time.monotonic() < deadline:
        time.sleep(0.01)

    result = instance.stop()

    assert result["state"] == "completed"
    assert result["bytes"] > 0
    assert result["durationSeconds"] > 0
    path = module.Path(result["path"])
    assert path.is_file()
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 44100
        assert handle.getnframes() > 0
    assert instance.active() is None


def test_second_start_is_rejected_while_running(service):
    instance, task_dir, _provider = service
    instance.start(task_dir)
    other = task_dir / "another"
    other.mkdir()
    with pytest.raises(AppError) as error:
        instance.start(other)
    assert error.value.code == "AUDIO_BUSY"
    instance.stop()


def test_directory_outside_allowed_root_is_rejected(service, tmp_path):
    instance, _task_dir, _provider = service
    outside = tmp_path.parent / "not-allowed"
    outside.mkdir(exist_ok=True)
    try:
        with pytest.raises(AppError) as error:
            instance.start(outside)
        assert error.value.code == "FORBIDDEN"
    finally:
        outside.rmdir()


def test_device_failure_leaves_no_empty_file(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ALLOWED_ROOT", tmp_path)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    instance = module.AudioRecordingService(
        config_path=tmp_path / "missing.yaml",
        provider_factory=lambda _settings: FakeProvider(fail=True),
    )

    instance.start(task_dir)
    deadline = time.monotonic() + 3
    while instance.active() and instance.active()["state"] != "failed" and time.monotonic() < deadline:
        time.sleep(0.01)

    active = instance.active()
    assert active is not None and active["state"] == "failed"
    assert "audio device unavailable" in active["error"]
    assert not (task_dir / "audio" / "session.wav").exists()


def test_writes_sidecar_metadata_for_sync(service):
    """audio/recording.json 是给后处理算音视频偏移用的，字段不能少。"""
    import json

    instance, task_dir, _provider = service
    instance.start(task_dir, duration_seconds=1.5)
    deadline = time.monotonic() + 3
    while instance.active()["state"] != "recording" and time.monotonic() < deadline:
        time.sleep(0.01)
    instance.stop()

    meta = json.loads((task_dir / "audio" / "recording.json").read_text(encoding="utf-8"))
    assert meta["limitSeconds"] == 1.5
    assert meta["durationSeconds"] > 0
    assert meta["startedAt"] <= meta["finishedAt"]
    assert meta["sampleRate"] == 44100 and meta["channels"] == 1
