"""巡检任务执行器：一次采集会话（P1b，模拟优先）。

一期定位（方案 §4.2 / §12）：
- **位移、视频、音频源都是模拟的**，但目录、事件流、manifest、数据库记录全是真的；
- 换成真机时只替换 `_open_audio` / `_video_files`，会话编排与落库不动；
- 音频复用 P1a 的 `capture_stream` + 双 writer（长录音 + 分段），共用一次读取。

线程模型：会话跑在独立线程里（音频读取是阻塞的），DB 会话在子线程内新建，
不与请求线程共享 Session。进程内只允许一个活动会话（相机是独占资源）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.common.clock import utc_now_iso
from app.common.config import Settings
from app.common.exceptions import AppError, ValidationFailed
from app.db.database import get_session_factory
from app.db.models import Mission
from app.common.audio_writers import (
    ContinuousWavWriter,
    RunningAudioStats,
    SegmentedWavWriter,
)

logger = logging.getLogger(__name__)

AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 1
BLOCK_FRAMES = 4410          # 0.1 秒
BLOCK_SECONDS = BLOCK_FRAMES / AUDIO_SAMPLE_RATE

#: 磁盘预估用的粗算系数（只用于启动前拦截明显不够的情况，不做精确预测）
AUDIO_BYTES_PER_SECOND = AUDIO_SAMPLE_RATE * 2 * AUDIO_CHANNELS   # 秒/单份
VIDEO_BYTES_PER_SECOND = 512 * 1024                               # 约 4 Mbps/路
DISK_HEADROOM = 1.2

# data/cams/<时间戳>-<任务名>/，与 robot_core/inspection/paths.py 的约定保持一致。
# 这里不 import robot_core：架构约束禁止 services 依赖设备运行时（见 test_architecture_boundaries）。
# 两处命名规则如需调整必须同步，否则同一台机器会出现两种目录风格。
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"

try:
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 精简系统缺 tzdata 时退回固定偏移（中国无夏令时，等价）
    from datetime import timedelta, timezone

    _TZ = timezone(timedelta(hours=8))

#: 目录名（时间戳 + 任务名）的字节上限。
#: ext4 / xfs / btrfs 的单个文件名上限是 255 **字节**，不是字符；中文按 UTF-8 占 3 字节，
#: 80 个汉字就是 240 字节，加上 24 字节时间戳前缀会超限并抛 ENAMETOOLONG。
#: Windows 的 NTFS 按 255 个字符计，所以这个问题在开发机上永远暴露不出来。
DIR_NAME_BYTE_LIMIT = 200


def _task_directory(task_name: str) -> Path:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(task_name)).strip(" .")[:80] or "巡检"
    stamp = datetime.now(_TZ).strftime("%Y.%m.%d.%H%M%S.%f")
    prefix = f"{stamp}-"
    # 按 UTF-8 字节数收缩，而不是字符数
    while name and len((prefix + name).encode("utf-8")) > DIR_NAME_BYTE_LIMIT:
        name = name[:-1]
    return DATA_ROOT / "cams" / f"{prefix}{name or '巡检'}"


def _validate_cameras_configured(settings: Settings, definition: dict[str, Any]) -> None:
    """执行前校验通道真实存在。

    写任务时校验过一次，这里再校验一次：`:run` 的 overrides 可以在启动时改 cameras，
    绕过了写任务的校验路径。
    """
    cameras = (definition.get("video") or {}).get("cameras") or []
    if not cameras:
        return
    try:
        from app.services.cameras import CameraDirectoryService

        directory = CameraDirectoryService(settings)
        if not directory.configured:
            return
        known = set(directory.inspection_ids())
    except Exception as error:  # noqa: BLE001 - 读不到配置就不要挡住启动
        logger.debug("camera directory unavailable, skip validation: %s", error)
        return
    if not known:
        return
    missing = [str(item) for item in cameras if str(item) not in known]
    if missing:
        raise ValidationFailed(
            f"未配置的相机通道：{', '.join(missing)}；可用编号：{', '.join(sorted(known))}"
        )


def _render_placeholder_video(path: Path, seconds: float, camera_id: str) -> dict[str, Any] | None:
    """给模拟会话生成一个可播放的占位视频（纯色 + 很低帧率）。

    为什么需要：模拟模式下如果完全不产视频，任务详情页里"视频"组合点进去是空的，
    看起来像 bug。占位视频用 1/30 fps 编码，1 小时也只有 120 帧，CPU 可忽略。
    接真机后由录制器产出真实 MKV，此函数不再调用。
    """
    try:
        import av
        from fractions import Fraction
        import numpy as np

        path.parent.mkdir(parents=True, exist_ok=True)
        container = av.open(str(path), "w")
        try:
            stream = container.add_stream("mpeg4", rate=Fraction(1, 30))
            stream.width, stream.height = 320, 240
            stream.pix_fmt = "yuv420p"
            stream.bit_rate = 150_000
            index = int(camera_id[-2:] or 1) if camera_id[-2:].isdigit() else 1
            fill = ((index * 37) % 200 + 30, (index * 61) % 200 + 30, (index * 23) % 200 + 30)
            frames = max(1, int(seconds / 30))
            for _ in range(frames):
                image = np.full((240, 320, 3), fill, dtype=np.uint8)
                for packet in stream.encode(av.VideoFrame.from_ndarray(image, format="rgb24")):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        finally:
            container.close()
        return {"path": str(path), "cameraId": camera_id,
                "size": path.stat().st_size, "simulated": True}
    except Exception as error:  # noqa: BLE001 - 占位视频失败不应让会话失败
        logger.warning("placeholder video failed (%s): %s", camera_id, error)
        return None

STATE_QUEUED = "queued"
STATE_EXECUTING = "executing"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_ABORTED = "aborted"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _Session:
    """一次会话的运行态；只被执行线程与停止接口访问。"""

    def __init__(self, *, mission_id: str, mission_name: str, definition: dict[str, Any],
                 task_run_id: str, robot_id: str, speed: float, directory: Path,
                 trace_id: str = "") -> None:
        self.mission_id = mission_id
        self.mission_name = mission_name
        self.definition = definition
        self.task_run_id = task_run_id
        self.robot_id = robot_id
        self.speed = max(0.1, float(speed or 1.0))
        self.directory = directory
        self.trace_id = trace_id or f"trace-{uuid4().hex[:12]}"
        self.stop_event = threading.Event()
        self.stop_reason = ""
        self.started_at = utc_now_iso()
        self.state = STATE_QUEUED
        self.result: dict[str, Any] = {}
        self.error = ""

    def stop(self, reason: str) -> None:
        self.stop_reason = reason or "operator"
        self.stop_event.set()


class MissionRunner:
    """会话执行器。进程内单例，同时只跑一个会话。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._active: _Session | None = None

    # -- 对外接口 --------------------------------------------------------
    def start(self, mission_id: str, *, overrides: dict[str, Any] | None = None,
              speed: float = 1.0, resumed_from: str | None = None) -> dict[str, Any]:
        session_factory = get_session_factory()
        with session_factory() as db:
            mission = db.get(Mission, mission_id)
            if mission is None:
                raise AppError(f"任务不存在：{mission_id}", code="MISSION_NOT_FOUND", status_code=404)
            if mission.state != "enabled":
                raise ValidationFailed(f"任务当前状态为 {mission.state}，只有启用状态可以执行")
            definition = self._merge_overrides(mission, overrides or {})
            _validate_cameras_configured(self._settings, definition)

        with self._lock:
            # 只拦真正在采的会话。已进入终态、但收尾（资产登记要为每个产物算 sha256）
            # 还没跑完的会话不该拦住下一个任务——否则界面上刚显示"已完成"，
            # 用户立刻点下一个任务却报"已有会话在运行"。旧会话的 finally
            # 会检查 `self._active is session` 再清理，不会误伤新会话。
            if (self._active is not None
                    and not self._active.stop_event.is_set()
                    and self._active.state in (STATE_QUEUED, STATE_EXECUTING)):
                raise ValidationFailed(f"已有会话在运行（{self._active.task_run_id}），请先结束")

            run_id = f"run-{uuid4().hex[:16]}"
            directory = _task_directory(mission.name)
            directory.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_disk_space(directory.parent, definition)
            if directory.exists():
                raise ValidationFailed(f"任务目录已存在：{directory}")
            directory.mkdir(parents=True, exist_ok=False)

            session = _Session(
                mission_id=mission_id, mission_name=mission.name, definition=definition,
                task_run_id=run_id, robot_id=getattr(self._settings, "robot_id", ""),
                speed=speed, directory=directory,
            )
            if resumed_from:
                session.definition = {**session.definition, "resumedFrom": resumed_from}
            self._active = session
            self._insert_run(session)

        thread = threading.Thread(target=self._run, args=(session,), name=f"mission-{run_id}", daemon=True)
        thread.start()
        return {"taskRunId": run_id, "missionId": mission_id, "state": STATE_QUEUED,
                "directory": str(directory), "speed": session.speed}

    def stop(self, task_run_id: str, reason: str = "operator") -> dict[str, Any]:
        with self._lock:
            session = self._active
        if session is None or session.task_run_id != task_run_id:
            raise AppError(f"会话不在运行中：{task_run_id}", code="RUN_NOT_ACTIVE", status_code=404)
        session.stop(reason)
        return {"taskRunId": task_run_id, "state": "stopping", "reason": session.stop_reason}

    def status(self, task_run_id: str) -> dict[str, Any]:
        session = self._active
        if session is not None and session.task_run_id == task_run_id:
            return {
                "taskRunId": task_run_id,
                "state": session.state,
                "startedAt": session.started_at,
                "stopReason": session.stop_reason,
                "directory": str(session.directory),
                "result": session.result,
                "error": session.error,
            }
        return self._status_from_db(task_run_id)

    # -- 内部 ------------------------------------------------------------
    def _ensure_disk_space(self, parent: Path, definition: dict[str, Any]) -> None:
        """启动前拦一次明显不够的情况；运行中不重复检查（避免每块一次 IO）。

        预估口径：音频按单份 88200 B/s × 份数，视频按 512 KB/s × 路数，
        再乘 1.2 的余量。只用于拦截"明显写不下"，不追求精确。
        """
        seconds = float(definition.get("durationSeconds") or 0) or float(
            definition.get("maxDurationSeconds") or 0)
        audio = definition.get("audio") or {}
        copies = 0
        if audio.get("enabled"):
            copies = int(bool((audio.get("continuous") or {}).get("enabled"))) \
                + int(bool((audio.get("segments") or {}).get("enabled")))
        need = AUDIO_BYTES_PER_SECOND * copies * seconds
        video = definition.get("video") or {}
        if video.get("enabled"):
            need += VIDEO_BYTES_PER_SECOND * max(1, len(video.get("cameras") or [])) * seconds
        need = int(need * DISK_HEADROOM)
        try:
            free = shutil.disk_usage(str(parent)).free
        except OSError as error:  # noqa: BLE001 - 取不到就不拦，别把可用场景挡掉
            logger.warning("disk usage check skipped: %s", error)
            return
        if free < need:
            raise ValidationFailed(
                f"磁盘空间不足：预计需要 {need / 1024 / 1024:.0f} MB，"
                f"当前可用 {free / 1024 / 1024:.0f} MB"
            )

    def _merge_overrides(self, mission: Mission, overrides: dict[str, Any]) -> dict[str, Any]:
        try:
            definition = json.loads(mission.definition or "{}")
        except json.JSONDecodeError:
            definition = {}
        # 只允许覆盖开关与时长，采集参数（采样率等）不在运行期改
        for key in ("video", "durationSeconds", "maxDurationSeconds"):
            if key in overrides and overrides[key] is not None:
                definition[key] = overrides[key]
        if isinstance(overrides.get("audio"), dict):
            current = dict(definition.get("audio") or {})
            for key in ("enabled", "channels", "sampleRate"):
                if key in overrides["audio"]:
                    current[key] = overrides["audio"][key]
            for key in ("continuous", "segments"):
                patch = overrides["audio"].get(key)
                if isinstance(patch, dict):
                    # 合并而不是整体替换：执行时只想改 enabled，
                    # 整体替换会把 segmentSeconds 一起冲掉、回落到默认 60 秒。
                    merged = dict(current.get(key) or {})
                    merged.update(patch)
                    current[key] = merged
            definition["audio"] = current
        return definition

    def _run(self, session: _Session) -> None:
        produced: list[str] = []
        continuous_writer: ContinuousWavWriter | None = None
        segment_writer: SegmentedWavWriter | None = None
        stats = RunningAudioStats()
        video_on = bool((session.definition.get("video") or {}).get("enabled"))
        audio_cfg = session.definition.get("audio") or {}
        # 子开关只在总开关打开时生效：关掉"录音"就必须完全不碰麦克风
        audio_enabled = bool(audio_cfg.get("enabled"))
        continuous_on = audio_enabled and bool((audio_cfg.get("continuous") or {}).get("enabled"))
        segments_on = audio_enabled and bool((audio_cfg.get("segments") or {}).get("enabled"))
        segment_seconds = int((audio_cfg.get("segments") or {}).get("segmentSeconds") or 60)
        duration = int(session.definition.get("durationSeconds") or 0)
        max_duration = int(session.definition.get("maxDurationSeconds") or 3600)
        target_seconds = duration if duration > 0 else max_duration

        started = time.monotonic()
        stopped_by = "completed"
        blocks = 0

        try:
            self._set_state(session, STATE_EXECUTING)
            self._append_event(session, "started", {
                "missionId": session.mission_id, "video": video_on,
                "continuous": continuous_on, "segments": segments_on,
                "targetSeconds": target_seconds,
            })

            if continuous_on:
                continuous_writer = ContinuousWavWriter(
                    session.directory / "audio" / "continuous" / "session.wav",
                    AUDIO_SAMPLE_RATE, AUDIO_CHANNELS,
                )
                produced.append("audio/continuous/session.wav")
            if segments_on:
                segment_writer = SegmentedWavWriter(
                    session.directory / "audio" / "segments",
                    AUDIO_SAMPLE_RATE, AUDIO_CHANNELS, segment_seconds=segment_seconds,
                )

            silence = b"\x00\x00" * (BLOCK_FRAMES * AUDIO_CHANNELS)
            # 用块数而不是墙钟推进：sleep 精度在不同平台上差异很大，
            # 按墙钟判断会导致"虚拟时间已到、音频还没录够"，产物时长对不上目标时长。
            target_blocks = max(1, int(round(target_seconds / BLOCK_SECONDS)))
            # 每轮写入的块数随加速倍数放大，使每轮 sleep 始终 >= ~20ms。
            # 直接按单块 sleep(BLOCK_SECONDS/speed) 在高倍速下会退化成
            # sleep(0.3ms)，而 Windows 定时精度只有 ~15ms，实测把 12 秒的任务
            # 拖成 9 分钟以上——会话卡在 executing，后续所有任务都启动不了。
            blocks_per_round = max(1, math.ceil(0.2 * session.speed))
            blocks = 0
            last_report = time.monotonic()
            while blocks < target_blocks and not session.stop_event.is_set():
                step = min(blocks_per_round, target_blocks - blocks)
                if continuous_writer is not None or segment_writer is not None:
                    payload = silence * step
                    stats.update(payload)
                    if continuous_writer is not None:
                        continuous_writer.feed(payload)
                    if segment_writer is not None:
                        segment_writer.feed(payload)
                blocks += step

                if time.monotonic() - last_report >= 2.0:
                    last_report = time.monotonic()
                    self._report_progress(session, blocks, blocks * BLOCK_SECONDS)
                    self._append_event(session, "progress", {
                        "blocks": blocks, "elapsedSeconds": round(blocks * BLOCK_SECONDS, 1),
                    })

                session.stop_event.wait(step * BLOCK_SECONDS / session.speed)

            if session.stop_event.is_set():
                # 区分"人点的结束"和"服务停机"，否则事后看 manifest 无法判断
                # 那次是不是因为重启被中断的
                stopped_by = "shutdown" if session.stop_reason == "shutdown" else "operator"
            else:
                stopped_by = "duration" if duration > 0 else "max_duration"

        except Exception as error:  # noqa: BLE001 - 会话异常必须落到状态里而不是吞掉
            session.error = str(error)
            stopped_by = "error"
            logger.exception("mission session failed: %s", session.task_run_id)
        finally:
            # 收尾（落盘 / 落库 / 登记）任何一步失败都不能让槽位泄漏：
            # 泄漏一次，进程内就再也启动不了新会话，只能重启服务。
            try:
                continuous_result = None
                segment_result = None
                if continuous_writer is not None:
                    try:
                        continuous_result = continuous_writer.close()
                    except Exception as error:  # noqa: BLE001
                        logger.warning("continuous writer close failed: %s", error)
                if segment_writer is not None:
                    try:
                        segment_result = segment_writer.close()
                        if segment_result.get("segments"):
                            produced.append("audio/segments")
                    except Exception as error:  # noqa: BLE001
                        logger.warning("segment writer close failed: %s", error)

                elapsed = (time.monotonic() - started) * session.speed
                # 会话虚拟时长：块数 × 每块秒数。不能拿音频统计的时长当会话时长——
                # 只录视频时没有任何音频 writer，统计值恒为 0，会让 manifest 记成 0 秒、
                # 占位视频只生成 1 帧。
                virtual_seconds = blocks * BLOCK_SECONDS
                metrics = stats.metrics(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
                video_files: list[dict[str, Any]] = []
                if video_on:
                    for camera in (session.definition.get("video") or {}).get("cameras") or []:
                        item = _render_placeholder_video(
                            session.directory / "video" / f"{camera}.mkv", virtual_seconds, str(camera))
                        if item:
                            video_files.append(item)
                            produced.append(f"video/{camera}.mkv")
                alerts = self._evaluate_alerts(session, metrics, stopped_by, virtual_seconds)
                manifest = {
                    "schemaVersion": 1,
                    "missionId": session.mission_id,
                    "missionName": session.mission_name,
                    "taskRunId": session.task_run_id,
                    "robotId": session.robot_id,
                    "captureMode": "simulated",
                    # 记录加速倍数：模拟会话按 speed 倍速跑，durationSeconds 是"虚拟时长"，
                    # 与 startedAt→finishedAt 的墙钟跨度不一致。事后排查时要能分辨，
                    # 否则会误判成"任务没跑够时间"。
                    "timeAcceleration": session.speed,
                    "positionSource": session.definition.get("positionSource", "simulated"),
                    "startedAt": session.started_at,
                    "finishedAt": utc_now_iso(),
                    "durationSeconds": round(virtual_seconds, 3),
                    "audioDurationSeconds": round(float(metrics["durationSeconds"]), 3),
                    "stoppedBy": stopped_by,
                    "config": session.definition,
                    "audio": {"continuous": continuous_result, "segments": segment_result, "metrics": metrics},
                    "video": {"enabled": video_on, "files": video_files, "simulated": True,
                              "note": "占位视频：纯色、1/30 fps；接真机后由录制器产出真实 MKV"},
                    "produced": produced,
                    "alerts": alerts,
                }
                manifest_path = session.directory / "manifest.json"
                try:
                    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as error:  # noqa: BLE001
                    logger.warning("manifest write failed: %s", error)

                self._append_event(session, "finished", {"stoppedBy": stopped_by, "durationSeconds": manifest["durationSeconds"]})

                # 先把终态落库：资产登记要为每个产物算 sha256 + 单独 commit，
                # 一小时任务可能几十秒。放在前面能让界面立刻看到"已完成"，
                # 而不是盯着 executing 等资产入库。
                final_state = STATE_ABORTED if stopped_by in ("operator", "shutdown") else (
                    STATE_FAILED if stopped_by == "error" else STATE_COMPLETED)
                session.state = final_state
                session.result = manifest
                self._finalize_run(session, final_state, stopped_by, manifest)

                self._write_alerts(session, alerts)
                self._register_media_assets(session, continuous_result, segment_result, video_files)
            except Exception as error:  # noqa: BLE001 - 收尾失败也要留下可诊断的状态
                logger.exception("mission finalize failed: %s", session.task_run_id)
                session.error = session.error or str(error)
                try:
                    self._finalize_run(session, STATE_FAILED, "error", {})
                except Exception:  # noqa: BLE001
                    logger.warning("finalize update failed", exc_info=True)
            finally:
                with self._lock:
                    if self._active is session:
                        self._active = None

    def _report_progress(self, session: _Session, blocks: int, elapsed: float) -> None:
        try:
            with get_session_factory()() as db:
                db.execute(
                    text("UPDATE mission_runs SET current_step=:step, updated_at=:now WHERE task_run_id=:run"),
                    {"step": str(blocks), "now": utc_now_iso(), "run": session.task_run_id},
                )
                db.commit()
        except Exception as error:  # noqa: BLE001
            logger.debug("progress update skipped: %s", error)

    def _append_event(self, session: _Session, kind: str, payload: dict[str, Any]) -> None:
        record = {"at": utc_now_iso(), "event": kind, **payload}
        try:
            with (session.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as error:  # noqa: BLE001
            logger.debug("event write skipped: %s", error)

    def _insert_run(self, session: _Session) -> None:
        with get_session_factory()() as db:
            db.execute(text(
                "INSERT INTO mission_runs(task_run_id, mission_id, robot_id, trace_id, state,"
                " current_step, started_at, updated_at, params_json)"
                " VALUES (:run, :mission, :robot, :trace, :state, '0', :started, :started, :params)"
            ), {
                "run": session.task_run_id, "mission": session.mission_id,
                "robot": session.robot_id, "trace": session.trace_id,
                "state": STATE_QUEUED, "started": session.started_at,
                # artifactDir 让详情页能定位产物；用 JSON 存避免为它单开一列
                "params": json.dumps({**session.definition, "artifactDir": str(session.directory)},
                                     ensure_ascii=False),
            })
            db.commit()

    def _set_state(self, session: _Session, state: str) -> None:
        session.state = state
        try:
            with get_session_factory()() as db:
                db.execute(
                    text("UPDATE mission_runs SET state=:state, updated_at=:now WHERE task_run_id=:run"),
                    {"state": state, "now": utc_now_iso(), "run": session.task_run_id},
                )
                db.commit()
        except Exception as error:  # noqa: BLE001
            logger.warning("state update failed: %s", error)

    def _finalize_run(self, session: _Session, state: str, stopped_by: str, manifest: dict[str, Any]) -> None:
        try:
            with get_session_factory()() as db:
                db.execute(text(
                    "UPDATE mission_runs SET state=:state, finished_at=:finished, updated_at=:finished,"
                    " failure_code=:failure, params_json=:params WHERE task_run_id=:run"
                ), {
                    "state": state, "finished": utc_now_iso(),
                    "failure": "device_error" if stopped_by == "error" else None,
                    "params": json.dumps({**session.definition, "artifactDir": str(session.directory)},
                                         ensure_ascii=False),
                    "run": session.task_run_id,
                })
                db.commit()
        except Exception as error:  # noqa: BLE001
            logger.warning("finalize update failed: %s", error)

    def _register_media_assets(self, session: _Session, continuous: dict | None,
                               segments: dict | None,
                               video_files: list[dict[str, Any]] | None = None) -> None:
        """把产物登记进 media_assets；失败不阻断会话收尾。"""
        candidates: list[dict[str, Any]] = []
        if continuous and continuous.get("bytes"):
            candidates.append({
                "path": continuous["path"], "media_type": "audio",
                "device_id": "audio", "waypoint_id": "session",
                "mime": "audio/wav",
            })
        for item in (segments or {}).get("segments", [])[:200]:
            candidates.append({
                "path": item["path"], "media_type": "audio",
                "device_id": "audio", "waypoint_id": "segment",
                "mime": "audio/wav",
            })
        for item in video_files or []:
            candidates.append({
                "path": item["path"], "media_type": "video",
                "device_id": str(item.get("cameraId") or "camera"),
                "waypoint_id": "session", "mime": "video/x-matroska",
            })

        for item in candidates:
            path = Path(item["path"])
            if not path.exists():
                continue
            try:
                with get_session_factory()() as db:
                    db.execute(text(
                        "INSERT INTO media_assets(asset_id, robot_id, task_run_id, waypoint_id, device_id,"
                        " media_type, uri, sha256, size_bytes, mime_type, quality_state, cage_id,"
                        " captured_at, created_at)"
                        " VALUES (:asset, :robot, :run, :waypoint, :device, :type, :uri, :sha, :size,"
                        " :mime, 'valid', NULL, :captured, :created)"
                    ), {
                        "asset": f"asset-{uuid4().hex[:16]}", "robot": session.robot_id,
                        "run": session.task_run_id, "waypoint": item["waypoint_id"],
                        "device": item["device_id"], "type": item["media_type"],
                        "uri": path.resolve().as_uri(), "sha": _sha256_of(path),
                        "size": path.stat().st_size, "mime": item["mime"],
                        "captured": session.started_at, "created": utc_now_iso(),
                    })
                    db.commit()
            except Exception as error:  # noqa: BLE001
                logger.warning("media asset register failed (%s): %s", path.name, error)

    # -- 告警（P4） ------------------------------------------------------
    def _evaluate_alerts(self, session: _Session, metrics: dict[str, Any],
                         stopped_by: str, virtual_seconds: float = 0.0) -> list[dict[str, Any]]:
        """采集结束后判断"这次采到的东西能不能用"。

        不做模型级异常声音判定（那要等 inference 接入），只做采集质量与执行结果。
        """
        alerts: list[dict[str, Any]] = []
        if stopped_by == "error":
            alerts.append({"severity": "critical", "type": "capture_failed",
                           "message": f"采集会话异常终止：{session.error or '未知错误'}"})
        if stopped_by == "operator":
            alerts.append({"severity": "info", "type": "operator_stopped",
                           "message": "会话被手动结束"})

        audio_seconds = float(metrics.get("durationSeconds") or 0)
        if audio_seconds > 0:
            rms = float(metrics.get("rmsDbfs") or -120)
            clipped = float(metrics.get("clippedRatio") or 0)
            if rms <= -50:
                alerts.append({"severity": "warning", "type": "audio_silent",
                               "message": f"录音电平过低（RMS {rms} dBFS），可能没采到有效声音"})
            if clipped > 0.01:
                alerts.append({"severity": "warning", "type": "audio_clipped",
                               "message": f"音频削波 {clipped * 100:.1f}%，建议调低采集增益"})

        # 时长检查用会话虚拟时长，与是否录音无关（仅视频任务也要能判断有没有跑够）
        target = float(session.definition.get("durationSeconds") or 0) or float(
            session.definition.get("maxDurationSeconds") or 0)
        # 手动结束本来就会短于目标时长，再报"时长不足"是误报
        if target and stopped_by != "operator" and virtual_seconds < target * 0.9:
            alerts.append({"severity": "warning", "type": "audio_short",
                           "message": f"采集时长不足：目标 {target:.0f}s，实际 {virtual_seconds:.1f}s"})
        return alerts

    def _write_alerts(self, session: _Session, alerts: list[dict[str, Any]]) -> None:
        for item in alerts:
            try:
                with get_session_factory()() as db:
                    db.execute(text(
                        "INSERT INTO alerts(alert_id, trace_id, source_run_id, severity, state,"
                        " alert_type, evidence_asset_id, message, created_at)"
                        " VALUES (:alert, :trace, :run, :severity, 'open', :type, NULL, :message, :now)"
                    ), {
                        "alert": f"alert-{uuid4().hex[:16]}", "trace": session.trace_id,
                        "run": session.task_run_id, "severity": item["severity"],
                        "type": item.get("type"), "message": item["message"], "now": utc_now_iso(),
                    })
                    db.commit()
            except Exception as error:  # noqa: BLE001
                logger.warning("alert write failed: %s", error)

    # -- 恢复（P2） ------------------------------------------------------
    def recover_stale(self) -> int:
        """启动时把上次进程遗留的 queued/executing 记录标成 interrupted。

        不做这一步的话，进程重启后这些记录会永远停在"执行中"，
        界面上看是活的、实际早死了。
        """
        try:
            with get_session_factory()() as db:
                result = db.execute(text(
                    "UPDATE mission_runs SET state='interrupted', failure_code='process_restart',"
                    " updated_at=:now WHERE state IN ('queued','executing')"
                ), {"now": utc_now_iso()})
                db.commit()
                count = result.rowcount or 0
            if count:
                logger.warning("recovered %s stale mission runs as interrupted", count)
            return count
        except Exception as error:  # noqa: BLE001
            logger.warning("recover stale runs failed: %s", error)
            return 0

    def resume(self, task_run_id: str, *, speed: float = 1.0) -> dict[str, Any]:
        """中断/失败后重跑一次。

        语义说明：连续音频无法"接着写"（WAV 头只能 close 一次），
        所以续跑是**新开一次会话**，用 resumedFrom 保留来源关系；
        旧目录原样保留，不覆盖、不追加。
        """
        with get_session_factory()() as db:
            row = db.execute(text(
                "SELECT mission_id, state FROM mission_runs WHERE task_run_id=:run"
            ), {"run": task_run_id}).mappings().first()
        if row is None:
            raise AppError(f"运行记录不存在：{task_run_id}", code="RUN_NOT_FOUND", status_code=404)
        if row["state"] not in ("interrupted", "failed", "aborted"):
            raise ValidationFailed(f"当前状态为 {row['state']}，只有中断/失败/中止的运行需要续跑")
        return self.start(row["mission_id"], speed=speed, resumed_from=task_run_id)

    def shutdown(self, timeout: float = 10.0) -> bool:
        """进程退出前主动结束会话，给收尾留时间。

        为什么必须有：采集线程是 daemon，进程收到 SIGTERM 后会被直接终止。
        WAV 头只在 close() 时回填、manifest 只在 finally 里写、数据库状态也要更新，
        线程被强杀就等于这三样全丢，只剩一堆"文件在、放不出来"的产物。
        在 Linux 上 ``systemctl restart backend`` 是最常见的操作，必然踩到。

        返回 True 表示已干净收尾，False 表示超时仍未结束。
        """
        with self._lock:
            session = self._active
        if session is None:
            return True
        logger.info("stopping active mission session before shutdown: %s", session.task_run_id)
        session.stop("shutdown")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._active is None:
                    return True
            time.sleep(0.1)
        logger.warning("mission session did not finish within %.0fs: %s", timeout, session.task_run_id)
        return False

    def _status_from_db(self, task_run_id: str) -> dict[str, Any]:
        with get_session_factory()() as db:
            row = db.execute(text(
                "SELECT task_run_id, mission_id, state, current_step, started_at, finished_at,"
                " failure_code, params_json FROM mission_runs WHERE task_run_id=:run"
            ), {"run": task_run_id}).mappings().first()
        if row is None:
            raise AppError(f"运行记录不存在：{task_run_id}", code="RUN_NOT_FOUND", status_code=404)
        return {
            "taskRunId": row["task_run_id"], "missionId": row["mission_id"],
            "state": row["state"], "currentStep": row["current_step"],
            "startedAt": row["started_at"], "finishedAt": row["finished_at"],
            "failureCode": row["failure_code"],
        }


_runner: MissionRunner | None = None
_runner_lock = threading.Lock()


def get_mission_runner(settings: Settings) -> MissionRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = MissionRunner(settings)
        return _runner
