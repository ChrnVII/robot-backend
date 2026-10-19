"""SoundDeviceAudioProvider 流式采集单测（44.1 kHz / 单声道 / 10 s 冻结参数）。

FakeSoundDevice 直接模拟 PortAudio InputStream 语义：``with`` 进入/退出、
``read(n)`` 返回 ``(numpy 风格 buffer, overflowed)``。不依赖真实声卡，
也不依赖 numpy —— 缓冲用支持 ``tobytes()`` 的对象。
"""

import io
import struct
import unittest
import wave

from app.robot_core.contracts import CaptureRequest
from app.robot_core.sensing.audio import (
    AudioCaptureError,
    AudioSettings,
    SoundDeviceAudioProvider,
    simulate_audio_capture,
)


class FakeBuffer:
    """模拟 sounddevice 返回的 numpy 数组（只实现 tobytes）。"""

    def __init__(self, content: bytes):
        self.content = content

    def tobytes(self) -> bytes:
        return self.content


class FakeInputStream:
    def __init__(self, fake, samplerate, channels, dtype, device, blocksize):
        self.fake = fake
        self.params = {"samplerate": samplerate, "channels": channels, "dtype": dtype,
                       "device": device, "blocksize": blocksize}
        self.closed = False
        fake.opened.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def read(self, frames):
        self.fake.reads.append(frames)
        if self.fake.error is not None:
            raise self.fake.error
        block = self.fake.block[: frames * 2 * self.params["channels"]]
        return FakeBuffer(block), self.fake.overflowed


class FakeSoundDevice:
    """模拟 sounddevice 模块：InputStream + 设备目录。"""

    def __init__(self, block=b"\x01\x00\x02\x00"):
        self.block = block
        self.opened = []
        self.reads = []
        self.overflowed = False
        self.error = None
        self.devices = [
            {"name": "HDA Intel PCH: ALC897 Analog (hw:0,0)", "max_input_channels": 2},
            {"name": "ATR USB microphone: Audio (hw:1,0)", "max_input_channels": 2},
        ]

    def query_devices(self):
        return self.devices

    def InputStream(self, samplerate, channels, dtype, device, blocksize):
        return FakeInputStream(self, samplerate, channels, dtype, device, blocksize)


class UnavailableSoundDevice(FakeSoundDevice):
    def InputStream(self, *args, **kwargs):
        raise OSError("device unavailable")


def capture_request(**parameters):
    return CaptureRequest("run-1", "point-1", "audio", "audio", parameters=parameters)


def wav_frames(content: bytes) -> bytes:
    with wave.open(io.BytesIO(content), "rb") as wav:
        return wav.readframes(wav.getnframes())


