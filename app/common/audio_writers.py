"""任务型长录音的流式写出器。

为什么需要这套东西（方案 §4.4）：

原来的 ``_capture_once`` 把整段 PCM 攒在 ``chunks`` 里，最后 ``b"".join`` 一次性写盘，
内存随时长线性增长（1 小时约 318MB，``_analyse`` 还要再放大一倍），中途崩溃则全部丢失。
任务要跑几十分钟，必须改成"读一块、写一块"。

三个组件都吃同一份 PCM 块，**绝不重复打开设备**（PulseAudio 已 mask，ALSA 独占）：

- ``ContinuousWavWriter``：整段一个文件，持续 ``writeframes``，close 时由 wave 回填 RIFF/data 头
- ``SegmentedWavWriter``：按字节数切段（60s = 60 × rate × 2 × channels），每段独立 close 成完整 WAV
- ``RunningAudioStats``：chunk 级增量统计，口径与 ``_analyse`` 的全量算法**完全一致**（RMS 平方和可累加、峰值取最大、削波计数可累加）

崩溃语义：WAV 头只在 close 时回填，进程被 kill 会导致长录音头部时长错误；
分段文件每段都是完整可播的 WAV，因此可作为恢复兜底。这也是保留两份的实际价值。
"""

from __future__ import annotations

import array
import json
import math
import sys
import wave
from pathlib import Path
from typing import Any

_INT16_FULL_SCALE = 32768.0
_CLIP_THRESHOLD = 32767

try:  # numpy 随 av / opencv 一起装；缺了就退回纯 Python，功能不变只慢一些
    import numpy as _np
except ImportError:  # pragma: no cover - 精简环境
    _np = None


def _samples(pcm: bytes) -> array.array:
    usable = len(pcm) - (len(pcm) % 2)
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1.0) / _INT16_FULL_SCALE)


class RunningAudioStats:
    """增量质量统计：结果与全量 ``_analyse`` 一致，但内存恒定。"""

    def __init__(self) -> None:
        self._frames = 0
        self._peak = 0
        self._clipped = 0
        self._sum_squares = 0.0

    def update(self, pcm: bytes) -> None:
        """累加一块 PCM 的统计量。

        逐样本的纯 Python 循环在实时采集下没问题（44.1k 样本/秒），
        但任务型采集常以几十倍速跑压测，1 小时就是 1.6 亿个样本，
        纯循环会把会话拖到几分钟，看起来像卡死。有 numpy 时走向量化。
        """
        usable = len(pcm) - (len(pcm) % 2)
        if usable <= 0:
            return

        if _np is not None:
            samples = _np.frombuffer(pcm[:usable], dtype="<i2")
            if not samples.size:
                return
            magnitude = _np.abs(samples.astype(_np.int32))
            self._peak = max(self._peak, int(magnitude.max()))
            self._clipped += int((magnitude >= _CLIP_THRESHOLD).sum())
            values = samples.astype(_np.float64)
            self._sum_squares += float(_np.dot(values, values))
            self._frames += int(samples.size)
            return

        samples = _samples(pcm)
        if not samples:
            return
        peak = self._peak
        clipped = self._clipped
        total = self._sum_squares
        for value in samples:
            if value < 0:
                value = -value
            if value > peak:
                peak = value
            if value >= _CLIP_THRESHOLD:
                clipped += 1
            total += value * value
        self._peak = peak
        self._clipped = clipped
        self._sum_squares = total
        self._frames += len(samples)

    @property
    def frames(self) -> int:
        return self._frames

    def metrics(self, sample_rate: int, channels: int) -> dict[str, Any]:
        if self._frames <= 0:
            return {
                "frames": 0, "durationSeconds": 0.0,
                "peakDbfs": -120.0, "rmsDbfs": -120.0, "clippedRatio": 0.0,
            }
        rms = math.sqrt(self._sum_squares / self._frames)
        seconds = self._frames / float(sample_rate) / max(1, channels)
        return {
            "frames": self._frames,
            "durationSeconds": round(seconds, 3),
            "peakDbfs": round(_dbfs(self._peak), 2),
            "rmsDbfs": round(_dbfs(rms), 2),
            "clippedRatio": round(self._clipped / self._frames, 6),
        }


