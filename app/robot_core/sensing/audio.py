"""麦克风采集：流式分块 + 电平/削波/静音质量判定。

参数已按真机实测冻结（见 ``docs/AUDIO_CAPTURE_UPLOAD_PLAN.md`` §0.5）：

- 44.1 kHz / 单声道 / 16-bit / 10 s / WAV，不做 AGC、不做降噪；
- 设备按 **名字子串** 匹配（``"USB Audio"``），避免 PortAudio 索引随 USB
  枚举顺序漂移；匹配不到时才回退系统默认输入并打 WARN；
- 每次采集独立 ``InputStream`` 并在 ``with`` 中关闭 —— 旧程序靠“每次录完
  kill 自己”绕开的设备占用问题，在这里由显式 close 解决；
- 质量判定：整段 RMS 低于阈值 → ``invalid``；满幅样本占比超阈值 → ``degraded``；
  实际时长不足目标的 90% → ``invalid``。
"""

from __future__ import annotations

import array
import io
import logging
import math
import sys
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional

from app.contracts.errors import IpcErrorCode
from app.robot_core.contracts import CaptureRequest, CaptureResult

logger = logging.getLogger(__name__)

QUALITY_VALID = "valid"
QUALITY_DEGRADED = "degraded"
QUALITY_INVALID = "invalid"

#: 冻结参数（三份 YAML 与 ``AudioSettings`` 默认值保持一致）。
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_DURATION_SECONDS = 10.0

_INT16_FULL_SCALE = 32768.0
_UNRETRYABLE_HINTS = (
    "invalid device", "no device", "device unavailable", "invalid number of channels",
    "invalid sample rate", "error querying device", "device not found",
)


class AudioCaptureError(RuntimeError):
    """Raised when an audio input cannot be recorded."""

    #: 让 IPC 层把“设备打不开”回成 CAPTURE_FAILED（后端 409），而不是 INTERNAL_ERROR。
    ipc_code = IpcErrorCode.CAPTURE_FAILED


@dataclass(frozen=True)
class AudioSettings:
    device: Any = None
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = 1
    duration_seconds: float = DEFAULT_DURATION_SECONDS
    gain_db: float = 0.0
    silence_rms_dbfs: float = -70.0
    clip_ratio_threshold: float = 0.001
    block_frames: int = 4410
    timeout_margin_seconds: float = 5.0
    max_retries: int = 1
    retry_delay_seconds: float = 0.5

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "AudioSettings":
        values = values or {}
        device = values.get("device")
        if device is not None and str(device).strip() == "":
            raise ValueError("audio.device must not be empty")
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        sample_rate = int(values.get("sample_rate", DEFAULT_SAMPLE_RATE))
        channels = int(values.get("channels", 1))
        duration_seconds = float(values.get("duration_seconds", DEFAULT_DURATION_SECONDS))
        gain_db = float(values.get("gain_db", 0.0) or 0.0)
        silence_rms_dbfs = float(values.get("silence_rms_dbfs", -70.0))
        clip_ratio_threshold = float(values.get("clip_ratio_threshold", 0.001))
        block_frames = int(values.get("block_frames", 4410))
        timeout_margin_seconds = float(values.get("timeout_margin_seconds", 5.0))
        max_retries = int(values.get("max_retries", 1))
        retry_delay_seconds = float(values.get("retry_delay_seconds", 0.5))
        if sample_rate < 8000 or sample_rate > 192000:
            raise ValueError("audio sample_rate must be between 8000 and 192000")
        if channels < 1 or channels > 8:
            raise ValueError("audio channels must be between 1 and 8")
        if duration_seconds <= 0 or duration_seconds > 60:
            raise ValueError("audio duration_seconds must be greater than 0 and at most 60")
        if silence_rms_dbfs >= 0 or silence_rms_dbfs < -120:
            raise ValueError("audio silence_rms_dbfs must be between -120 and 0")
        if not 0 <= clip_ratio_threshold <= 1:
            raise ValueError("audio clip_ratio_threshold must be between 0 and 1")
        if block_frames < 128 or block_frames > sample_rate:
            raise ValueError("audio block_frames must be between 128 and sample_rate")
        if timeout_margin_seconds < 0:
            raise ValueError("audio timeout_margin_seconds must not be negative")
        if max_retries < 0 or max_retries > 5:
            raise ValueError("audio max_retries must be between 0 and 5")
        if retry_delay_seconds < 0:
            raise ValueError("audio retry_delay_seconds must not be negative")
        return cls(device, sample_rate, channels, duration_seconds, gain_db, silence_rms_dbfs,
                   clip_ratio_threshold, block_frames, timeout_margin_seconds, max_retries,
                   retry_delay_seconds)


