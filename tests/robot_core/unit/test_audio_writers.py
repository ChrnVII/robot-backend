"""流式长录音与分段写出器的单元测试（不需要数据库、不需要声卡）。"""

from __future__ import annotations

import struct
import threading
from pathlib import Path

import pytest

from app.robot_core.sensing.audio import (
    AudioCaptureError,
    AudioSettings,
    SoundDeviceAudioProvider,
    _analyse,
    _samples,
)
from app.common.audio_writers import (
    ContinuousWavWriter,
    RunningAudioStats,
    SegmentedWavWriter,
)

pytestmark = pytest.mark.no_database


class _Buffer:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def tobytes(self) -> bytes:
        return self._content


class _Stream:
    def __init__(self, owner: "_FakeSD", channels: int) -> None:
        self.owner = owner
        self.channels = channels
        self.closed = False

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc) -> bool:
        self.closed = True
        return False

    def read(self, frames: int):
        owner = self.owner
        if owner.raise_after is not None and owner.reads >= owner.raise_after:
            raise RuntimeError("device boom")
        if owner.stop_after is not None and owner.reads >= owner.stop_after and owner.stop_event is not None:
            owner.stop_event.set()
        owner.reads += 1
        block = owner.block[: frames * 2 * self.channels]
        return _Buffer(block), False


class _FakeSD:
    def __init__(self, block: bytes, *, stop_after: int | None = None,
                 stop_event: threading.Event | None = None, raise_after: int | None = None) -> None:
        self.block = block
        self.reads = 0
        self.stop_after = stop_after
        self.stop_event = stop_event
        self.raise_after = raise_after

    def query_devices(self):
        return [{"name": "ATR USB microphone: Audio (hw:1,0)", "max_input_channels": 1}]

    def InputStream(self, samplerate, channels, dtype, device, blocksize):
        return _Stream(self, channels)


def _tone(frames: int, value: int = 1000) -> bytes:
    return struct.pack("<h", value) * frames


# --------------------------------------------------------------------------
# 增量统计：必须与全量分析完全一致，否则质量判定口径就漂了
# --------------------------------------------------------------------------

def test_incremental_stats_match_full_analysis():
    values = [0, 100, -200, 32767, -32768, 5000, -5000, 32000]
    pcm = b"".join(struct.pack("<h", value) for value in values)

    stats = RunningAudioStats()
    stats.update(pcm[:6])
    stats.update(pcm[6:])

    full = _analyse(_samples(pcm))
    incremental = stats.metrics(44100, 1)

    assert incremental["frames"] == len(values)
    assert incremental["peakDbfs"] == pytest.approx(full["peak_dbfs"], abs=0.01)
    assert incremental["rmsDbfs"] == pytest.approx(full["rms_dbfs"], abs=0.01)
    assert incremental["clippedRatio"] == pytest.approx(full["clipped_ratio"], abs=1e-9)


def test_incremental_stats_handles_empty():
    metrics = RunningAudioStats().metrics(44100, 1)
    assert metrics["frames"] == 0
    assert metrics["durationSeconds"] == 0.0


# --------------------------------------------------------------------------
# 分段写出器：按字节数切，每段都是完整 WAV
# --------------------------------------------------------------------------

def test_segment_writer_splits_by_bytes(tmp_path: Path):
    writer = SegmentedWavWriter(tmp_path / "segments", 1000, 1, segment_seconds=1)
    writer.feed(_tone(1000))       # 2000 字节 = 恰好一段
    writer.feed(_tone(1500))       # 3000 字节 = 一段 + 半段
    result = writer.close()

    assert result["count"] == 3
    assert [item["durationSeconds"] for item in result["segments"]] == [1.0, 1.0, 0.5]
    assert Path(result["indexPath"]).exists()
    for item in result["segments"]:
        assert Path(item["path"]).stat().st_size > 44   # 头 + 数据


