"""Durable inspection lifecycle and stable result read contracts."""

from alembic import op

revision = "0002_perception"
down_revision = "0001_platform"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE inspection_runs (
        task_run_id TEXT PRIMARY KEY, state TEXT NOT NULL,
        payload TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    op.execute("CREATE INDEX idx_inspection_runs_state ON inspection_runs(state)")
    op.execute("ALTER TABLE inference_runs ADD COLUMN camera_id TEXT")
    op.execute("ALTER TABLE inference_runs ADD COLUMN frame_id BIGINT")
    op.execute("ALTER TABLE inference_runs ADD COLUMN result_asset_id TEXT REFERENCES media_assets(asset_id)")
    op.execute("""UPDATE inference_runs SET camera_id=split_part(request_id, ':', 2),
        frame_id=CASE WHEN split_part(request_id, ':', 3) ~ '^[0-9]{1,18}$'
        THEN split_part(request_id, ':', 3)::bigint ELSE 0 END""")
    op.execute("CREATE INDEX idx_detection_request ON inference_detections(request_id, detection_id)")
    op.execute("CREATE INDEX idx_media_frame ON media_assets(task_run_id, device_id, captured_at)")
    op.execute("CREATE INDEX idx_inference_paging ON inference_runs(task_run_id, finished_at DESC, run_id DESC)")
    op.execute("""CREATE OR REPLACE VIEW v_inference_run_results AS
        SELECT ir.run_id, ir.request_id, ir.task_run_id, ir.trace_id, ir.capability,
               ir.model_version, ir.model_sha256, ir.status, ir.result,
               ir.started_at, ir.finished_at, ma.asset_id, ma.uri, ma.mime_type,
               ma.sha256, ma.size_bytes, ma.captured_at,
               ma2.asset_id AS result_asset_id, ma2.uri AS result_uri,
               ma2.mime_type AS result_mime_type, ir.camera_id, ir.frame_id
        FROM inference_runs ir JOIN media_assets ma ON ma.asset_id=ir.asset_id
        LEFT JOIN LATERAL (
            SELECT candidate.* FROM media_assets candidate
            WHERE candidate.asset_id=ir.result_asset_id OR (
                ir.result_asset_id IS NULL AND candidate.task_run_id=ir.task_run_id
                AND candidate.device_id=ma.device_id AND candidate.media_type='image'
                AND strpos(candidate.uri, '_result.jpg')>0 AND candidate.captured_at=ma.captured_at)
            ORDER BY candidate.created_at DESC, candidate.asset_id LIMIT 1
        ) ma2 ON true""")
    op.execute("CREATE VIEW v_inference_detections AS SELECT * FROM inference_detections")
    op.execute("INSERT INTO schema_revisions(version,applied_at) VALUES (8,CURRENT_TIMESTAMP::text)")


def downgrade():
    raise RuntimeError("Inspection history must be preserved; downgrade using a backup")
