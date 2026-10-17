from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional



from app.common.cameras import validate_cameras
from app.common.deployment import (
    ConfigurationError, expand_environment as _expand_environment,
    load_platform_values, validate_deployment,
)


def redact_database_url(value: str) -> str:
    """Hide a password before a DSN is written to checks, logs or reports."""
    if "://" not in value:
        return value
    scheme, remainder = value.split("://", 1)
    if "@" not in remainder:
        return value
    credentials, endpoint = remainder.rsplit("@", 1)
    username = credentials.split(":", 1)[0]
    return f"{scheme}://{username}:***@{endpoint}"


@dataclass(frozen=True)
class PlatformConfig:
    source: Path
    values: Mapping[str, Any]

    @property
    def robot_id(self) -> str:
        return str(self.values["robot"]["robot_id"])

    def path(self, section: str, key: str):
        raw = str(self.values[section][key])
        if "://" in raw:
            return raw
        value = Path(raw)
        return value if value.is_absolute() else (self.source.parent / value).resolve()

    @property
    def database_url(self) -> str:
        return str(self.values["storage"].get("database_url") or self.values["storage"]["database"])

    def database_url_for(self, role: str) -> str:
        if role not in {"core", "perception", "sync", "migration"}:
            raise ValueError(f"unknown database role: {role}")
        return os.environ.get(f"ROBOT_{role.upper()}_DATABASE_URL") or self.database_url

    @property
    def sync_enabled(self) -> bool:
        override = os.environ.get("ROBOT_SYNC_ENABLED")
        return override.lower() in {"true", "1", "yes"} if override is not None else bool(self.values["sync"]["enabled"])

    @property
    def database_display_url(self) -> str:
        return redact_database_url(self.database_url)

    @property
    def database_pool_options(self) -> Dict[str, Any]:
        pool = self.values["storage"].get("pool", {})
        return {
            "min_size": int(pool.get("min_size", 1)),
            "max_size": int(pool.get("max_size", 8)),
            "timeout": float(pool.get("timeout_seconds", 10.0)),
        }


REQUIRED_SECTIONS = {
    "robot", "runtime", "storage", "ipc", "api", "safety", "sync", "models", "legacy_compat"
}


def load_config(path: Optional[str] = None) -> PlatformConfig:
    root = Path(__file__).resolve().parents[3]
    source = Path(path or os.environ.get("ROBOT_PLATFORM_CONFIG", root / "configs" / "base.yaml")).resolve()
    value = load_platform_values(source)
    missing = REQUIRED_SECTIONS - set(value)
    unknown = set(value) - (REQUIRED_SECTIONS | {"schema_version", "devices", "inspection"})
    if missing or unknown:
        raise ConfigurationError(f"configuration sections invalid: missing={sorted(missing)}, unknown={sorted(unknown)}")
    if value.get("schema_version") != 1:
        raise ConfigurationError("unsupported configuration schema_version")
    if value["runtime"].get("mode") not in {"simulated", "hardware"}:
        raise ConfigurationError("runtime.mode must be simulated or hardware")
    storage = value["storage"]
    database_url = storage.get("database_url") or storage.get("database")
    if not database_url:
        raise ConfigurationError("storage.database_url is required")
    if str(storage.get("backend", "postgresql")) != "postgresql":
        raise ConfigurationError("storage.backend must be 'postgresql' (SQLite support removed)")
    if not str(database_url).startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        raise ConfigurationError("storage requires a PostgreSQL DSN")
    pool = storage.get("pool", {})
    if not isinstance(pool, dict):
        raise ConfigurationError("storage.pool must be a mapping")
    min_size = int(pool.get("min_size", 1))
    max_size = int(pool.get("max_size", 8))
    timeout = float(pool.get("timeout_seconds", 10.0))
    if min_size < 1 or max_size < min_size or timeout <= 0:
        raise ConfigurationError("storage.pool must satisfy 1 <= min_size <= max_size and timeout_seconds > 0")
    validate_deployment(value, os.environ)
    validate_cameras(value)
    return PlatformConfig(source=source, values=value)
