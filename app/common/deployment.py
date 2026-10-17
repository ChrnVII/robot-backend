"""Shared, offline deployment checks; never include DSN credentials in errors."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

import yaml


class ConfigurationError(ValueError):
    pass


_ENVIRONMENT_VALUE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigurationError(f"environment variable {name} is required")
        return _ENVIRONMENT_VALUE.sub(replace, value)
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    return value


def load_platform_values(source: Path) -> dict:
    with source.open(encoding="utf-8") as handle:
        value = expand_environment(yaml.safe_load(handle))
    if not isinstance(value, dict):
        raise ConfigurationError("configuration root must be a mapping")
    return value


def _database_target(dsn: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(dsn)
        if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
            raise ValueError
        query = parse_qs(parsed.query)
        host = query.get("host", [parsed.hostname or ""])[-1]
        if not host.startswith("/"):
            host = host.lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            host = "loopback"
        port = query.get("port", [str(parsed.port or 5432)])[-1]
        database = query.get("dbname", [unquote(parsed.path.lstrip("/")) or unquote(parsed.username or "")])[-1]
        # libpq service files and environment-derived endpoints cannot be
        # compared reliably without resolving external configuration.
        if "service" in query or not host or not database:
            raise ValueError
        return host, port, database
    except (TypeError, ValueError):
        raise ConfigurationError("database target must explicitly specify host and database") from None


def validate_deployment(values: Mapping[str, Any], overrides: Mapping[str, Any]) -> None:
    for variable, expected in (("ROBOT_ROBOT_ID", values["robot"]["robot_id"]),
                               ("ROBOT_MODE", values["runtime"]["mode"])):
        if variable in overrides and overrides[variable] != expected:
            raise ConfigurationError(f"{variable} conflicts with platform configuration")
    storage = values["storage"]
    baseline = str(storage.get("database_url") or storage["database"])
    for variable in ("ROBOT_DATABASE_URL", "ROBOT_CORE_DATABASE_URL", "ROBOT_PERCEPTION_DATABASE_URL",
                     "ROBOT_SYNC_DATABASE_URL", "ROBOT_MIGRATION_DATABASE_URL"):
        if overrides.get(variable) and _database_target(str(overrides[variable])) != _database_target(baseline):
            raise ConfigurationError(f"{variable} targets a different platform database")


def validate_backend_deployment(settings) -> None:
    if settings.platform_config:
        overrides = dict(os.environ)
        overrides.update(ROBOT_ROBOT_ID=settings.robot_id, ROBOT_MODE=settings.mode,
                         ROBOT_DATABASE_URL=settings.database_url)
        values = load_platform_values(Path(settings.platform_config))
        validate_deployment(values, overrides)
        # 延迟导入：app.common.cameras 依赖本模块的 ConfigurationError
        from app.common.cameras import validate_cameras

        validate_cameras(values)
