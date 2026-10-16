import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.common.dburl import psycopg_dsn
from app.migrations import HEAD_REVISION, check_database, upgrade_database
from app.migrations.permissions import grant_roles


def test_existing_v7_data_survives_upgrade_and_check_is_read_only():
    dsn = os.environ["ROBOT_TEST_DATABASE_URL"]
    baseline = Path(__file__).parents[3] / "app/migrations/versions/0001_platform.sql"
    with psycopg.connect(psycopg_dsn(dsn)) as connection:
        connection.execute(baseline.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_revisions VALUES (7,'2026-01-01')")
        connection.execute("INSERT INTO robots(robot_id,name,created_at,updated_at) VALUES ('legacy','old','now','now')")
    upgrade_database(dsn)
    upgrade_database(dsn)
    check_database(dsn)
    with psycopg.connect(psycopg_dsn(dsn)) as connection:
        assert connection.execute("SELECT name FROM robots WHERE robot_id='legacy'").fetchone() == ("old",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
        connection.execute("UPDATE alembic_version SET version_num='0001_platform'")
    with pytest.raises(RuntimeError, match="revision mismatch"):
        check_database(dsn)
    with psycopg.connect(psycopg_dsn(dsn)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001_platform",)


def test_deployment_roles_enforce_table_and_view_boundaries():
    dsn = os.environ["ROBOT_TEST_DATABASE_URL"]
    upgrade_database(dsn)
    names = ["arch_" + uuid4().hex[:12] for _ in range(4)]
    with psycopg.connect(psycopg_dsn(dsn), autocommit=True) as connection:
        # Role provisioning belongs to deployment, and needs a role administrator.
        if not connection.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]:
            pytest.skip("permission integration requires a superuser test database")
        try:
            for name in names:
                connection.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(name)))
            grant_roles(connection, *names)
            for name, owned in zip(names, ("ui_tasks", "commands", "inspection_runs", "outbox")):
                with connection.transaction():
                    connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(name)))
                    connection.execute("SELECT version_num FROM alembic_version")
                    connection.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(sql.Identifier(owned)))
                    if name in {names[0], names[2]}:
                        connection.execute("SELECT * FROM v_inference_run_results LIMIT 1")
                        connection.execute("SELECT * FROM v_inference_detections LIMIT 1")
                    foreign_table = "commands" if name != names[1] else "media_assets"
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with connection.transaction():
                            connection.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(foreign_table)))
        finally:
            for name in names:
                connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
