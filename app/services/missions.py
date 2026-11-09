"""巡检任务（采集会话）服务。

职责边界：
- 只写 missions / mission_points，不碰 ui_tasks / ui_runs（那是旧工作台的影子表）
- 采集配置（视频 / 音频 / 时长）整体存进 missions.definition 的 JSON，不加列
- 校验放在服务层，接口层只做转发，保证以后 :run 复用同一套规则

关键业务规则（方案 §4.1.1）：
- 视频与音频各自独立，至少开一项，全关直接拒绝
- 音频开着但长录与分段都关，等于没开录音，按全关处理
- durationSeconds 为 0 表示不定时（靠手动结束），不得超过 maxDurationSeconds
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.common.clock import utc_now_iso
from app.common.exceptions import AppError, ValidationFailed
from app.db.models import Mission, MissionPoint
from app.services.paging import paginate

MISSION_STATES = ("draft", "enabled", "disabled", "archived")
DEFAULT_MAX_DURATION = 3600
MIN_SEGMENT_SECONDS = 10
MAX_SEGMENT_SECONDS = 60


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(value)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_definition(body: dict[str, Any]) -> dict[str, Any]:
    """把前端提交的采集配置规整并做业务校验，返回可落库的结构。"""
    video = body.get("video") or {}
    audio = body.get("audio") or {}

    video_enabled = _flag(video.get("enabled"), True)
    cameras = [str(item).strip() for item in (video.get("cameras") or []) if str(item).strip()]
    if video_enabled and not cameras:
        raise ValidationFailed("开启视频录像时至少需要选择一个相机通道")

    audio_enabled = _flag(audio.get("enabled"), True)
    continuous_enabled = _flag((audio.get("continuous") or {}).get("enabled"), True)
    segments_enabled = _flag((audio.get("segments") or {}).get("enabled"), True)
    audio_active = audio_enabled and (continuous_enabled or segments_enabled)

    if audio_enabled and not audio_active:
        raise ValidationFailed("已开启录音，但长录音与分段录音都关闭，请至少保留一项")
    if not video_enabled and not audio_active:
        raise ValidationFailed("请至少开启录音或录像")

    duration = _int(body.get("durationSeconds"), 0)
    max_duration = _int(body.get("maxDurationSeconds"), DEFAULT_MAX_DURATION)
    if duration < 0:
        raise ValidationFailed("设定时长不能为负数")
    if max_duration <= 0:
        raise ValidationFailed("安全上限必须大于 0")
    if duration > max_duration:
        raise ValidationFailed("设定时长不能超过安全上限")

    segment_seconds = _int((audio.get("segments") or {}).get("segmentSeconds"), MAX_SEGMENT_SECONDS)
    if segments_enabled and not (MIN_SEGMENT_SECONDS <= segment_seconds <= MAX_SEGMENT_SECONDS):
        raise ValidationFailed(
            f"分段时长必须在 {MIN_SEGMENT_SECONDS}~{MAX_SEGMENT_SECONDS} 秒之间（当前 {segment_seconds}）"
        )

    channels = _int(audio.get("channels"), 1)
    if channels < 1 or channels > 8:
        raise ValidationFailed("录音声道数必须在 1~8 之间")
    sample_rate = _int(audio.get("sampleRate"), 44100)
    if sample_rate < 8000 or sample_rate > 192000:
        raise ValidationFailed("采样率必须在 8000~192000 之间")

    infer = body.get("infer") or {}
    return {
        "video": {"enabled": video_enabled, "cameras": cameras},
        "audio": {
            "enabled": audio_enabled,
            "continuous": {"enabled": continuous_enabled},
            "segments": {"enabled": segments_enabled, "segmentSeconds": segment_seconds},
            "channels": channels,
            "sampleRate": sample_rate,
        },
        "durationSeconds": duration,
        "maxDurationSeconds": max_duration,
        "positionSource": str(body.get("positionSource") or "simulated"),
        "infer": {
            "enabled": _flag(infer.get("enabled"), False),
            "capability": str(infer.get("capability") or "heat_stress_detection"),
        },
    }


def capture_summary(definition: dict[str, Any]) -> str:
    """生成采集组合的中文摘要，用于列表页标签与单模态提示。"""
    video = (definition.get("video") or {}).get("enabled", False)
    audio = definition.get("audio") or {}
    continuous = (audio.get("continuous") or {}).get("enabled", False)
    segments = (audio.get("segments") or {}).get("enabled", False)
    audio_active = audio.get("enabled", False) and (continuous or segments)

    if video and audio_active:
        parts = ["视频"]
        if continuous:
            parts.append("长录")
        if segments:
            parts.append("分段")
        return "+".join(parts)
    if video:
        return "仅视频"
    if audio_active:
        parts = []
        if continuous:
            parts.append("长录")
        if segments:
            parts.append("分段")
        return "仅音频（" + "+".join(parts) + "）"
    return "未采集"


def _artifact_dir(params_json: str | None) -> str | None:
    """运行记录把产物目录放在 params_json 里（避免为它单开一列）。"""
    if not params_json:
        return None
    try:
        value = json.loads(params_json)
    except json.JSONDecodeError:
        return None
    directory = value.get("artifactDir") if isinstance(value, dict) else None
    return str(directory) if directory else None


def _progress_of(params_json: str | None, current_step: Any) -> dict[str, Any]:
    """把 current_step（已采集的 0.1 秒块数）换算成可展示的进度。

    放在服务端算而不是前端算：目标时长藏在 params_json 的采集配置里，
    前端再去解析一份容易和 normalize_definition 的口径走偏。
    """
    try:
        config = json.loads(params_json or "{}")
    except json.JSONDecodeError:
        config = {}
    if not isinstance(config, dict):
        config = {}

    duration = float(config.get("durationSeconds") or 0)
    target = duration if duration > 0 else float(config.get("maxDurationSeconds") or 0)
    try:
        blocks = int(current_step or 0)
    except (TypeError, ValueError):
        blocks = 0

    elapsed = blocks * 0.1          # 每块 0.1 秒，与执行器的 BLOCK_SECONDS 对齐
    percent = min(100.0, elapsed / target * 100) if target > 0 else 0.0
    return {
        "progressPercent": round(percent, 1),
        "elapsedSeconds": round(elapsed, 1),
        "targetSeconds": round(target, 1),
    }


def _category_of(relative: str) -> str:
    if relative.startswith("audio/"):
        return "audio"
    if relative.startswith("video/"):
        return "video"
    return "index"


def _mime_of(relative: str) -> str:
    suffix = Path(relative).suffix.lower()
    return {
        ".wav": "audio/wav",
        ".mkv": "video/x-matroska",
        ".mp4": "video/mp4",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
    }.get(suffix, "application/octet-stream")


def _to_public(row: Mission) -> dict[str, Any]:
    try:
        definition = json.loads(row.definition or "{}")
    except json.JSONDecodeError:
        definition = {}
    return {
        "missionId": row.mission_id,
        "name": row.name,
        "state": row.state,
        "revision": row.revision,
        "video": definition.get("video", {}),
        "audio": definition.get("audio", {}),
        "durationSeconds": definition.get("durationSeconds", 0),
        "maxDurationSeconds": definition.get("maxDurationSeconds", DEFAULT_MAX_DURATION),
        "positionSource": definition.get("positionSource", "simulated"),
        "infer": definition.get("infer", {}),
        "captureSummary": capture_summary(definition),
        "createdBy": row.created_by,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at or row.created_at,
    }


class MissionService:
    """巡检任务 CRUD。薄封装，逻辑集中在 normalize_definition。"""

    def __init__(self, session: Session, camera_directory: Any | None = None) -> None:
        self._session = session
        # 可选注入：有它就能校验相机通道是否真在本机配置里。
        # 不传时跳过校验（单测、无 YAML 环境），语义与 InspectionService 一致。
        self._cameras = camera_directory

    def _validate_cameras(self, definition: dict[str, Any]) -> None:
        """校验证任务选的通道在本机配置中存在。

        之前只校验"至少选一个"，不校验"选的是不是真的有一路"。模拟采集不碰设备，
        所以选 cam08 也能跑完、也能产出占位文件；一旦接真机就会在启动瞬间失败，
        且错误发生在采集线程里，界面只看到一个 failed，排查成本很高。
        """
        cameras = (definition.get("video") or {}).get("cameras") or []
        if not cameras or self._cameras is None:
            return
        if not getattr(self._cameras, "configured", False):
            return  # 没读到平台 YAML，不知道本机有哪几路，放行
        known = set(self._cameras.inspection_ids())
        if not known:
            return
        missing = [item for item in cameras if item not in known]
        if missing:
            raise ValidationFailed(
                f"未配置的相机通道：{', '.join(missing)}；可用编号：{', '.join(sorted(known))}"
            )

    def list_missions(
        self,
        *,
        page: int | None = None,
        page_size: int | None = None,
        keyword: str = "",
        state: str | None = None,
    ) -> Any:
        rows = [_to_public(row) for row in self._session.scalars(select(Mission).order_by(Mission.created_at.desc()))]
        text = (keyword or "").strip().lower()
        if text:
            rows = [
                row for row in rows
                if text in str(row["name"]).lower() or text in str(row["missionId"]).lower()
            ]
        if state:
            rows = [row for row in rows if row["state"] == state]
        return paginate(rows, page, page_size)

    def get_mission(self, mission_id: str) -> dict[str, Any]:
        return _to_public(self._require(mission_id))

    def create_mission(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValidationFailed("任务名称不能为空")
        state = str(body.get("state") or "enabled")
        if state not in MISSION_STATES:
            raise ValidationFailed(f"任务状态非法：{state}（可选 {'/'.join(MISSION_STATES)}）")

        definition = normalize_definition(body)
        self._validate_cameras(definition)
        now = utc_now_iso()
        row = Mission(
            mission_id=f"mission-{uuid4().hex[:12]}",
            name=name[:200],
            revision=1,
            definition=json.dumps(definition, ensure_ascii=False),
            state=state,
            created_by=str(body.get("createdBy") or "system")[:64],
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return _to_public(row)

    def update_mission(self, mission_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = self._require(mission_id)
        if row.state == "archived":
            raise ValidationFailed("已归档的任务不能修改")
        self._ensure_idle(mission_id)

        name = str(body.get("name") or row.name).strip()
        if not name:
            raise ValidationFailed("任务名称不能为空")
        state = str(body.get("state") or row.state)
        if state not in MISSION_STATES:
            raise ValidationFailed(f"任务状态非法：{state}")

        # 未提交的字段沿用旧定义，避免部分更新时把采集配置清空
        merged: dict[str, Any] = {}
        try:
            merged = json.loads(row.definition or "{}")
        except json.JSONDecodeError:
            merged = {}
        merged.update({key: value for key, value in body.items() if key in (
            "video", "audio", "durationSeconds", "maxDurationSeconds", "positionSource", "infer",
        )})
        definition = normalize_definition(merged)
        self._validate_cameras(definition)

        row.name = name[:200]
        row.state = state
        row.definition = json.dumps(definition, ensure_ascii=False)
        row.revision = int(row.revision or 1) + 1
        row.updated_at = utc_now_iso()
        self._session.commit()
        self._session.refresh(row)
        return _to_public(row)

    def archive_mission(self, mission_id: str) -> dict[str, Any]:
        """软删除：置为 archived，保留历史与运行记录的引用。"""
        row = self._require(mission_id)
        self._ensure_idle(mission_id)
        row.state = "archived"
        row.updated_at = utc_now_iso()
        self._session.commit()
        return {"missionId": mission_id, "state": row.state}

    def list_points(self, mission_id: str) -> list[dict[str, Any]]:
        self._require(mission_id)
        rows = self._session.scalars(
            select(MissionPoint).where(MissionPoint.mission_id == mission_id).order_by(MissionPoint.seq_no)
        )
        return [
            {"seqNo": row.seq_no, "cageId": row.cage_id, "missionPointId": row.mission_point_id}
            for row in rows
        ]

    # ---------- 详情与产物（P3） ----------
    def mission_detail(self, mission_id: str) -> dict[str, Any]:
        """任务 + 最近运行 + 最近一次的产物清单 + 告警，供详情页一次拿全。"""
        mission = self.get_mission(mission_id)
        runs = self._recent_runs(mission_id, limit=10)
        latest = runs[0] if runs else None
        artifacts = self._scan_artifacts(latest["artifactDir"]) if latest and latest.get("artifactDir") else None
        return {
            "mission": mission,
            "runs": runs,
            "latestArtifacts": artifacts,
            "alerts": self._alerts_for(mission_id),
        }

    def _alerts_for(self, mission_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._session.execute(
            text("SELECT a.alert_id, a.severity, a.state, a.message, a.created_at, a.source_run_id"
                 " FROM alerts a JOIN mission_runs r ON r.task_run_id = a.source_run_id"
                 " WHERE r.mission_id = :mission ORDER BY a.created_at DESC LIMIT :limit"),
            {"mission": mission_id, "limit": limit},
        ).mappings().all()
        return [
            {
                "alertId": row["alert_id"],
                "severity": row["severity"],
                "state": row["state"],
                "message": row["message"],
                "createdAt": row["created_at"],
                "taskRunId": row["source_run_id"],
            }
            for row in rows
        ]

    def run_artifacts(self, task_run_id: str) -> dict[str, Any]:
        run = self._run_row(task_run_id)
        directory = run.get("artifactDir")
        if not directory:
            raise AppError("该运行没有记录产物目录", code="ARTIFACT_NOT_FOUND", status_code=404)
        return {"taskRunId": task_run_id, "state": run["state"], **self._scan_artifacts(directory)}

    def resolve_artifact(self, task_run_id: str, relative_path: str) -> Path:
        """把相对路径解析成任务目录内的真实文件；越界一律拒绝。"""
        run = self._run_row(task_run_id)
        directory = run.get("artifactDir")
        if not directory:
            raise AppError("该运行没有记录产物目录", code="ARTIFACT_NOT_FOUND", status_code=404)
        base = Path(directory).resolve()
        target = (base / relative_path).resolve()
        if target != base and base not in target.parents:
            raise AppError("非法的文件路径", code="ARTIFACT_FORBIDDEN", status_code=400)
        if not target.is_file():
            raise AppError(f"文件不存在：{relative_path}", code="ARTIFACT_NOT_FOUND", status_code=404)
        return target

    def _recent_runs(self, mission_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._session.execute(
            text("SELECT task_run_id, state, started_at, finished_at, failure_code, params_json,"
                 " current_step"
                 " FROM mission_runs WHERE mission_id = :mission ORDER BY started_at DESC LIMIT :limit"),
            {"mission": mission_id, "limit": limit},
        ).mappings().all()
        recent: list[dict[str, Any]] = []
        for row in rows:
            progress = _progress_of(row["params_json"], row["current_step"])
            recent.append({
                "taskRunId": row["task_run_id"],
                "state": row["state"],
                "startedAt": row["started_at"],
                "finishedAt": row["finished_at"],
                "failureCode": row["failure_code"],
                "artifactDir": _artifact_dir(row["params_json"]),
                **progress,
            })
        return recent

    def _run_row(self, task_run_id: str) -> dict[str, Any]:
        row = self._session.execute(
            text("SELECT task_run_id, state, started_at, finished_at, params_json"
                 " FROM mission_runs WHERE task_run_id = :run"),
            {"run": task_run_id},
        ).mappings().first()
        if row is None:
            raise AppError(f"运行记录不存在：{task_run_id}", code="RUN_NOT_FOUND", status_code=404)
        return {
            "taskRunId": row["task_run_id"],
            "state": row["state"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "artifactDir": _artifact_dir(row["params_json"]),
        }

    @staticmethod
    def _scan_artifacts(directory: str) -> dict[str, Any]:
        base = Path(directory)
        if not base.is_dir():
            return {"directory": directory, "exists": False, "files": [], "manifest": None,
                    "totals": {"audio": 0, "video": 0, "bytes": 0}}
        files: list[dict[str, Any]] = []
        totals = {"audio": 0, "video": 0, "bytes": 0}
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
            size = path.stat().st_size
            category = _category_of(relative)
            if category in totals:
                totals[category] += 1
            totals["bytes"] += size
            files.append({
                "path": relative,
                "size": size,
                "category": category,
                "mime": _mime_of(relative),
            })
        manifest = None
        manifest_path = base / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = None
        return {"directory": str(base), "exists": True, "files": files,
                "manifest": manifest, "totals": totals}

    def _ensure_idle(self, mission_id: str) -> None:
        """有会话在跑时不允许改定义或归档：改了会让运行中的任务与配置对不上。"""
        row = self._session.execute(
            text("SELECT count(*) AS total FROM mission_runs"
                 " WHERE mission_id = :mission AND state IN ('queued','executing')"),
            {"mission": mission_id},
        ).mappings().first()
        if row and int(row["total"]) > 0:
            raise ValidationFailed("该任务有正在运行的会话，请先结束或等待完成后再操作")

    def _require(self, mission_id: str) -> Mission:
        row = self._session.get(Mission, mission_id)
        if row is None:
            raise AppError(f"任务不存在：{mission_id}", code="MISSION_NOT_FOUND", status_code=404)
        return row
