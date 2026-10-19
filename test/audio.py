from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from inspection_robot.contracts import CaptureRequest


class AudioCaptureError(RuntimeError):
    """Raised when an audio input cannot be recorded."""


@dataclass(frozen=True)
class AudioSettings:
    device: Any = None
    sample_rate: int = 16000
    channels: int = 1
    duration_seconds: float = 3.0

    @classmethod
    def from_mapping(cls, values: Optional[Mapping[str, Any]]) -> "AudioSettings":
        values = values or {}
        device = values.get("device")
        if device is not None and str(device).strip() == "":
            raise ValueError("audio.device must not be empty")
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        sample_rate = int(values.get("sample_rate", 16000))
        channels = int(values.get("channels", 1))
        duration_seconds = float(values.get("duration_seconds", 3.0))
        if sample_rate < 8000 or sample_rate > 192000:
            raise ValueError("audio sample_rate must be between 8000 and 192000")
        if channels < 1 or channels > 8:
            raise ValueError("audio channels must be between 1 and 8")
        if duration_seconds <= 0 or duration_seconds > 60:
            raise ValueError("audio duration_seconds must be greater than 0 and at most 60")
        return cls(device, sample_rate, channels, duration_seconds)


class SoundDeviceAudioProvider:
    """Record signed 16-bit PCM audio and return a WAV file."""

    def __init__(self, settings: AudioSettings | None = None, *, sounddevice_module: Any = None):
        self.settings = settings or AudioSettings()
        self._sounddevice = sounddevice_module

    def capture(self, request: CaptureRequest) -> bytes:
        sd = self._load_sounddevice()
        settings = self._settings_for(request)
        frames = max(1, round(settings.duration_seconds * settings.sample_rate))
        try:
            recording = sd.rec(frames, samplerate=settings.sample_rate, channels=settings.channels,
                               dtype="int16", device=settings.device)
            sd.wait()
        except Exception as error:
            raise AudioCaptureError(f"audio capture failed: {error}") from error
        try:
            pcm = recording.tobytes() if hasattr(recording, "tobytes") else bytes(recording)
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(settings.channels)
                wav.setsampwidth(2)
                wav.setframerate(settings.sample_rate)
                wav.writeframes(pcm)
            return output.getvalue()
        except Exception as error:
            raise AudioCaptureError("audio WAV encoding failed") from error

    def _load_sounddevice(self) -> Any:
        if self._sounddevice is not None:
            return self._sounddevice
        try:
            import sounddevice
        except ImportError as error:
            raise AudioCaptureError("install the media extra with sounddevice before using audio capture") from error
        self._sounddevice = sounddevice
        return sounddevice

    def _settings_for(self, request: CaptureRequest) -> AudioSettings:
        if not request.parameters:
            return self.settings
        values = {
            "device": request.parameters.get("device", self.settings.device),
            "sample_rate": request.parameters.get("sample_rate", self.settings.sample_rate),
            "channels": request.parameters.get("channels", self.settings.channels),
            "duration_seconds": request.parameters.get("duration_seconds", self.settings.duration_seconds),
        }
        return AudioSettings.from_mapping(values)
