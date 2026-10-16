from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional, Union
from urllib.parse import unquote, urlparse

from app.robot_core.common import new_id, utc_now

from .database import PlatformStore


class MediaStore:
    def __init__(self, root: Union[str, Path], store: Optional[PlatformStore], robot_id: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.robot_id = robot_id

    def save(self, source: BinaryIO, *, task_run_id: str, waypoint_id: str, device_id: str,
             media_type: str, extension: str, mime_type: str, quality_state: str = "valid", cage_id: Optional[str] = None) -> Dict[str, Any]:
        asset_id = new_id("asset")
        directory = self.root / media_type
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, temporary_name = tempfile.mkstemp(prefix=f".{asset_id}-", dir=directory)
        temporary = Path(temporary_name)
        final = directory / f"{asset_id}{extension}"
        try:
            with os.fdopen(fd, "wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, final)
            now = utc_now()
            asset = {
                "asset_id": asset_id, "robot_id": self.robot_id, "task_run_id": task_run_id,
                "waypoint_id": waypoint_id, "device_id": device_id, "media_type": media_type,
                "uri": final.resolve().as_uri(), "sha256": digest.hexdigest(), "size_bytes": size,
                "mime_type": mime_type, "quality_state": quality_state, "cage_id": cage_id, "captured_at": now, "created_at": now,
            }
            if self.store is not None:
                self.store.register_asset(asset)
            return asset
        except Exception:
            temporary.unlink(missing_ok=True)
            if final.exists():
                final.unlink()
            raise

    def discard(self, asset: Dict[str, Any]) -> None:
        uri = str(asset["uri"])
        parsed = urlparse(uri)
        path = Path(unquote(parsed.path))
        path.unlink(missing_ok=True)
