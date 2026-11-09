"""Alerts: add alert_type so the unified alert page keeps its type column.

旧告警页有「告警类型」列（ui_alerts.payload.type），而规范表 alerts 只有 message。
统一到 alerts 后需要一个可筛选的类别字段，故补一列，不改任何既有列。
"""

from alembic import op

revision = "0004_alert_type"
down_revision = "0003_mission_session"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS alert_type TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type)")
    op.execute("INSERT INTO schema_revisions(version,applied_at) VALUES (10,CURRENT_TIMESTAMP::text)")


def downgrade():
    raise RuntimeError("Alert history must be preserved; downgrade using a backup")
