from __future__ import annotations

import argparse
import json
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
    parser.add_argument("command", choices=("upgrade", "check"))
    args = parser.parse_args()
    config = load_config(args.config)
    store = PlatformStore(config.database_url, pool_options=config.database_pool_options)
    try:
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
    finally:
        store.close()


if __name__ == "__main__":
    main()
