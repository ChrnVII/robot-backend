"""巡检服务：任务启动/停止/状态、检测分页、证据图片读取。

迁移来源：InspectionApplicationService.java、InferenceQueryService.java。
- InspectionService：经 inspection-worker 网关（IPC）；
- InferenceQueryService：通过只读视图查询推理记录，数据库故障使用统一错误封套；
  复用 app.db.database 的共享 engine，不再自建第二个连接池。
"""

from __future__ import annotations

import json
import base64
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Engine

from app.contracts import InspectionRequestWire, IpcErrorCode, Op, WireError
from app.common.exceptions import AppError, InspectionWorkerError, ValidationFailed
from app.db.database import get_engine
from app.gateways.base import camelize
from app.gateways.inspection_worker import InspectionWorkerGateway
from app.schemas.inspection import StartInspectionRequest, StartVideoStreamRequest
from app.services.cameras import CameraDirectoryService


@dataclass(frozen=True)
class ResolvedImage:
    path: Path
    content_type: str


class InspectionService:
    def __init__(self, gateway: InspectionWorkerGateway, directory: CameraDirectoryService) -> None:
        self._gateway = gateway
        self._directory = directory

    def _validate_cameras(self, camera_ids: list[str], mode: str) -> None:
        """提交前按相机目录校验编号，把引擎里的英文错误换成可读提示。

        未配置平台 YAML 时无法判断本机有哪几路，交给 inspection-worker 兜底。
        """
        if not self._directory.configured:
            return
        known = self._directory.inspection_ids()
        if camera_ids:
            missing = [camera_id for camera_id in camera_ids if camera_id not in known]
            if missing:
                raise ValidationFailed(
                    f"未配置的相机编号：{', '.join(missing)}；可用编号：{', '.join(known) or '（没有已启用的巡检相机）'}"
                )
            return
        expected = {"single": 1, "four": 4, "eight": 8}.get(mode)
        if expected and len(known) < expected:
            raise ValidationFailed(
                f"mode={mode} 需要 {expected} 路已启用的相机，当前配置只有 {len(known)} 路：{', '.join(known) or '无'}"
            )

    async def _call(self, request: dict) -> dict:
        try:
            payload = InspectionRequestWire.from_mapping(request).to_payload()
        except WireError as error:
            raise InspectionWorkerError(IpcErrorCode.INVALID_REQUEST, str(error)) from error
        return await self._gateway.call(payload)

    async def start(self, request: StartInspectionRequest) -> dict:
        self._validate_cameras(list(request.cameraIds), request.mode)
        value: dict[str, Any] = {
            "operation": Op.TASK_START,
            "mode": request.mode,
            "pipeline": request.pipeline,
            "duration_seconds": request.durationSeconds,
        }
        if request.taskRunId is not None:
            value["task_run_id"] = request.taskRunId
        if request.cameraIds:
            value["camera_ids"] = list(request.cameraIds)
        return camelize(await self._call(value))

    async def start_video_stream(self, request: StartVideoStreamRequest) -> dict:
        self._validate_cameras(list(request.cameraIds), request.mode)
        value: dict[str, Any] = {
            "operation": Op.TASK_START,
            "mode": request.mode,
            "pipeline": "passthrough",
            "duration_seconds": request.durationSeconds,
        }
        if request.streamId is not None:
            value["task_run_id"] = request.streamId
        if request.cameraIds:
            value["camera_ids"] = list(request.cameraIds)
        return camelize(await self._call(value))

    async def stop(self, task_run_id: str) -> dict:
        return camelize(await self._call({"operation": Op.TASK_STOP, "task_run_id": task_run_id}))

    async def status(self, task_run_id: str) -> dict:
        return camelize(await self._call({"operation": Op.TASK_STATUS, "task_run_id": task_run_id}))

    async def detections(self, task_run_id: str, offset: int, limit: int) -> dict:
        return camelize(await self._call({
            "operation": Op.TASK_DETECTIONS, "task_run_id": task_run_id, "offset": offset, "limit": limit,
        }))

    async def latest_frame(self, task_run_id: str, camera_id: str | None = None) -> dict:
        request: dict[str, Any] = {"operation": Op.TASK_LATEST_FRAME, "task_run_id": task_run_id}
        if camera_id:
            request["camera_id"] = camera_id
        value = await self._call(request)
        return camelize(await asyncio.to_thread(_render_frames, value))

    async def latest_frames(self, task_run_id: str) -> dict:
        value = await self._call({"operation": Op.TASK_LATEST_FRAMES, "task_run_id": task_run_id})
        return camelize(await asyncio.to_thread(_render_frames, value))

    async def snapshot(self, task_run_id: str, *, camera_id: str | None = None,
                       include_detections: bool = True) -> dict:
        request = {"operation": Op.TASK_SNAPSHOT, "task_run_id": task_run_id,
                   "include_detections": include_detections}
        if camera_id:
            request["camera_id"] = camera_id
        value = await self._call(request)
        return camelize(await asyncio.to_thread(_render_frames, value))

    async def resolve_image(self, task_run_id: str, image_id: str) -> ResolvedImage:
        value = await self._call({
            "operation": Op.TASK_RESOLVE_IMAGE, "task_run_id": task_run_id, "image_id": image_id,
        })
        return ResolvedImage(
            path=Path(str(value.get("path"))).absolute(),
            content_type=str(value.get("content_type", "image/jpeg")),
        )


