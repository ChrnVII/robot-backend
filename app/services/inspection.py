"""巡检服务：任务启动/停止/状态、检测分页、证据图片读取。

迁移来源：InspectionApplicationService.java、InferenceQueryService.java。
- InspectionService：经 inspection-worker 网关（IPC）；
- InferenceQueryService：只读 robot_platform 库的推理记录（数据库不可用时返回空页，
  与旧文档「mock 模式检测列表为空」一致）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from sqlalchemy import create_engine, text

from app.common.dburl import require_postgres_url
from app.gateways.base import camelize
from app.gateways.inspection_worker import InspectionWorkerGateway
from app.schemas.inspection import StartInspectionRequest, StartVideoStreamRequest


@dataclass(frozen=True)
class ResolvedImage:
    path: Path
    content_type: str


class InspectionService:
    def __init__(self, gateway: InspectionWorkerGateway) -> None:
        self._gateway = gateway

    async def start(self, request: StartInspectionRequest) -> dict:
        value: dict[str, Any] = {
            "operation": "start",
            "mode": request.mode,
            "pipeline": request.pipeline,
            "duration_seconds": request.durationSeconds,
        }
        if request.taskRunId is not None:
            value["task_run_id"] = request.taskRunId
        if request.cameraIds:
            value["camera_ids"] = list(request.cameraIds)
        return camelize(await self._gateway.call(value))

    async def start_video_stream(self, request: StartVideoStreamRequest) -> dict:
        value: dict[str, Any] = {
            "operation": "start",
            "mode": request.mode,
            "pipeline": "passthrough",
            "duration_seconds": request.durationSeconds,
        }
        if request.streamId is not None:
            value["task_run_id"] = request.streamId
        if request.cameraIds:
            value["camera_ids"] = list(request.cameraIds)
        return camelize(await self._gateway.call(value))

    async def stop(self, task_run_id: str) -> dict:
        return camelize(await self._gateway.call({"operation": "stop", "task_run_id": task_run_id}))

    async def status(self, task_run_id: str) -> dict:
        return camelize(await self._gateway.call({"operation": "status", "task_run_id": task_run_id}))

    async def detections(self, task_run_id: str, offset: int, limit: int) -> dict:
        return camelize(await self._gateway.call({
            "operation": "detections", "task_run_id": task_run_id, "offset": offset, "limit": limit,
        }))

    async def latest_frame(self, task_run_id: str, camera_id: str | None = None) -> dict:
        request: dict[str, Any] = {"operation": "latest_frame", "task_run_id": task_run_id}
        if camera_id:
            request["camera_id"] = camera_id
        return camelize(await self._gateway.call(request))

    async def latest_frames(self, task_run_id: str) -> dict:
        return camelize(await self._gateway.call({"operation": "latest_frames", "task_run_id": task_run_id}))

    async def resolve_image(self, task_run_id: str, image_id: str) -> ResolvedImage:
        value = await self._gateway.call({
            "operation": "resolve_image", "task_run_id": task_run_id, "image_id": image_id,
        })
        return ResolvedImage(
            path=Path(str(value.get("path"))).absolute(),
            content_type=str(value.get("content_type", "image/jpeg")),
        )


_RESULT_SQL = text(
    """
    SELECT ir.run_id, ir.request_id, ir.task_run_id, ir.trace_id, ir.capability,
           ir.model_version, ir.model_sha256, ir.status, ir.result,
           ir.started_at, ir.finished_at, ma.asset_id, ma.uri, ma.mime_type,
           ma.sha256, ma.size_bytes, ma.captured_at,
           ma2.asset_id AS result_asset_id, ma2.uri AS result_uri,
           ma2.mime_type AS result_mime_type
    FROM inference_runs ir
    JOIN media_assets ma ON ma.asset_id = ir.asset_id
    LEFT JOIN media_assets ma2 ON ma2.task_run_id = ir.task_run_id
      AND ma2.device_id = ma.device_id AND ma2.media_type = 'image'
      AND ma2.uri LIKE '%_result.jpg' AND ma2.captured_at = ma.captured_at
    WHERE ir.task_run_id = :task_run_id
    ORDER BY ir.finished_at DESC, ir.run_id DESC
    LIMIT :limit OFFSET :offset
    """
)

_DETECTION_SQL = text(
    """
    SELECT detection_id, camera_id, frame_id, label, class_id, confidence,
           x0, y0, x1, y1, created_at
    FROM inference_detections WHERE request_id = :request_id ORDER BY detection_id
    """
)


class InferenceQueryService:
    """分页读取持久化的逐帧推理记录（迁移 InferenceQueryService.java）。"""

    def __init__(self, robot_database_url: str) -> None:
        self._url = robot_database_url
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            url = require_postgres_url(self._url, "ROBOT_ROBOT_DATABASE_URL")
            self._engine = create_engine(url, pool_pre_ping=True)
        return self._engine

    def dispose(self) -> None:
        """释放数据库连接（测试与关闭时调用）。"""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def results(self, task_run_id: str, offset: int, limit: int) -> dict[str, Any]:
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 200))
        try:
            with self._get_engine().connect() as connection:
                total = connection.execute(
                    text("SELECT count(*) FROM inference_runs WHERE task_run_id=:task_run_id"),
                    {"task_run_id": task_run_id},
                ).scalar() or 0
                rows = connection.execute(
                    _RESULT_SQL,
                    {"task_run_id": task_run_id, "offset": safe_offset, "limit": safe_limit},
                ).mappings().all()
                items = [self._to_public_item(connection, row) for row in rows]
        except Exception:  # noqa: BLE001 - 数据库缺失/未运行 robot-core 时返回空页
            return {"items": [], "offset": safe_offset, "limit": safe_limit, "total": 0}
        return {"items": items, "offset": safe_offset, "limit": safe_limit, "total": int(total)}

    def _to_public_item(self, connection, row) -> dict[str, Any]:
        request_id = str(row["request_id"])
        camera_id, frame_text = _parse_identity(request_id, row["task_run_id"])
        frame_id = _parse_int(frame_text)
        result = _parse_result(row["result"])
        item: dict[str, Any] = {
            "runId": row["run_id"],
            "requestId": request_id,
            "taskRunId": row["task_run_id"],
            "traceId": row["trace_id"],
            "capability": row["capability"],
            "modelVersion": row["model_version"],
            "modelSha256": row["model_sha256"],
            "status": row["status"],
            "startedAt": row["started_at"],
            "finishedAt": row["finished_at"],
            "capturedAt": row["captured_at"],
            "cameraId": camera_id,
            "frameId": frame_id,
            "latencyMs": _number(result.get("latency_ms"), result.get("latencyMs")),
            "detections": self._detections(connection, request_id),
            "countByLabel": result.get("count_by_label", result.get("countByLabel", {})),
            "result": camelize(result),
            "assetId": row["asset_id"],
            "resultAssetId": row["result_asset_id"],
            "originalImageId": _image_id(camera_id, row["uri"], frame_id, "_original.jpg"),
            "resultImageId": _image_id(camera_id, row["result_uri"], frame_id, "_result.jpg"),
            "mimeType": row["mime_type"],
            "resultMimeType": row["result_mime_type"],
            "uri": row["uri"],
            "resultUri": row["result_uri"],
            "sha256": row["sha256"],
            "sizeBytes": row["size_bytes"],
        }
        return item

    @staticmethod
    def _detections(connection, request_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(_DETECTION_SQL, {"request_id": request_id}).mappings().all()
        return [
            {
                "detectionId": row["detection_id"],
                "cameraId": row["camera_id"],
                "frameId": row["frame_id"],
                "label": row["label"],
                "classId": row["class_id"],
                "confidence": row["confidence"],
                "x0": row["x0"], "y0": row["y0"], "x1": row["x1"], "y1": row["y1"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]


def _parse_identity(request_id: str, task_run_id: str | None) -> tuple[str, str]:
    prefix = f"{task_run_id}:" if task_run_id else ""
    if request_id and request_id.startswith(prefix):
        rest = request_id[len(prefix):]
        separator = rest.rfind(":")
        if separator > 0:
            return rest[:separator], rest[separator + 1:]
    return "camera", "0"


def _parse_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _number(first: Any, second: Any) -> Any:
    value = first if first is not None else second
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except ValueError:
        return None


def _parse_result(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return dict(parsed) if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _image_id(camera_id: str, uri: Any, frame_id: int, suffix: str) -> str:
    filename = _file_name(uri)
    if not filename:
        filename = f"{frame_id:08d}{suffix}"
    return f"{camera_id}:{filename}"


def _file_name(uri: Any) -> str | None:
    if not uri:
        return None
    value = str(uri)
    try:
        if "://" in value:
            parsed = urlparse(value)
            if parsed.scheme == "file":
                return Path(url2pathname(parsed.path)).name or None
            return Path(parsed.path).name or None
        return Path(value).name or None
    except (ValueError, OSError):
        slash = max(value.rfind("/"), value.rfind("\\"))
        return value[slash + 1:] if slash >= 0 else value