class SoundDeviceAudioProvider:
    """Record signed 16-bit PCM audio as a WAV file, with quality metrics."""

    def __init__(self, settings: AudioSettings | None = None, *, sounddevice_module: Any = None):
        self.settings = settings or AudioSettings()
        self._sounddevice = sounddevice_module
        self._lock = threading.Lock()

    def capture(self, request: CaptureRequest, *, stop_event: Optional[threading.Event] = None) -> CaptureResult:
        settings = self._settings_for(request)
        if not self._lock.acquire(blocking=False):
            raise AudioCaptureError("audio capture already in progress on this device")
        try:
            attempt = 0
            while True:
                try:
                    return self._capture_once(settings, stop_event, attempt)
                except AudioCaptureError as error:
                    if attempt >= settings.max_retries or not _is_retryable(error):
                        raise
                    attempt += 1
                    logger.warning("audio capture retry %s/%s: %s", attempt, settings.max_retries, error)
                    time.sleep(settings.retry_delay_seconds)
        finally:
            self._lock.release()

    # -- 内部实现 ---------------------------------------------------------
    def _capture_once(self, settings: AudioSettings, stop_event: Optional[threading.Event],
                      attempt: int) -> CaptureResult:
        sd = self._load_sounddevice()
        device, device_label = self._resolve_device(sd, settings.device)
        target_frames = max(1, round(settings.duration_seconds * settings.sample_rate))
        block_frames = max(1, min(settings.block_frames, target_frames))
        deadline = time.monotonic() + settings.duration_seconds + settings.timeout_margin_seconds
        chunks: List[bytes] = []
        frames_read = 0
        overflows = 0
        timed_out = False

        try:
            stream = sd.InputStream(
                samplerate=settings.sample_rate,
                channels=settings.channels,
                dtype="int16",
                device=device,
                blocksize=block_frames,
            )
        except Exception as error:  # noqa: BLE001 - 驱动异常类型不稳定，统一转领域异常
            raise AudioCaptureError(f"audio device unavailable ({device_label}): {error}") from error

        try:
            with stream:
                while frames_read < target_frames:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if time.monotonic() > deadline:
                        timed_out = True
                        logger.warning("audio capture timed out after %.1fs", settings.duration_seconds)
                        break
                    want = min(block_frames, target_frames - frames_read)
                    try:
                        data, overflowed = stream.read(want)
                    except Exception as error:  # noqa: BLE001
                        raise AudioCaptureError(f"audio read failed ({device_label}): {error}") from error
                    payload = _to_bytes(data)
                    if not payload:
                        break
                    chunks.append(payload)
                    frames_read += len(payload) // (2 * settings.channels)
                    overflows += 1 if overflowed else 0
        except AudioCaptureError:
            raise
        except Exception as error:  # noqa: BLE001 - 端口层/驱动层的其余异常
            raise AudioCaptureError(f"audio capture failed ({device_label}): {error}") from error

        if frames_read <= 0 or not chunks:
            raise AudioCaptureError(f"audio capture returned no frames ({device_label})")

        pcm = b"".join(chunks)
        analysis = _analyse(_samples(pcm))
        quality_state = _quality_state(analysis, frames_read, target_frames, timed_out, settings)
        metrics = {
            "device": device_label,
            "sampleRate": settings.sample_rate,
            "channels": settings.channels,
            "frames": frames_read,
            "durationSeconds": round(frames_read / settings.sample_rate, 3),
            "requestedSeconds": settings.duration_seconds,
            "rmsDbfs": round(analysis["rms_dbfs"], 2),
            "peakDbfs": round(analysis["peak_dbfs"], 2),
            "clippedRatio": round(analysis["clipped_ratio"], 6),
            "gainDb": settings.gain_db,
            "overflows": overflows,
            "timedOut": timed_out,
            "attempt": attempt + 1,
        }
        try:
            content = _encode_wav(pcm, settings.sample_rate, settings.channels)
        except Exception as error:  # noqa: BLE001
            raise AudioCaptureError("audio WAV encoding failed") from error
        logger.info("audio captured: %s", metrics)
        return CaptureResult(content=content, quality_state=quality_state, metrics=metrics)

    def _load_sounddevice(self) -> Any:
        if self._sounddevice is not None:
            return self._sounddevice
        try:
            import sounddevice
        except Exception as error:  # noqa: BLE001 - ImportError 或缺少系统 libportaudio
            raise AudioCaptureError(
                "sounddevice unavailable: install libportaudio2 (apt install libportaudio2)"
            ) from error
        self._sounddevice = sounddevice
        return sounddevice

    def _resolve_device(self, sd: Any, device: Any) -> tuple[Any, str]:
        """把配置值解析成 PortAudio 设备索引。

        - ``None`` → 系统默认输入；
        - 整数 / 纯数字字符串 → 直接用索引；
        - 其它字符串 → 先在本进程输入设备名里做**不区分大小写的子串匹配**；
          匹配不到时**不回退默认设备**（板载声卡的静音比报错更危险），而是把
          字符串原样交给 PortAudio/ALSA 解析 —— ALSA 支持按设备名子串匹配
          （真机实测 ``"USB Audio"`` 能命中 ``ATR USB microphone: Audio (hw:1,0)``）；
          若 ALSA 也解析失败，``InputStream`` 会抛错并被包装成 AudioCaptureError。
        """
        if device is None:
            return None, "default"
        if isinstance(device, int):
            return device, f"index:{device}"
        text = str(device).strip()
        if text.isdigit():
            return int(text), f"index:{text}"
        try:
            for index, entry in enumerate(sd.query_devices()):
                if int(entry.get("max_input_channels", 0)) <= 0:
                    continue
                name = str(entry.get("name", ""))
                if text.lower() in name.lower():
                    return index, name
        except Exception as error:  # noqa: BLE001 - 探测失败则交给 PortAudio 解析
            logger.warning("audio device query failed: %s", error)
        logger.info("audio device %r not listed locally; delegating to PortAudio name matching", text)
        return text, text

    def _settings_for(self, request: CaptureRequest) -> AudioSettings:
        if not request.parameters:
            return self.settings
        values = {
            "device": request.parameters.get("device", self.settings.device),
            "sample_rate": request.parameters.get("sample_rate", self.settings.sample_rate),
            "channels": request.parameters.get("channels", self.settings.channels),
            "duration_seconds": request.parameters.get("duration_seconds", self.settings.duration_seconds),
        }
        override = AudioSettings.from_mapping({**self._settings_mapping(), **values})
        return override

    def _settings_mapping(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "device": settings.device,
            "sample_rate": settings.sample_rate,
            "channels": settings.channels,
            "duration_seconds": settings.duration_seconds,
            "gain_db": settings.gain_db,
            "silence_rms_dbfs": settings.silence_rms_dbfs,
            "clip_ratio_threshold": settings.clip_ratio_threshold,
            "block_frames": settings.block_frames,
            "timeout_margin_seconds": settings.timeout_margin_seconds,
            "max_retries": settings.max_retries,
            "retry_delay_seconds": settings.retry_delay_seconds,
        }


