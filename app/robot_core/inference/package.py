from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.robot_core.common import freeze_directory, sha256_file, verify_manifest


class ModelPackageError(ValueError):
    pass


@dataclass(frozen=True)
class ModelPackage:
    model_id: str
    version: str
    artifact: Path
    sha256: str
    adapter_version: str
    result_schema: str
    metadata: Mapping[str, Any]

    @classmethod
    def create(
        cls, root: str | Path, artifact: str | Path, *, model_id: str, version: str,
        adapter_version: str = "1", result_schema: str = "object-detection.v1",
        metadata: Mapping[str, Any] | None = None,
    ) -> "ModelPackage":
        """Publish an immutable package directory for a model artifact."""
        directory = Path(root).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        source = Path(artifact).expanduser().resolve()
        if not source.is_file():
            raise ModelPackageError(f"model artifact not found: {source}")
        target = directory / source.name
        if source != target:
            target.write_bytes(source.read_bytes())
        package = cls(model_id, version, target, sha256_file(target), adapter_version, result_schema, dict(metadata or {}))
        manifest = {
            "model_id": package.model_id, "version": package.version,
            "artifact": target.name, "sha256": package.sha256,
            "adapter_version": package.adapter_version, "result_schema": package.result_schema,
            "metadata": dict(package.metadata),
        }
        (directory / "model-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        freeze_directory(directory, kind="model", identifier=model_id, version=version)
        return package

    @classmethod
    def from_artifact(
        cls, artifact: str | Path, *, model_id: str, version: str | None = None,
        adapter_version: str = "1", result_schema: str = "object-detection.v1",
    ) -> "ModelPackage":
        path = Path(artifact).expanduser().resolve()
        if not path.is_file():
            raise ModelPackageError(f"model artifact not found: {path}")
        return cls(model_id, version or path.stem, path, sha256_file(path), adapter_version, result_schema, {})

    @classmethod
    def load(cls, root: str | Path, *, expected_model_id: str | None = None) -> "ModelPackage":
        directory = Path(root).expanduser().resolve()
        if not directory.is_dir():
            raise ModelPackageError(f"model package directory not found: {directory}")
        try:
            verify_manifest(directory)
        except ValueError as error:
            raise ModelPackageError(str(error)) from error
        manifest_path = directory / "model-manifest.json"
        if not manifest_path.is_file():
            raise ModelPackageError(f"model-manifest.json not found: {directory}")
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelPackageError(f"invalid model manifest: {manifest_path}") from error
        if not isinstance(value, dict):
            raise ModelPackageError("model manifest must be an object")
        model_id = str(value.get("model_id", ""))
        version = str(value.get("version", ""))
        if expected_model_id and model_id != expected_model_id:
            raise ModelPackageError(f"model id mismatch: expected {expected_model_id}, found {model_id}")
        artifact = (directory / str(value.get("artifact", ""))).resolve()
        if directory not in artifact.parents or not artifact.is_file():
            raise ModelPackageError("model artifact must be a file inside the package")
        expected_sha = str(value.get("sha256", "")).lower()
        actual_sha = sha256_file(artifact)
        if not model_id or not version or not expected_sha or actual_sha != expected_sha:
            raise ModelPackageError("model manifest identity or SHA-256 is invalid")
        return cls(
            model_id, version, artifact, actual_sha,
            str(value.get("adapter_version", "1")),
            str(value.get("result_schema", "object-detection.v1")),
            dict(value.get("metadata", {})),
        )