class ContinuousWavWriter:
    """整段连续录音：一次任务一个文件，随任务结束而 close。"""

    def __init__(self, path: str | Path, sample_rate: int, channels: int) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.channels = channels
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(channels)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sample_rate)
        self._bytes = 0
        self._closed = False

    def feed(self, pcm: bytes) -> None:
        if self._closed:
            raise RuntimeError("continuous writer already closed")
        if not pcm:
            return
        self._wav.writeframes(pcm)
        self._bytes += len(pcm)

    def flush(self) -> None:
        """把缓冲刷到磁盘；数据虽已写入，但 WAV 头仍要等 close 才正确。"""
        if self._closed:
            return
        handle = getattr(self._wav, "_file", None)
        if handle is not None and hasattr(handle, "flush"):
            handle.flush()

    def close(self) -> dict[str, Any]:
        if not self._closed:
            self._wav.close()
            self._closed = True
        seconds = self._bytes / float(2 * self.channels * self.sample_rate) if self._bytes else 0.0
        return {
            "path": str(self.path),
            "bytes": self._bytes,
            "durationSeconds": round(seconds, 3),
        }


class SegmentedWavWriter:
    """按字节数切段的录音：60 秒一段，每段是独立完整的 WAV。"""

    def __init__(
        self,
        directory: str | Path,
        sample_rate: int,
        channels: int,
        *,
        segment_seconds: int = 60,
        prefix: str = "seg",
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sample_rate = sample_rate
        self.channels = channels
        self.segment_seconds = max(1, int(segment_seconds))
        self._prefix = prefix
        # 只按字节数判断切点：音频流匀速，字节数即时间
        self._limit_bytes = self.segment_seconds * sample_rate * 2 * channels
        self._index = 0
        self._current: wave.Wave_write | None = None
        self._current_bytes = 0
        self._closed_seconds = 0.0
        self.segments: list[dict[str, Any]] = []

    def _open_next(self) -> None:
        self._index += 1
        path = self.directory / f"{self._prefix}-{self._index:04d}.wav"
        handle = wave.open(str(path), "wb")
        handle.setnchannels(self.channels)
        handle.setsampwidth(2)
        handle.setframerate(self.sample_rate)
        self._current = handle
        self._current_bytes = 0
        self._current_path = path

    def _close_current(self) -> None:
        if self._current is None:
            return
        self._current.close()
        seconds = self._current_bytes / float(2 * self.channels * self.sample_rate) if self._current_bytes else 0.0
        self.segments.append({
            "name": self._current_path.name,
            "path": str(self._current_path),
            "bytes": self._current_bytes,
            "durationSeconds": round(seconds, 3),
            "offsetSeconds": round(self._closed_seconds, 3),
        })
        self._closed_seconds += seconds
        self._current = None
        self._current_bytes = 0

    def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        view = memoryview(pcm)
        while view:
            if self._current is None:
                self._open_next()
            remaining = self._limit_bytes - self._current_bytes
            take = min(len(view), remaining)
            self._current.writeframes(view[:take])
            self._current_bytes += take
            view = view[take:]
            if self._current_bytes >= self._limit_bytes:
                self._close_current()

    def flush(self) -> None:
        if self._current is not None:
            handle = getattr(self._current, "_file", None)
            if handle is not None and hasattr(handle, "flush"):
                handle.flush()

    def close(self) -> dict[str, Any]:
        self._close_current()
        index_path = self.directory / "segments.index.json"
        payload = {
            "sampleRate": self.sample_rate,
            "channels": self.channels,
            "segmentSeconds": self.segment_seconds,
            "count": len(self.segments),
            "totalSeconds": round(self._closed_seconds, 3),
            "segments": self.segments,
        }
        index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["indexPath"] = str(index_path)
        return payload
