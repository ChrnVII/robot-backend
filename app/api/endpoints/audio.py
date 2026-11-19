"""独立音频录制：开始 / 停止 / 查询当前录音。

与「巡检会话里的长录音」的区别写在 `services/audio_recording.py` 顶部：
那条链路写的是静音占位，这里才是真麦克风采集（sounddevice → 标准 WAV）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_audio_recording_service
from app.common.responses import ok, trace_of
from app.schemas.audio import StartAudioRecordingRequest
from app.services.audio_recording import AudioRecordingService

router = APIRouter()


@router.post("/audio/recordings", tags=["audio"])
def start_audio_recording(
    request: Request,
    body: StartAudioRecordingRequest,
    service: AudioRecordingService = Depends(get_audio_recording_service),
) -> dict:
    """开始录音。已经有录音在进行时返回 409 `AUDIO_BUSY`（麦克风独占）。"""
    return ok(service.start(body.directory), trace_of(request))


@router.post("/audio/recordings/stop", tags=["audio"])
def stop_audio_recording(
    request: Request,
    service: AudioRecordingService = Depends(get_audio_recording_service),
) -> dict:
    """停止并收尾 WAV（写正确的头）。返回路径、字节数与实际时长。"""
    return ok(service.stop(), trace_of(request))


@router.get("/audio/recordings/active", tags=["audio"])
def active_audio_recording(
    request: Request,
    service: AudioRecordingService = Depends(get_audio_recording_service),
) -> dict:
    """当前录音状态；`data` 为 null 表示没有录音。"""
    return ok(service.active(), trace_of(request))
