"""Mission session: runtime snapshot, capture mode and position source.

巡检任务（采集会话）所需的最小增量：
- missions 补 updated_at（现有表只有 created_at / approved_at，改任务时无处记录）
- mission_runs 补 params_json（本次运行实际生效的采集组合，事后可追溯）
- inspection_records 补位置可信度与定位来源（一期 simulated，二期接二维定位 + RFID）

只加列与索引，不重建任何表，不触碰 ui_* 工作台影子表。
"""

from alembic import op

revision = "0003_mission_session"
down_revision = "0002_perception"
branch_labels = None
depends_on = None


def upgrade():
    # ---- 任务定义 ----
    op.execute("ALTER TABLE missions ADD COLUMN IF NOT EXISTS updated_at TEXT")
    op.execute("UPDATE missions SET updated_at = created_at WHERE updated_at IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state)")

    # ---- 运行实例 ----
    op.execute("ALTER TABLE mission_runs ADD COLUMN IF NOT EXISTS params_json TEXT NOT NULL DEFAULT '{}'")
    op.execute("CREATE INDEX IF NOT EXISTS idx_mission_runs_mission ON mission_runs(mission_id, started_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_mission_runs_state ON mission_runs(state)")

    # ---- 巡检记录：位置可信度 ----
    op.execute("ALTER TABLE inspection_records ADD COLUMN IF NOT EXISTS capture_mode TEXT NOT NULL DEFAULT 'real'")
    op.execute("ALTER TABLE inspection_records ADD COLUMN IF NOT EXISTS position_source TEXT NOT NULL DEFAULT 'simulated'")
    op.execute("ALTER TABLE inspection_records ADD COLUMN IF NOT EXISTS map_x REAL")
    op.execute("ALTER TABLE inspection_records ADD COLUMN IF NOT EXISTS map_y REAL")
    op.execute("ALTER TABLE inspection_records ADD COLUMN IF NOT EXISTS rfid_tag TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_inspection_records_run ON inspection_records(task_run_id, captured_at DESC)")

    op.execute("INSERT INTO schema_revisions(version,applied_at) VALUES (9,CURRENT_TIMESTAMP::text)")


def downgrade():
    raise RuntimeError("Mission history must be preserved; downgrade using a backup")
