import io
import unittest
import wave

from app.robot_core.contracts import CaptureRequest
from app.robot_core.sensing.audio import AudioCaptureError, AudioSettings, SoundDeviceAudioProvider


class FakeRecording:
    def __init__(self, content):
        self.content = content

    def tobytes(self):
        return self.content


class FakeSoundDevice:
    def __init__(self, content=b"\x01\x00\x02\x00"):
        self.content = content
        self.calls = []
        self.waited = False

    def rec(self, frames, **kwargs):
        self.calls.append((frames, kwargs))
        return FakeRecording(self.content)

    def wait(self):
        self.waited = True


class FailingSoundDevice(FakeSoundDevice):
    def rec(self, frames, **kwargs):
        raise RuntimeError("input unavailable")


class AudioProviderTest(unittest.TestCase):
    def test_capture_returns_pcm_wav(self):
        sounddevice = FakeSoundDevice()
        provider = SoundDeviceAudioProvider(
            AudioSettings(device=2, sample_rate=16000, channels=1, duration_seconds=0.5),
            sounddevice_module=sounddevice,
        )
        request = CaptureRequest("run-1", "point-1", "audio", "audio")

        content = provider.capture(request)

        self.assertTrue(sounddevice.waited)
        self.assertEqual(sounddevice.calls[0][0], 8000)
        self.assertEqual(sounddevice.calls[0][1]["device"], 2)
        with wave.open(io.BytesIO(content), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.readframes(wav.getnframes()), b"\x01\x00\x02\x00")

    def test_request_parameters_override_defaults(self):
        sounddevice = FakeSoundDevice()
        provider = SoundDeviceAudioProvider(sounddevice_module=sounddevice)
        request = CaptureRequest(
            "run-1", "point-1", "audio", "audio",
            parameters={"device": "3", "sample_rate": 48000, "channels": 2, "duration_seconds": 1.5},
        )

        provider.capture(request)

        frames, options = sounddevice.calls[0]
        self.assertEqual(frames, 72000)
        self.assertEqual(options["device"], 3)
        self.assertEqual(options["samplerate"], 48000)
        self.assertEqual(options["channels"], 2)

    def test_capture_error_is_wrapped(self):
        provider = SoundDeviceAudioProvider(sounddevice_module=FailingSoundDevice())
        request = CaptureRequest("run-1", "point-1", "audio", "audio")

        with self.assertRaisesRegex(AudioCaptureError, "input unavailable"):
            provider.capture(request)


if __name__ == "__main__":
    unittest.main()
