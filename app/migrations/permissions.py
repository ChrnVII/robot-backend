"""Grant existing deployment roles access only to their own data and read views."""

import argparse
import os

import psycopg
from psycopg import sql

from app.common.config import get_settings
from app.common.dburl import psycopg_dsn

BACKEND_TABLES = (
    "ui_tasks", "ui_task_templates", "ui_runs", "ui_alerts", "ui_presets",
    "ui_config", "ui_map", "ui_lift", "users", "refresh_tokens",
)
CORE_TABLES = ("commands", "control_leases", "device_states", "device_state_history", "events", "outbox")
PERCEPTION_TABLES = ("inspection_runs", "media_assets", "inference_runs", "inference_detections", "events", "outbox")
READ_VIEWS = ("v_inference_run_results", "v_inference_detections")


def grant_roles(connection, backend: str, core: str, perception: str, sync: str | None = None):
    assignments = [(backend, BACKEND_TABLES), (core, CORE_TABLES), (perception, PERCEPTION_TABLES)]
    if sync:
        assignments.append((sync, ("outbox", "inbox")))
    if len({role for role, _ in assignments}) != len(assignments):
        raise ValueError("use distinct deployment roles")
    for role, tables in assignments:
        info = connection.execute("SELECT rolsuper FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
        if info is None or info[0]:
            raise ValueError(f"role must exist and must not be a superuser: {role}")
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT SELECT ON alembic_version TO {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO {}").format(
            sql.SQL(",").join(sql.Identifier(table) for table in tables), sql.Identifier(role)))
        if role in {backend, perception}:
            connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(
                sql.SQL(",").join(sql.Identifier(table) for table in READ_VIEWS), sql.Identifier(role)))
    connection.execute(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE users_id_seq,refresh_tokens_id_seq TO {}").format(sql.Identifier(backend)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-role", required=True)
    parser.add_argument("--core-role", required=True)
    parser.add_argument("--perception-role", required=True)
    parser.add_argument("--sync-role")
    args = parser.parse_args()
    database_url = os.environ.get("ROBOT_MIGRATION_DATABASE_URL") or get_settings().database_url
    with psycopg.connect(psycopg_dsn(database_url)) as connection:
        grant_roles(connection, args.backend_role, args.core_role, args.perception_role, args.sync_role)


if __name__ == "__main__":
    main()