def test_continuous_writer_reports_duration(tmp_path: Path):
    writer = ContinuousWavWriter(tmp_path / "continuous" / "session.wav", 1000, 1)
    writer.feed(_tone(500))
    writer.feed(_tone(500))
    result = writer.close()

    assert result["bytes"] == 2000
    assert result["durationSeconds"] == pytest.approx(1.0)
    assert Path(result["path"]).stat().st_size == 44 + 2000


# --------------------------------------------------------------------------
# capture_stream：一次读、多处写
# --------------------------------------------------------------------------

def _provider(sd, *, sample_rate=44100, channels=1, block_frames=4410):
    settings = AudioSettings(sample_rate=sample_rate, channels=channels, block_frames=block_frames)
    return SoundDeviceAudioProvider(settings, sounddevice_module=sd), settings


def test_capture_stream_writes_both_outputs(tmp_path: Path):
    stop = threading.Event()
    sd = _FakeSD(_tone(4410), stop_after=3, stop_event=stop)
    provider, settings = _provider(sd)

    result = provider.capture_stream(
        settings,
        continuous_writer=ContinuousWavWriter(tmp_path / "continuous" / "session.wav", 44100, 1),
        segment_writer=SegmentedWavWriter(tmp_path / "segments", 44100, 1, segment_seconds=60),
        stop_event=stop,
    )

    # 读了 4 块 × 0.1 秒
    assert result["metrics"]["durationSeconds"] == pytest.approx(0.4, abs=0.05)
    assert result["metrics"]["stoppedBy"] == "stop_event"
    assert result["continuous"]["bytes"] == 4 * 8820
    assert result["segments"]["count"] == 1          # 0.4 秒远未到 60 秒
    assert result["segments"]["totalSeconds"] == pytest.approx(0.4, abs=0.05)


def test_capture_stream_supports_audio_only_mode(tmp_path: Path):
    """组合 D（仅音频，只开长录音）不应要求分段 writer。"""
    stop = threading.Event()
    sd = _FakeSD(_tone(4410), stop_after=1, stop_event=stop)
    provider, settings = _provider(sd)

    result = provider.capture_stream(
        settings,
        continuous_writer=ContinuousWavWriter(tmp_path / "session.wav", 44100, 1),
        stop_event=stop,
    )

    assert result["segments"] is None
    assert result["continuous"]["bytes"] == 2 * 8820


def test_capture_stream_supports_segments_only_mode(tmp_path: Path):
    """组合 C（只分段）也应成立。"""
    stop = threading.Event()
    sd = _FakeSD(_tone(4410), stop_after=1, stop_event=stop)
    provider, settings = _provider(sd)

    result = provider.capture_stream(
        settings,
        segment_writer=SegmentedWavWriter(tmp_path / "segments", 44100, 1, segment_seconds=60),
        stop_event=stop,
    )

    assert result["continuous"] is None
    assert result["segments"]["count"] == 1


def test_capture_stream_requires_a_writer():
    provider, settings = _provider(_FakeSD(_tone(10)))
    with pytest.raises(AudioCaptureError):
        provider.capture_stream(settings)


def test_capture_stream_closes_files_on_device_error(tmp_path: Path):
    """设备中途报错时，已写入的数据必须 close 成可播放文件。"""
    provider, settings = _provider(_FakeSD(_tone(4410), raise_after=2))

    with pytest.raises(AudioCaptureError):
        provider.capture_stream(
            settings,
            continuous_writer=ContinuousWavWriter(tmp_path / "session.wav", 44100, 1),
        )

    wav = tmp_path / "session.wav"
    assert wav.exists()
    assert wav.stat().st_size > 44      # 已写入的数据没丢，头也被回填


def test_capture_stream_respects_max_duration(tmp_path: Path):
    sd = _FakeSD(_tone(4410))           # 永不主动停
    provider, settings = _provider(sd)

    result = provider.capture_stream(
        settings,
        continuous_writer=ContinuousWavWriter(tmp_path / "session.wav", 44100, 1),
        max_duration_seconds=0.25,
    )

    assert result["metrics"]["stoppedBy"] == "max_duration"
    assert result["metrics"]["durationSeconds"] >= 0.2
