from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Allow `python runtime/src/.../migrate.py` from a source checkout without
# requiring the caller to install the package or set PYTHONPATH manually.
SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from app.robot_core.common.config import load_config, redact_database_url

try:
    from .database import PlatformStore
except ImportError:  # direct script execution
    from app.robot_core.storage.database import PlatformStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="robot-db")
    parser.add_argument("--config")
    parser.add_argument("command", choices=("upgrade", "check", "import-sqlite"))
    parser.add_argument("--from-sqlite", help="SQLite file to import when command=import-sqlite")
    args = parser.parse_args()
    config = load_config(args.config)
    store = PlatformStore(config.database_url, pool_options=config.database_pool_options)
    if args.command == "import-sqlite":
        if not args.from_sqlite:
            parser.error("--from-sqlite is required for import-sqlite")
        imported = _import_sqlite(Path(args.from_sqlite), store)
        print(json.dumps({"status": "ok", "command": args.command, "database": redact_database_url(store.database_url), "tables": imported}, ensure_ascii=False))
        return
    expected = {
        "schema_revisions", "robots", "control_leases", "config_revisions",
        "topology_revisions", "device_registry", "device_states", "device_state_history", "missions",
        "mission_runs", "task_checkpoints", "commands", "events", "media_assets",
        "farms", "coops", "agv_points", "cages", "mission_points",
        "inspection_records", "detection_boxes", "inspection_symptoms",
        "inference_runs", "alerts", "reviews", "telemetry_segments", "outbox", "inbox",
    }
    missing = expected - store.table_names()
    if missing:
        raise RuntimeError(f"database schema incomplete: {sorted(missing)}")
    print(json.dumps({"status": "ok", "command": args.command, "database": redact_database_url(store.database_url), "revision": 5}, ensure_ascii=False))


def _import_sqlite(source_path: Path, store: PlatformStore) -> dict[str, int]:
    """Copy matching tables from a legacy SQLite database into PostgreSQL.

    The import is intentionally additive and idempotent: existing rows win on
    conflicts, allowing an interrupted import to be safely resumed.
    """
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    with sqlite3.connect(source_path) as source, store.connect() as target:
        source.row_factory = sqlite3.Row
        tables = [
            row["name"] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        copied: dict[str, int] = {}
        for table in tables:
            columns = [row["name"] for row in source.execute(f"PRAGMA table_info({table})").fetchall()]
            if not columns:
                continue
            if getattr(target, "is_postgres", False):
                target_columns = {
                    row["column_name"] for row in target.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = ?", (table,)
                    ).fetchall()
                }
            else:
                target_columns = {row["name"] for row in target.execute(f"PRAGMA table_info({table})").fetchall()}
            columns = [column for column in columns if column in target_columns]
            if not columns:
                continue
            quoted = ",".join(columns)
            placeholders = ",".join("?" for _ in columns)
            count = 0
            for row in source.execute(f"SELECT {quoted} FROM {table}"):
                target.execute(
                    f"INSERT INTO {table} ({quoted}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    tuple(row[column] for column in columns),
                )
                count += 1
            copied[table] = count
        return copied


if __name__ == "__main__":
    main()
