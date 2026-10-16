CREATE TABLE IF NOT EXISTS schema_revisions (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS robots (
    robot_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    serial_number TEXT,
    site_code TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_leases (
    lease_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    mode TEXT NOT NULL,
    priority INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT
);
CREATE TABLE IF NOT EXISTS config_revisions (
    config_revision_id TEXT PRIMARY KEY,
    config_type TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    UNIQUE(config_type, revision)
);
CREATE TABLE IF NOT EXISTS topology_revisions (
    topology_revision_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    topology TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    UNIQUE(name, revision)
);
CREATE TABLE IF NOT EXISTS device_registry (
    device_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL,
    device_type TEXT NOT NULL,
    protocol TEXT NOT NULL,
    connection_alias TEXT NOT NULL UNIQUE,
    vendor TEXT,
    model TEXT,
    serial_number TEXT,
    firmware_version TEXT,
    capabilities TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_robot_type ON device_registry(robot_id, device_type);
CREATE TABLE IF NOT EXISTS device_states (
    device_id TEXT PRIMARY KEY,
    health TEXT NOT NULL,
    quality TEXT NOT NULL,
    values_json TEXT NOT NULL,
    raw_fault_code TEXT,
    sampled_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL
);
-- Append-only device state samples. device_states remains the latest snapshot.
CREATE TABLE IF NOT EXISTS device_state_history (
    sample_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    health TEXT NOT NULL,
    quality TEXT NOT NULL,
    values_json TEXT NOT NULL,
    raw_fault_code TEXT,
    sampled_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL
);
-- 覆盖 device_state_history 的「按设备取最近 N 条」查询：
-- ORDER BY sampled_at DESC, received_at DESC 全程走索引，无需额外排序。
CREATE INDEX IF NOT EXISTS idx_device_state_history_device_time
    ON device_state_history(device_id, sampled_at DESC, received_at DESC);
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    topology_revision_id TEXT,
    definition TEXT NOT NULL,
    state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    UNIQUE(name, revision)
);
CREATE TABLE IF NOT EXISTS mission_runs (
    task_run_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    state TEXT NOT NULL,
    current_step TEXT,
    current_waypoint_id TEXT,
    config_revision_id TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    failure_code TEXT,
    recovery_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_mission_runs_robot_state ON mission_runs(robot_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_mission_runs_mission_time ON mission_runs(mission_id, started_at);
CREATE TABLE IF NOT EXISTS task_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    state TEXT NOT NULL,
    completed_actions TEXT NOT NULL,
    pending_actions TEXT NOT NULL,
    evidence_asset_ids TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run_time ON task_checkpoints(task_run_id, created_at);
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    trace_id TEXT NOT NULL,
    task_run_id TEXT,
    issuer TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    target TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    completed_at TEXT,
    result TEXT,
    reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_state_issued ON commands(state, issued_at);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    task_run_id TEXT,
    aggregate_type TEXT,
    aggregate_id TEXT,
    data TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_trace_time ON events(trace_id, occurred_at);
CREATE TABLE IF NOT EXISTS media_assets (
    asset_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    waypoint_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    uri TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mime_type TEXT NOT NULL,
    quality_state TEXT NOT NULL,
    cage_id TEXT,
    captured_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_task_time ON media_assets(task_run_id, captured_at);
CREATE TABLE IF NOT EXISTS inference_runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    trace_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    model_version TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    task_run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT NOT NULL,
    model_sha256 TEXT,
    error_stage TEXT,
    suggested_action TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_inference_task_time ON inference_runs(task_run_id, finished_at);
CREATE TABLE IF NOT EXISTS inference_detections (
    detection_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES inference_runs(request_id),
    task_run_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    frame_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    class_id INTEGER,
    confidence REAL NOT NULL,
    x0 REAL NOT NULL,
    y0 REAL NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inference_detections_task ON inference_detections(task_run_id, frame_id);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    source_run_id TEXT,
    severity TEXT NOT NULL,
    state TEXT NOT NULL,
    evidence_asset_id TEXT,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    closed_reason TEXT
);
-- 告警列表按 state/created_at 分页、按 trace_id 回查；原表此前没有任何索引。
CREATE INDEX IF NOT EXISTS idx_alerts_state_time ON alerts(state, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_trace ON alerts(trace_id);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    notes TEXT,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY(alert_id) REFERENCES alerts(alert_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_alert ON reviews(alert_id);
CREATE TABLE IF NOT EXISTS telemetry_segments (
    segment_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL,
    device_id TEXT,
    uri TEXT NOT NULL UNIQUE,
    format TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    quality_summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_robot_time ON telemetry_segments(robot_id, started_at);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    claim_token TEXT,
    claimed_at TEXT,
    last_attempt_at TEXT,
    remote_ack_at TEXT,
    delivery_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_delivery ON outbox(state, next_retry_at, created_at);
CREATE TABLE IF NOT EXISTS inbox (
    inbox_id TEXT PRIMARY KEY,
    remote_message_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT,
    message_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    last_error TEXT,
    response_payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_state_time ON inbox(state, received_at);


-- =====================================================================
-- 巡检域：农场 / 鸡舍 / AGV 点位 / 鸡笼 / 巡检结果
-- =====================================================================

CREATE TABLE IF NOT EXISTS farms (
    farm_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    site_code TEXT UNIQUE,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coops (
    coop_id TEXT PRIMARY KEY,
    farm_id TEXT NOT NULL REFERENCES farms(farm_id),
    coop_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    column_count INTEGER NOT NULL,
    layer_count INTEGER NOT NULL DEFAULT 6,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(farm_id, coop_no)
);

CREATE TABLE IF NOT EXISTS agv_points (
    point_id TEXT PRIMARY KEY,
    coop_id TEXT NOT NULL REFERENCES coops(coop_id),
    coop_no INTEGER NOT NULL,
    column_no INTEGER NOT NULL,
    point_agv TEXT NOT NULL UNIQUE,
    rfid_tag TEXT,
    map_x REAL,
    map_y REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(coop_id, column_no)
);

CREATE TABLE IF NOT EXISTS cages (
    cage_id TEXT PRIMARY KEY,
    coop_id TEXT NOT NULL REFERENCES coops(coop_id),
    point_id TEXT NOT NULL REFERENCES agv_points(point_id),
    coop_no INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    column_no INTEGER NOT NULL,
    layer_no INTEGER NOT NULL,
    coop_code TEXT NOT NULL,
    pointlayer_code TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(coop_id, row_no, column_no, layer_no)
);

CREATE TABLE IF NOT EXISTS mission_points (
    mission_point_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id),
    seq_no INTEGER NOT NULL,
    cage_id TEXT NOT NULL REFERENCES cages(cage_id),
    UNIQUE(mission_id, seq_no),
    UNIQUE(mission_id, cage_id)
);

CREATE TABLE IF NOT EXISTS inspection_records (
    record_id TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES mission_runs(task_run_id),
    cage_id TEXT NOT NULL REFERENCES cages(cage_id),
    coop_no INTEGER NOT NULL,
    point_agv TEXT NOT NULL,
    layer_no INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    death INTEGER,
    chicks INTEGER,
    cleaness INTEGER,
    feed INTEGER,
    sick INTEGER,
    sickweak INTEGER,
    sicklow INTEGER,
    eggtotal INTEGER,
    eggblocked INTEGER,
    eggbreaked INTEGER,
    flocklie INTEGER,
    flockmouthopen INTEGER,
    fogn_fogn INTEGER,
    fogn_lens INTEGER,
    fogn_bar INTEGER,
    fogn_eggbelt INTEGER,
    fogn_dirty_egg INTEGER,
    fogn_blood_egg INTEGER,
    fogn_broken_egg INTEGER,
    temp_max REAL,
    temp_min REAL,
    temp_avg REAL,
    temp_all TEXT,
    temperature REAL,
    humidity REAL,
    illumination REAL,
    wind_speed REAL,
    cal_temperature REAL,
    cal_humidity REAL,
    cal_wind_speed REAL,
    ammonia REAL,
    oxygen REAL,
    atmos REAL,
    co2 REAL,
    thi REAL,
    et REAL,
    hsi REAL,
    eci REAL,
    comfort_index REAL,
    sick_low_data TEXT,
    average_disease_health_index REAL,
    egg_health_index REAL,
    chicken_status_index REAL,
    created_at TEXT NOT NULL,
    UNIQUE(task_run_id, cage_id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_inspection_records_cage_time ON inspection_records(cage_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_inspection_records_run_time ON inspection_records(task_run_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_inspection_records_coop_point ON inspection_records(coop_no, point_agv, layer_no);

CREATE TABLE IF NOT EXISTS detection_boxes (
    box_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES inspection_records(record_id),
    label TEXT NOT NULL,
    x0 INTEGER NOT NULL,
    y0 INTEGER NOT NULL,
    x1 INTEGER NOT NULL,
    y1 INTEGER NOT NULL,
    score REAL NOT NULL,
    hash_score TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_detection_boxes_record_label ON detection_boxes(record_id, label);

CREATE TABLE IF NOT EXISTS inspection_symptoms (
    symptom_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES inspection_records(record_id),
    symptom_name TEXT NOT NULL,
    symptom_count INTEGER,
    confidence_type TEXT,
    disease_name TEXT,
    disease_count INTEGER,
    disease_confidence REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspection_symptoms_record ON inspection_symptoms(record_id);

-- =====================================================================
-- 跨端只读视图：收敛后端对 robot_core 独占表的读取契约
-- =====================================================================
-- 后端 services/inspection.py 的检测分页此前在源码里硬编码 inference_runs /
-- media_assets 的表结构与「结果图关联规则」（文件名后缀 + captured_at 匹配），
-- 属于共享数据库集成的隐性耦合。该视图由 robot_core（表的 owner）定义，
-- 视图本身即跨端读契约：表结构演进时只改这里，后端无需同步修改。
-- 注：后缀匹配用 strpos 而非 LIKE，避免 DDL 语句中 % 与参数占位符转义的歧义。
CREATE OR REPLACE VIEW v_inference_run_results AS
SELECT ir.run_id, ir.request_id, ir.task_run_id, ir.trace_id, ir.capability,
       ir.model_version, ir.model_sha256, ir.status, ir.result,
       ir.started_at, ir.finished_at, ma.asset_id, ma.uri, ma.mime_type,
       ma.sha256, ma.size_bytes, ma.captured_at,
       ma2.asset_id AS result_asset_id, ma2.uri AS result_uri,
       ma2.mime_type AS result_mime_type
FROM inference_runs ir
JOIN media_assets ma ON ma.asset_id = ir.asset_id
LEFT JOIN media_assets ma2 ON ma2.task_run_id = ir.task_run_id
  AND ma2.device_id = ma.device_id AND ma2.media_type = 'image'
  AND strpos(ma2.uri, '_result.jpg') > 0 AND ma2.captured_at = ma.captured_at;


CREATE TABLE IF NOT EXISTS refresh_tokens (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(128) NOT NULL, 
	expires_at VARCHAR(64) NOT NULL, 
	revoked BOOLEAN NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_refresh_tokens_token_hash ON refresh_tokens (token_hash);


CREATE TABLE IF NOT EXISTS ui_alerts (
	alert_id VARCHAR(128) NOT NULL, 
	level VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	message TEXT NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	payload JSON NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (alert_id)
)

;


CREATE TABLE IF NOT EXISTS ui_config (
	key VARCHAR(128) NOT NULL, 
	value JSON, 
	PRIMARY KEY (key)
)

;


CREATE TABLE IF NOT EXISTS ui_lift (
	id INTEGER NOT NULL, 
	state JSON NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS ui_map (
	id INTEGER NOT NULL, 
	map_version INTEGER NOT NULL, 
	stations JSON NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE IF NOT EXISTS ui_presets (
	preset_id SERIAL NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	payload JSON NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (preset_id)
)

;


CREATE TABLE IF NOT EXISTS ui_runs (
	run_id VARCHAR(128) NOT NULL, 
	task_id VARCHAR(128), 
	status INTEGER NOT NULL, 
	payload JSON NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (run_id)
)

;
CREATE INDEX IF NOT EXISTS ix_ui_runs_task_id ON ui_runs (task_id);


CREATE TABLE IF NOT EXISTS ui_task_templates (
	template_id VARCHAR(128) NOT NULL, 
	payload JSON NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (template_id)
)

;


CREATE TABLE IF NOT EXISTS ui_tasks (
	task_id VARCHAR(128) NOT NULL, 
	payload JSON NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (task_id)
)

;


CREATE TABLE IF NOT EXISTS users (
	id SERIAL NOT NULL, 
	username VARCHAR(128) NOT NULL, 
	password_hash VARCHAR(256) NOT NULL, 
	role VARCHAR(64) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
)

;
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username);
