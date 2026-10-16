"""Adopt existing v1-v7 installations and bootstrap new platform databases."""

from pathlib import Path

from alembic import op

revision = "0001_platform"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    script = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    statements = []
    for part in script.split(";"):
        sql = "\n".join(line for line in part.splitlines() if not line.strip().startswith("--")).strip()
        if sql:
            statements.append(sql)
    # Old tables must gain their columns before indexes/views reference them.
    for sql in statements:
        if sql.upper().startswith("CREATE TABLE"):
            op.get_bind().exec_driver_sql(sql)
    additions = {
        "commands": {"task_run_id": "TEXT"},
        "events": {"task_run_id": "TEXT", "aggregate_type": "TEXT", "aggregate_id": "TEXT"},
        "inference_runs": {"model_sha256": "TEXT"},
        "media_assets": {"cage_id": "TEXT"},
        "outbox": {"claim_token": "TEXT", "claimed_at": "TEXT", "last_attempt_at": "TEXT",
                   "remote_ack_at": "TEXT", "delivery_count": "INTEGER NOT NULL DEFAULT 0"},
        "inbox": {"idempotency_key": "TEXT"},
    }
    for table, columns in additions.items():
        for name, definition in columns.items():
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {definition}")
    op.execute("ALTER TABLE inference_detections DROP CONSTRAINT IF EXISTS inference_detections_request_id_detection_id_key")
    op.execute("DROP INDEX IF EXISTS idx_device_state_history_device_time")
    for sql in statements:
        if not sql.upper().startswith("CREATE TABLE"):
            op.get_bind().exec_driver_sql(sql)
    op.execute("INSERT INTO schema_revisions(version,applied_at) VALUES (7, CURRENT_TIMESTAMP::text) ON CONFLICT DO NOTHING")


def downgrade():
    raise RuntimeError("Baseline contains existing user data; restore a backup to downgrade")