class AudioProviderTest(unittest.TestCase):
    def test_capture_returns_wav_with_frozen_defaults(self):
        # 每块 4410 帧 int16 —— 0.5 s = 22050 帧。
        block = b"\x10\x00\x20\x00" * 4410
        sounddevice = FakeSoundDevice(block=block)
        provider = SoundDeviceAudioProvider(
            AudioSettings(sample_rate=44100, channels=1, duration_seconds=0.5, block_frames=4410),
            sounddevice_module=sounddevice,
        )

        result = provider.capture(capture_request())

        stream = sounddevice.opened[0]
        self.assertTrue(stream.closed, "stream 必须随 with 关闭（旧程序靠 kill -9 绕设备占用）")
        self.assertEqual(stream.params["samplerate"], 44100)
        self.assertEqual(stream.params["channels"], 1)
        self.assertEqual(stream.params["dtype"], "int16")
        pcm = wav_frames(result.content)
        self.assertEqual(len(pcm), 22050 * 2)
        self.assertEqual(result.quality_state, "valid")
        self.assertEqual(result.metrics["sampleRate"], 44100)
        self.assertEqual(result.metrics["requestedSeconds"], 0.5)

    def test_device_resolved_by_name_substring(self):
        # 名单里能子串命中时，解析成对应索引（避免依赖 ALSA 的字符串匹配）。
        sounddevice = FakeSoundDevice(block=b"\x10\x00" * 4410)
        provider = SoundDeviceAudioProvider(
            AudioSettings(device="microphone", sample_rate=44100, channels=1,
                          duration_seconds=0.1, block_frames=4410),
            sounddevice_module=sounddevice,
        )

        result = provider.capture(capture_request())

        self.assertEqual(sounddevice.opened[0].params["device"], 1)
        self.assertEqual(result.metrics["device"], "ATR USB microphone: Audio (hw:1,0)")

    def test_device_name_delegates_to_portaudio(self):
        # 名单子串匹配不到（真机 "USB Audio" vs "ATR USB microphone: Audio"）时，
        # 必须把字符串原样交给 PortAudio/ALSA 解析 —— 绝不回退默认设备
        # （回退默认会静默录到板载声卡，产出无声 WAV）。
        sounddevice = FakeSoundDevice(block=b"\x10\x00" * 4410)
        provider = SoundDeviceAudioProvider(
            AudioSettings(device="USB Audio", sample_rate=44100, channels=1,
                          duration_seconds=0.1, block_frames=4410),
            sounddevice_module=sounddevice,
        )

        provider.capture(capture_request())

        self.assertEqual(sounddevice.opened[0].params["device"], "USB Audio")

    def test_silence_is_marked_invalid(self):
        # 整段数字零 → RMS 低于静音阈值，必须判 invalid（训练数据静音不可用）。
        sounddevice = FakeSoundDevice(block=b"\x00\x00\x00\x00" * 4410)
        provider = SoundDeviceAudioProvider(
            AudioSettings(sample_rate=44100, duration_seconds=0.1, block_frames=4410),
            sounddevice_module=sounddevice,
        )

        result = provider.capture(capture_request())

        self.assertEqual(result.quality_state, "invalid")

    def test_clipping_is_marked_degraded(self):
        # 全部样本满幅 → 削波占比超阈值 → degraded。
        block = struct.pack("<4410h", *([32767] * 4410))
        sounddevice = FakeSoundDevice(block=block)
        provider = SoundDeviceAudioProvider(
            AudioSettings(sample_rate=44100, duration_seconds=0.1, block_frames=4410,
                          clip_ratio_threshold=0.001),
            sounddevice_module=sounddevice,
        )

        result = provider.capture(capture_request())

        self.assertEqual(result.quality_state, "degraded")
        self.assertGreater(result.metrics["peakDbfs"], -0.1)

    def test_unavailable_device_wraps_error(self):
        provider = SoundDeviceAudioProvider(
            AudioSettings(max_retries=0), sounddevice_module=UnavailableSoundDevice()
        )

        with self.assertRaisesRegex(AudioCaptureError, "device unavailable"):
            provider.capture(capture_request())

    def test_retryable_error_is_retried_then_succeeds(self):
        sounddevice = FakeSoundDevice(block=b"\x10\x00" * 4410)
        provider = SoundDeviceAudioProvider(
            AudioSettings(sample_rate=44100, duration_seconds=0.1, block_frames=4410,
                          max_retries=1, retry_delay_seconds=0.0),
            sounddevice_module=sounddevice,
        )
        original = provider._capture_once
        calls = {"n": 0}

        def flaky(settings, stop_event, attempt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AudioCaptureError("audio read failed (transient)")
            return original(settings, stop_event, attempt)

        provider._capture_once = flaky
        result = provider.capture(capture_request())
        self.assertEqual(result.quality_state, "valid")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result.metrics["attempt"], 2)

    def test_request_parameters_override_defaults(self):
        sounddevice = FakeSoundDevice(block=b"\x10\x00" * 4410)
        provider = SoundDeviceAudioProvider(sounddevice_module=sounddevice)

        provider.capture(capture_request(sample_rate=48000, channels=1, duration_seconds=0.1))

        stream = sounddevice.opened[0]
        self.assertEqual(stream.params["samplerate"], 48000)

    def test_concurrent_capture_rejected(self):
        provider = SoundDeviceAudioProvider(
            AudioSettings(sample_rate=44100, duration_seconds=0.05),
            sounddevice_module=FakeSoundDevice(block=b"\x10\x00" * 4410),
        )
        provider._lock.acquire()
        try:
            with self.assertRaisesRegex(AudioCaptureError, "already in progress"):
                provider.capture(capture_request())
        finally:
            provider._lock.release()


class SimulatedCaptureTest(unittest.TestCase):
    def test_simulated_capture_writes_valid_silent_wav(self):
        # 旧实现把 "simulated:audio:..." 文本当 WAV 落盘，任何播放器都读不了。
        result = simulate_audio_capture(capture_request(duration_seconds=2.0))

        self.assertEqual(result.quality_state, "valid")
        self.assertTrue(result.metrics["simulated"])
        with wave.open(io.BytesIO(result.content), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getframerate(), 44100)
            self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), 2.0, places=2)
            self.assertEqual(wav.readframes(wav.getnframes()), b"\x00\x00" * wav.getnframes())


if __name__ == "__main__":
    unittest.main()
