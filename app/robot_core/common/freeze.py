from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MANIFEST_NAME = "freeze-manifest.json"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path, excluded: set[str] | None = None) -> list[Path]:
    excluded = excluded or set()
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME and path.name not in excluded
    )


def freeze_directory(root: str | Path, *, kind: str, identifier: str, version: str, exclude_names: set[str] | None = None) -> Path:
    """Write a content manifest and make the published tree read-only."""
    directory = Path(root).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    excluded = set(exclude_names or ())
    entries = [
        {"path": path.relative_to(directory).as_posix(), "sha256": sha256_file(path)}
        for path in _files(directory)
    ]
    manifest: Mapping[str, Any] = {
        "manifest_version": 1,
        "kind": kind,
        "identifier": identifier,
        "version": version,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "excluded": sorted(excluded),
        "files": entries,
    }
    fd, temporary_name = tempfile.mkstemp(prefix=".freeze-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / MANIFEST_NAME)
    finally:
        temporary.unlink(missing_ok=True)
    for path in _files(directory, excluded) + [directory / MANIFEST_NAME]:
        try:
            path.chmod(path.stat().st_mode & ~0o222)
        except OSError:
            pass
    return directory / MANIFEST_NAME


def verify_manifest(root: str | Path) -> dict[str, Any]:
    directory = Path(root).expanduser().resolve()
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError(f"freeze manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        raise ValueError(f"invalid freeze manifest: {manifest_path}")
    excluded = {str(value) for value in manifest.get("excluded", [])}
    expected = {str(item["path"]): str(item["sha256"]) for item in manifest.get("files", [])}
    actual = {path.relative_to(directory).as_posix(): sha256_file(path) for path in _files(directory, excluded)}
    if expected != actual:
        raise ValueError("frozen directory content does not match its manifest")
    return manifest
