"""独立音频录制：给「手动采集」提供开始/停止的连续录音。

为什么不复用巡检会话的长录音：`mission_runner` 里写的是**静音占位**
（`silence = b"\\x00\\x00" * ...`），只为让产物时长对得上目标时长，并不碰麦克风；
而且它只能从会话内部启动，要求先有 mission 定义并独占会话槽位。手动采集不该被这些绑住。

真正复用的是底层：`SoundDeviceAudioProvider.capture_stream()`（sounddevice / PortAudio，
按设备名子串匹配 ALSA 输入）+ `ContinuousWavWriter`（写标准 WAV）。
设备与采样参数取自 `configs/hardware.yaml` 的 `devices.audio`——那组值是按真机实测冻结的
（ATR USB 麦 / 44.1kHz / 单声道 / 增益 +16dB），不在这里再写一份常量。

落盘位置由调用方指定，且**只允许 `data/cams/` 下的任务目录**：
这样音频与视频同处一个任务目录，前端一个路径就能看到全部产物，云端同步也一次带走。
"""

from __future__ import annotations

import logging
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from app.common.audio_writers import ContinuousWavWriter
from app.common.exceptions import AppError
from app.robot_core.sensing.audio import (
    AudioCaptureError,
    AudioSettings,
    SoundDeviceAudioProvider,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "hardware.yaml"
#: 只允许写进巡检/采集任务目录，避免任意路径写文件。
ALLOWED_ROOT = REPO_ROOT / "data" / "cams"
#: 停止时等 writer 收尾的上限：超过就报错，不让请求线程无限等。
STOP_TIMEOUT_SECONDS = 15.0


def load_audio_settings(config_path: Path | None = None) -> AudioSettings:
    """从硬件配置读音频参数；读不到就用默认值（device=None → 系统默认输入）。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    values: dict[str, Any] = {}
    try:
        if path.is_file():
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            values = ((payload.get("devices") or {}).get("audio")) or {}
    except Exception as error:  # noqa: BLE001 - 配置坏了不该让接口 500
        logger.warning("read audio config failed (%s): %s", path, error)
    picked = {
        key: values[key]
        for key in ("device", "sample_rate", "channels", "block_frames")
        if values.get(key) is not None
    }
    try:
        return AudioSettings.from_mapping(picked)
    except ValueError as error:
        raise AppError(f"音频配置非法：{error}", code="AUDIO_CONFIG_INVALID", status_code=500) from error


def _resolve_directory(raw: str | Path) -> Path:
    """校验并规范化目标目录：必须存在、必须是目录、必须在 data/cams 之下。"""
    if not raw:
        raise AppError("缺少目标目录", code="INVALID_REQUEST", status_code=400)
    candidate = Path(str(raw))
    if not candidate.is_absolute():
        raise AppError("目标目录必须是绝对路径", code="INVALID_REQUEST", status_code=400)
    try:
        target = candidate.resolve(strict=True)
    except OSError as error:
        raise AppError("目标目录不存在", code="DIRECTORY_NOT_FOUND", status_code=404) from error
    if not target.is_dir():
        raise AppError("目标不是目录", code="INVALID_REQUEST", status_code=400)
    root = ALLOWED_ROOT.resolve()
    if target != root and root not in target.parents:
        raise AppError("只允许写入 data/cams 下的任务目录", code="FORBIDDEN", status_code=403)
    return target


def _create_session_directory(name: str = "手动采集") -> Path:
    """仅声音采集时由后端建目录，命名与巡检任务目录保持一致，便于按日期清理。"""
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo

    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:40] or "手动采集"
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y.%m.%d.%H%M%S.%f")
    root = ALLOWED_ROOT.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{stamp}-{safe}"
    target.mkdir(parents=True, exist_ok=False)
    return target


class AudioRecordingService:
    """进程内单例：同一时间只允许一段录音（麦克风是独占资源）。"""

    def __init__(
        self,
        config_path: Path | None = None,
        *,
        provider_factory: Callable[[AudioSettings], Any] | None = None,
    ) -> None:
        self._config_path = config_path
        self._provider_factory = provider_factory or (lambda settings: SoundDeviceAudioProvider(settings))
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    # -- 对外接口 ---------------------------------------------------------

    def start(self, directory: str | Path | None = None, *, name: str = "手动采集",
              duration_seconds: float = 0.0) -> dict[str, Any]:
        # 有视频时用巡检建好的任务目录（音频与视频同处一处）；只录声音时后端自己建一个。
        target = _resolve_directory(directory) if directory else _create_session_directory(name)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise AppError("已有一段录音在进行，请先停止", code="AUDIO_BUSY", status_code=409)
            path = target / "audio" / "session.wav"
            if path.exists():
                raise AppError(
                    f"该目录已有录音文件：{path.name}，换一次采集再录",
                    code="AUDIO_EXISTS",
                    status_code=409,
                )
            settings = load_audio_settings(self._config_path)
            state: dict[str, Any] = {
                "directory": str(target),
                "path": str(path),
                "sampleRate": settings.sample_rate,
                "channels": settings.channels,
                "startedAt": time.time(),
                "state": "starting",
                "bytes": 0,
                "durationSeconds": 0.0,
                "error": "",
                "metrics": None,
                "stopEvent": threading.Event(),
                "limitSeconds": max(0.0, float(duration_seconds or 0.0)),
            }
            self._state = state
            self._thread = threading.Thread(
                target=self._run,
                args=(settings, path, state, max(0.0, float(duration_seconds or 0.0))),
                daemon=True,
                name="audio-recording",
            )
            self._thread.start()
            return self._public(state)

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> dict[str, Any]:
        with self._lock:
            state = self._state
            thread = self._thread
            if state is None or thread is None:
                raise AppError("当前没有录音", code="AUDIO_IDLE", status_code=404)
            state["stopEvent"].set()
        thread.join(timeout)
        if thread.is_alive():
            raise AppError("停止录音超时，设备可能未响应", code="AUDIO_STOP_TIMEOUT", status_code=504)
        with self._lock:
            result = self._public(state)
            self._state = None
            self._thread = None
            return result

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            state = self._state
            if state is None:
                return None
            return self._public(state)

    # -- 内部实现 ---------------------------------------------------------

    def _run(self, settings: AudioSettings, path: Path, state: dict[str, Any],
             limit_seconds: float = 0.0) -> None:
        writer: ContinuousWavWriter | None = None
        try:
            # writer 放在线程里创建：设备打不开时就不会留下一个 44 字节的空 WAV。
            writer = ContinuousWavWriter(path, settings.sample_rate, settings.channels)
            state["state"] = "recording"
            provider = self._provider_factory(settings)
            result = provider.capture_stream(
                settings,
                continuous_writer=writer,
                stop_event=state["stopEvent"],
                # 0/None 表示不限；与视频任务同一个时长，避免视频停了音频还在写
                max_duration_seconds=limit_seconds or None,
            )
            state["metrics"] = result.get("metrics")
            continuous = result.get("continuous") or {}
            state["bytes"] = int(continuous.get("bytes") or 0)
            state["durationSeconds"] = float(continuous.get("durationSeconds") or 0.0)
            state["state"] = "completed"
        except AudioCaptureError as error:
            state["state"] = "failed"
            state["error"] = str(error)
            logger.warning("audio recording failed: %s", error)
        except Exception as error:  # noqa: BLE001 - 会话异常必须落进状态而不是吞掉
            state["state"] = "failed"
            state["error"] = str(error)
            logger.exception("audio recording crashed")
        finally:
            # capture_stream 正常路径会自己 close；异常路径可能还没关，
            # 这里兜底（close 是幂等的），否则句柄泄漏且 Windows 上删不掉空文件。
            if writer is not None:
                try:
                    writer.close()
                except Exception as error:  # noqa: BLE001
                    logger.warning("close audio writer failed: %s", error)
            if state["state"] == "failed":
                # 只删没写进任何数据的空文件；中途失败保留了数据的不删，留给现场判断
                _discard(path)
            state["finishedAt"] = time.time()
            # 侧车元数据：记录起止时间，后处理可用它与视频的 started_at 算偏移（见 CHANGELOG 限制 1）
            _write_metadata(path, state)

    @staticmethod
    def _public(state: dict[str, Any]) -> dict[str, Any]:
        elapsed = float(state.get("durationSeconds") or 0.0)
        if state["state"] in {"starting", "recording"}:
            elapsed = max(0.0, round(time.time() - float(state["startedAt"]), 3))
        return {
            "state": state["state"],
            "directory": state["directory"],
            "path": state["path"],
            "sampleRate": state["sampleRate"],
            "channels": state["channels"],
            "startedAt": state["startedAt"],
            # elapsedSeconds 供录制中展示；durationSeconds 是收尾后按实际字节算的时长
            "elapsedSeconds": elapsed,
            "durationSeconds": float(state.get("durationSeconds") or 0.0),
            "bytes": int(state.get("bytes") or 0),
            "error": state.get("error") or "",
        }


def _discard(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size <= 44:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_metadata(path: Path, state: dict[str, Any]) -> None:
    """写 `audio/recording.json`：录音的起止时间与参数。

    它不为界面服务，而是给后处理用：视频任务的 `started_at` 与这里的 `startedAt`
    都在同一台机器上取，两者的差值就是这次采集的音视频偏移量，可以据此对齐。
    """
    payload = {
        "startedAt": state.get("startedAt"),
        "finishedAt": state.get("finishedAt"),
        "durationSeconds": state.get("durationSeconds") or 0.0,
        "sampleRate": state.get("sampleRate"),
        "channels": state.get("channels"),
        "bytes": state.get("bytes") or 0,
        "state": state.get("state"),
        "error": state.get("error") or "",
        "limitSeconds": state.get("limitSeconds") or 0.0,
    }
    try:
        target = path.with_name("recording.json")
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    except OSError as error:
        logger.warning("write audio metadata failed: %s", error)
