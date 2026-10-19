"""媒体服务：采集请求 + 音频/图片资产查询与回放（迁移 MediaCaptureService.java）。

``deviceId`` 语义收敛为「相机编号」：可传球形摄像头的设备名 ``camera``（兼容旧调用）、
球形编号（如 ``VAR-1``）或多路抓图相机编号（``cam01``…）。服务端把它映射成
sensor-worker 的 provider key，避免“都叫 camera”时全部落到同一路。

``MediaLibraryService`` 只读 ``media_assets`` 与本地文件（音频回放/下载），
写入权仍属于感知进程 —— 与 ``InferenceQueryService`` 同一套约定。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.common.config import Settings, get_settings
from app.common.deployment import load_platform_values
from app.common.exceptions import AppError, ValidationFailed
from app.contracts import Op
from app.db.database import get_engine
from app.gateways.base import camelize
from app.gateways.sensor_worker import SensorWorkerGateway
from app.schemas.media import MediaCaptureRequest
from app.services.cameras import CameraDirectoryService

logger = logging.getLogger(__name__)

# sensor-worker 固定注册的非相机 provider
AUDIO_DEVICE_ID = "audio"
THERMAL_DEVICE_ID = "thermal"

_WINDOWS_FILE_URI = re.compile(r"^/([A-Za-z]:[/\\].*)$")

_COLUMNS = ("asset_id, robot_id, task_run_id, waypoint_id, device_id, media_type, uri, "
            "sha256, size_bytes, mime_type, quality_state, captured_at")


class MediaService:
    def __init__(self, gateway: SensorWorkerGateway, directory: CameraDirectoryService) -> None:
        self._gateway = gateway
        self._directory = directory

    async def capture(self, request: MediaCaptureRequest) -> dict:
        device_id = str(request.deviceId)
        media_type = str(request.mediaType)
        if media_type == "image":
            provider = self._camera_provider(device_id)
        elif media_type == "audio" and device_id == AUDIO_DEVICE_ID:
            provider = AUDIO_DEVICE_ID
        elif media_type == "thermal" and device_id == THERMAL_DEVICE_ID:
            provider = THERMAL_DEVICE_ID
        else:
            raise ValidationFailed(
                f"deviceId 与 mediaType 不匹配：deviceId={device_id}、mediaType={media_type}；"
                "图片采集请传相机编号，录音传 audio，热成像传 thermal"
            )
        return camelize(await self._gateway.call({
            "operation": Op.CAPTURE,
            "task_run_id": request.taskRunId,
            "waypoint_id": request.waypointId,
            "device_id": provider,
            "media_type": media_type,
            "parameters": request.parameters,
        }))

    def _camera_provider(self, device_id: str) -> str:
        """相机编号 → sensor-worker provider key。"""
        ptz = self._directory.ptz()
        if device_id == ptz.device_id:
            # 旧调用直接写设备名 camera：仍然指球形，避免破坏既有前端
            return ptz.device_id
        entry = self._directory.resolve(device_id)
        if not entry.enabled:
            raise ValidationFailed(f"{entry.label}（{entry.id}）已在配置里禁用，无法采集")
        return entry.device_id if entry.is_ptz else entry.id


class MediaLibraryService:
    """媒体资产只读查询：列表、详情、文件路径（供回放/下载）。"""

    def __init__(self, settings: Optional[Settings] = None, engine: Optional[Engine] = None) -> None:
        self._settings = settings or get_settings()
        self._engine = engine
        self._media_root: Optional[Path] = None

    # -- 查询 -------------------------------------------------------------
    def list_assets(self, *, task_run_id: str | None = None, media_type: str | None = None,
                    offset: int = 0, limit: int = 50) -> dict[str, Any]:
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 200))
        clauses: list[str] = []
        params: dict[str, Any] = {"offset": safe_offset, "limit": safe_limit}
        if task_run_id:
            clauses.append("task_run_id = :task_run_id")
            params["task_run_id"] = task_run_id
        if media_type:
            clauses.append("media_type = :media_type")
            params["media_type"] = media_type
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            with self._get_engine().connect() as connection:
                total = connection.execute(text(f"SELECT count(*) FROM media_assets{where}"), params).scalar() or 0
                rows = connection.execute(text(
                    f"SELECT {_COLUMNS} FROM media_assets{where} ORDER BY captured_at DESC, asset_id DESC "
                    "OFFSET :offset LIMIT :limit"), params).mappings().all()
        except SQLAlchemyError as error:
            if getattr(getattr(error, "orig", None), "sqlstate", None) == "42P01":
                return {"items": [], "offset": safe_offset, "limit": safe_limit, "total": 0}
            raise AppError("媒体资产暂时不可用") from error
        return {
            "items": [self._to_item(row) for row in rows],
            "offset": safe_offset,
            "limit": safe_limit,
            "total": int(total),
        }

    def asset(self, asset_id: str) -> dict[str, Any]:
        row = self._row(asset_id)
        if row is None:
            raise AppError("媒体资产不存在", code="NOT_FOUND", status_code=404)
        return self._to_item(row)

    def file(self, asset_id: str) -> tuple[Path, dict[str, Any]]:
        """返回本地文件路径与资产信息；路径必须落在媒体根目录内。"""
        row = self._row(asset_id)
        if row is None:
            raise AppError("媒体资产不存在", code="NOT_FOUND", status_code=404)
        path = self._path_of(str(row["uri"]))
        if path is None or not path.is_file():
            raise AppError("媒体文件不存在或已被清理", code="NOT_FOUND", status_code=404)
        return path, self._to_item(row)

    # -- 内部 -------------------------------------------------------------
    def _row(self, asset_id: str):
        try:
            with self._get_engine().connect() as connection:
                return connection.execute(
                    text(f"SELECT {_COLUMNS} FROM media_assets WHERE asset_id = :asset_id"),
                    {"asset_id": asset_id},
                ).mappings().first()
        except SQLAlchemyError as error:
            if getattr(getattr(error, "orig", None), "sqlstate", None) == "42P01":
                return None
            raise AppError("媒体资产暂时不可用") from error

    def _to_item(self, row) -> dict[str, Any]:
        item = {key: row[key] for key in row.keys()}
        item["file_name"] = f"{row['asset_id']}{Path(urlparse(str(row['uri'])).path).suffix}"
        item["download_url"] = f"/api/v1/media/assets/{row['asset_id']}/content"
        return camelize(item)

    def _path_of(self, uri: str) -> Optional[Path]:
        parsed = urlparse(uri)
        if parsed.scheme not in ("", "file"):
            return None
        raw = unquote(parsed.path)
        match = _WINDOWS_FILE_URI.match(raw) if os.name == "nt" else None
        if match:
            raw = match.group(1)
        try:
            resolved = Path(raw).resolve()
        except OSError:
            return None
        root = self.media_root()
        if resolved != root and root not in resolved.parents:
            logger.warning("media path escapes media root: %s", resolved)
            return None
        return resolved

    def media_root(self) -> Path:
        if self._media_root is None:
            self._media_root = _resolve_media_root(self._settings)
        return self._media_root

    def _get_engine(self) -> Engine:
        return self._engine if self._engine is not None else get_engine()

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None


def _resolve_media_root(settings: Settings) -> Path:
    project = Path(__file__).resolve().parents[2]
    if settings.platform_config:
        try:
            values = load_platform_values(Path(settings.platform_config))
            raw = (values.get("storage") or {}).get("media_root")
            if raw:
                config_dir = Path(settings.platform_config).expanduser().resolve().parent
                return (config_dir / str(raw)).resolve()
        except Exception as error:  # noqa: BLE001 - 配置不可读时退回默认目录并留痕
            logger.warning("media_root from platform config unavailable: %s", error)
    if settings.media_root:
        root = Path(settings.media_root).expanduser()
        return (root if root.is_absolute() else project / root).resolve()
    return (project / "data" / "media").resolve()