def simulate_audio_capture(request: CaptureRequest, *, sample_rate: int = DEFAULT_SAMPLE_RATE,
                           duration_seconds: float = 1.0) -> CaptureResult:
    """模拟模式：产出**合法的静音 WAV**（而不是 `simulated:...` 文本字节）。

    以前模拟模式把字符串写进 `.wav`，任何播放器/训练脚本读到都是损坏文件；
    这里给等长静音，既能让前端播放器跑通，也不会被误当成可用训练样本
    （``metrics.simulated=True`` 可事后筛掉）。
    """
    parameters = request.parameters or {}
    rate = int(parameters.get("sample_rate", sample_rate) or sample_rate)
    duration = float(parameters.get("duration_seconds", duration_seconds) or duration_seconds)
    duration = max(0.1, min(duration, 60.0))
    frames = max(1, round(duration * rate))
    content = _encode_wav(b"\x00\x00" * frames, rate, 1)
    return CaptureResult(content=content, quality_state=QUALITY_VALID, metrics={
        "simulated": True, "sampleRate": rate, "channels": 1,
        "durationSeconds": round(frames / rate, 3), "rmsDbfs": None, "peakDbfs": None,
    })


def _is_retryable(error: BaseException) -> bool:
    message = str(error).lower()
    return not any(hint in message for hint in _UNRETRYABLE_HINTS)


def _to_bytes(data: Any) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if hasattr(data, "tobytes"):
        return data.tobytes()
    return bytes(memoryview(data))


def _samples(pcm: bytes) -> array.array:
    usable = len(pcm) - (len(pcm) % 2)
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _analyse(samples: array.array) -> dict[str, float]:
    if not samples:
        return {"peak": 0.0, "peak_dbfs": -120.0, "rms_dbfs": -120.0, "clipped_ratio": 0.0}
    peak = 0
    clipped = 0
    total = 0
    for value in samples:
        if value < 0:
            value = -value
        if value > peak:
            peak = value
        if value >= 32767:
            clipped += 1
        total += value * value
    rms = math.sqrt(total / len(samples))
    return {
        "peak": float(peak),
        "peak_dbfs": _dbfs(peak),
        "rms_dbfs": _dbfs(rms),
        "clipped_ratio": clipped / len(samples),
    }


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1.0) / _INT16_FULL_SCALE)


def _quality_state(analysis: Mapping[str, float], frames_read: int, target_frames: int,
                   timed_out: bool, settings: AudioSettings) -> str:
    if frames_read < target_frames * 0.9:
        return QUALITY_INVALID
    if analysis["rms_dbfs"] <= settings.silence_rms_dbfs:
        return QUALITY_INVALID
    if analysis["clipped_ratio"] > settings.clip_ratio_threshold:
        return QUALITY_DEGRADED
    if timed_out:
        return QUALITY_DEGRADED
    return QUALITY_VALID


def _encode_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()