_RESULT_SQL = text(
    """
    SELECT run_id, request_id, task_run_id, trace_id, capability,
           model_version, model_sha256, status, result,
           started_at, finished_at, asset_id, uri, mime_type,
           sha256, size_bytes, captured_at,
           result_asset_id, result_uri, result_mime_type
    FROM v_inference_run_results
    WHERE task_run_id = :task_run_id
    ORDER BY finished_at DESC, run_id DESC
    LIMIT :limit OFFSET :offset
    """
)

_DETECTION_SQL = text(
    """
    SELECT request_id, detection_id, camera_id, frame_id, label, class_id, confidence,
           x0, y0, x1, y1, created_at
    FROM v_inference_detections WHERE request_id IN :request_ids ORDER BY detection_id
    """
).bindparams(bindparam("request_ids", expanding=True))


def _render_frames(value: Any, cache: dict[str, str] | None = None) -> Any:
    """Keep the public Base64 contract while internal IPC transfers small references."""
    cache = {} if cache is None else cache
    if isinstance(value, dict):
        result = {key: _render_frames(item, cache) for key, item in value.items() if key != "jpeg_path"}
        path = value.get("jpeg_path")
        if path:
            if path not in cache:
                cache[path] = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            result["jpeg_base64"] = cache[path]
        return result
    if isinstance(value, list):
        return [_render_frames(item, cache) for item in value]
    return value


class InferenceQueryService:
    """分页读取持久化的逐帧推理记录（迁移 InferenceQueryService.java）。

    engine 注入策略：
    - 不传（生产默认）：复用 app.db.database 的共享 engine，全后端只保留一个连接池；
    - 显式传入：用于测试，或将来把检测记录拆到独立库时指向另一个 DSN，
      此时由调用方负责 dispose()。
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    def _get_engine(self) -> Engine:
        return self._engine if self._engine is not None else get_engine()

    def dispose(self) -> None:
        """只释放显式注入的 engine；共享 engine 由 reset_engine() 统一释放。"""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def results(self, task_run_id: str, offset: int, limit: int) -> dict[str, Any]:
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 200))
        try:
            with self._get_engine().connect() as connection:
                total = connection.execute(
                    text("SELECT count(*) FROM v_inference_run_results WHERE task_run_id=:task_run_id"),
                    {"task_run_id": task_run_id},
                ).scalar() or 0
                rows = connection.execute(
                    _RESULT_SQL,
                    {"task_run_id": task_run_id, "offset": safe_offset, "limit": safe_limit},
                ).mappings().all()
                grouped = self._detections(connection, [str(row["request_id"]) for row in rows])
                items = [self._to_public_item(row, grouped.get(str(row["request_id"]), [])) for row in rows]
        except SQLAlchemyError as error:
            if getattr(getattr(error, "orig", None), "sqlstate", None) == "42P01":
                return {"items": [], "offset": safe_offset, "limit": safe_limit, "total": 0}
            raise AppError("Detection results are temporarily unavailable") from error
        return {"items": items, "offset": safe_offset, "limit": safe_limit, "total": int(total)}

    def _to_public_item(self, row, detections: list[dict]) -> dict[str, Any]:
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
            "detections": detections,
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
    def _detections(connection, request_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not request_ids:
            return {}
        rows = connection.execute(_DETECTION_SQL, {"request_ids": request_ids}).mappings().all()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(str(row["request_id"]), []).append({
                "detectionId": row["detection_id"],
                "cameraId": row["camera_id"],
                "frameId": row["frame_id"],
                "label": row["label"],
                "classId": row["class_id"],
                "confidence": row["confidence"],
                "x0": row["x0"], "y0": row["y0"], "x1": row["x1"], "y1": row["y1"],
                "createdAt": row["created_at"],
            })
        return grouped


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
