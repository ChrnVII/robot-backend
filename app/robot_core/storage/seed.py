from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

from app.robot_core.common import utc_now
from app.robot_core.common.config import redact_database_url
from app.robot_core.contracts import DeviceState, HealthState

from .database import SCHEMA_VERSION, PlatformStore

# 场地规范：两个临近鸡舍，均为 8 排、6 层。
# 鸡舍 1 每排 132 列，鸡舍 2 每排 160 列。
COOP_SPECS = [
    {"coop_no": 1, "name": "1号鸡舍", "row_count": 8, "column_count": 132, "layer_count": 6},
    {"coop_no": 2, "name": "2号鸡舍", "row_count": 8, "column_count": 160, "layer_count": 6},
]

FARM_ID = "farm-001"


def format_coop_code(coop_no: int, row_no: int, column_no: int) -> str:
    """鸡舍编码：鸡舍号-排号-列号，列号不足三位左侧补零。"""
    return f"{coop_no}-{row_no}-{column_no:03d}"


def format_point_agv(coop_no: int, column_no: int) -> str:
    """AGV 停车/巡检点位编号：第 1 位为鸡舍号，后 3 位为列号，不含排号和层号。"""
    return f"{coop_no}{column_no:03d}"


def format_pointlayer_code(coop_code: str, point_agv: str, layer_no: int) -> str:
    """鸡笼唯一点位编码：鸡舍编码-AGV点位-层号。"""
    return f"{coop_code}-{point_agv}-{layer_no}"


def build_domain() -> Dict[str, Any]:
    """生成农场、鸡舍、AGV 点位和鸡笼的完整静态清单。"""
    now = utc_now()
    farm = {
        "farm_id": FARM_ID,
        "name": "示范鸡场",
        "site_code": "DEMO-FARM",
        "timezone": "Asia/Shanghai",
        "created_at": now,
        "updated_at": now,
    }
    coops: List[Dict[str, Any]] = []
    agv_points: List[Dict[str, Any]] = []
    cages: List[Dict[str, Any]] = []
    for spec in COOP_SPECS:
        coop_no = spec["coop_no"]
        coop_id = f"coop-{coop_no}"
        coops.append({
            "coop_id": coop_id,
            "farm_id": FARM_ID,
            "coop_no": coop_no,
            "name": spec["name"],
            "row_count": spec["row_count"],
            "column_count": spec["column_count"],
            "layer_count": spec["layer_count"],
            "created_at": now,
            "updated_at": now,
        })
        for column_no in range(1, spec["column_count"] + 1):
            point_agv = format_point_agv(coop_no, column_no)
            point_id = f"point-{point_agv}"
            agv_points.append({
                "point_id": point_id,
                "coop_id": coop_id,
                "coop_no": coop_no,
                "column_no": column_no,
                "point_agv": point_agv,
                "rfid_tag": f"RFID-{point_agv}",
                "map_x": None,
                "map_y": None,
                "enabled": 1,
                "created_at": now,
                "updated_at": now,
            })
            for row_no in range(1, spec["row_count"] + 1):
                for layer_no in range(1, spec["layer_count"] + 1):
                    coop_code = format_coop_code(coop_no, row_no, column_no)
                    pointlayer_code = format_pointlayer_code(coop_code, point_agv, layer_no)
                    cages.append({
                        "cage_id": f"cage-{point_agv}-{row_no}-{layer_no}",
                        "coop_id": coop_id,
                        "point_id": point_id,
                        "coop_no": coop_no,
                        "row_no": row_no,
                        "column_no": column_no,
                        "layer_no": layer_no,
                        "coop_code": coop_code,
                        "pointlayer_code": pointlayer_code,
                        "enabled": 1,
                        "created_at": now,
                    })
    return {"farm": farm, "coops": coops, "agv_points": agv_points, "cages": cages}


def write_domain(store: PlatformStore, domain: Dict[str, Any]) -> None:
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO farms(farm_id, name, site_code, timezone, created_at, updated_at)
               VALUES (:farm_id, :name, :site_code, :timezone, :created_at, :updated_at)""",
            domain["farm"],
        )
        connection.executemany(
            """INSERT INTO coops(coop_id, farm_id, coop_no, name, row_count, column_count, layer_count, created_at, updated_at)
               VALUES (:coop_id, :farm_id, :coop_no, :name, :row_count, :column_count, :layer_count, :created_at, :updated_at)""",
            domain["coops"],
        )
        connection.executemany(
            """INSERT INTO agv_points(point_id, coop_id, coop_no, column_no, point_agv, rfid_tag, map_x, map_y, enabled, created_at, updated_at)
               VALUES (:point_id, :coop_id, :coop_no, :column_no, :point_agv, :rfid_tag, :map_x, :map_y, :enabled, :created_at, :updated_at)""",
            domain["agv_points"],
        )
        connection.executemany(
            """INSERT INTO cages(cage_id, coop_id, point_id, coop_no, row_no, column_no, layer_no, coop_code, pointlayer_code, enabled, created_at)
               VALUES (:cage_id, :coop_id, :point_id, :coop_no, :row_no, :column_no, :layer_no, :coop_code, :pointlayer_code, :enabled, :created_at)""",
            domain["cages"],
        )


def write_sample_inspection(store: PlatformStore) -> None:
    """写入一条与旧库样例对齐的巡检记录，用于验证字段映射。

    旧库唯一可靠样例：coop=1-2-002，point_agv=1002，layer=1。
    对应的鸡笼为 1 号鸡舍、第 2 排、第 2 列、第 1 层，pointlayer_code=1-2-002-1002-1。
    指标数值在旧历史库中未保留，因此此处仅填充身份字段，指标字段保持 NULL。
    """
    now = utc_now()
    captured_at = "2021-07-12T16:00:00+00:00"  # 旧库仅有日期 2021-07-13，折算为 UTC
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO robots(robot_id, name, serial_number, site_code, enabled, created_at, updated_at)
               VALUES ('robot-001', '巡检机器人1号', NULL, 'DEMO-FARM', 1, ?, ?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO missions(mission_id, name, revision, topology_revision_id, definition, state, created_by, created_at, approved_by, approved_at)
               VALUES ('mission-001', '1号鸡舍全量巡检', 1, NULL, ?, 'approved', 'system', ?, NULL, NULL)""",
            (json.dumps({"coop_no": 1, "all_columns": True}, ensure_ascii=False), now),
        )
        connection.execute(
            """INSERT INTO mission_runs(task_run_id, mission_id, robot_id, trace_id, state, current_step, current_waypoint_id, config_revision_id, started_at, updated_at, finished_at, failure_code, recovery_state)
               VALUES ('run-legacy-001', 'mission-001', 'robot-001', 'trace-legacy-001', 'completed', NULL, NULL, NULL, ?, ?, ?, NULL, NULL)""",
            (captured_at, captured_at, captured_at),
        )
        connection.execute(
            """INSERT INTO inspection_records(record_id, task_run_id, cage_id, coop_no, point_agv, layer_no, captured_at, created_at)
               VALUES ('record-legacy-001', 'run-legacy-001', 'cage-1002-2-1', 1, '1002', 1, ?, ?)""",
            (captured_at, now),
        )


def write_sample_device_states(store: PlatformStore) -> None:
    """Add representative local state samples so the prototype demonstrates the history path."""
    sampled_at = utc_now()
    store.upsert_device_state(DeviceState(
        "environment", HealthState.OK, sampled_at, "prototype",
        {"temperature_c": 24.8, "humidity_percent": 68.5,
         "ammonia_ppm": 12.3, "illuminance_lux": 320.0},
    ))
    store.upsert_device_state(DeviceState(
        "motion", HealthState.OK, sampled_at, "prototype",
        {"moving": False, "position_m": 0.0, "battery_percent": 87.0},
    ))


def seed_prototype(store: PlatformStore, *, include_sample: bool = True) -> None:
    write_domain(store, build_domain())
    if include_sample:
        write_sample_inspection(store)
        write_sample_device_states(store)


_SEEDED_TABLES = (
    "farms", "coops", "agv_points", "cages",
    "robots", "missions", "mission_runs", "inspection_records",
    "device_states", "device_state_history",
)


def clear_seeded_tables(store: PlatformStore) -> None:
    """清空种子数据表（--force 时使用）；TRUNCATE CASCADE 处理外键依赖。"""
    with store.connect() as connection:
        for table in _SEEDED_TABLES:
            connection.execute(f"TRUNCATE TABLE {table} CASCADE")


def main() -> None:
    parser = argparse.ArgumentParser(prog="robot-seed", description="生成巡检域原型数据（PostgreSQL）")
    parser.add_argument(
        "--database",
        required=True,
        help="目标 PostgreSQL DSN（如 postgresql+psycopg://robot:***@127.0.0.1:5432/robot_platform）",
    )
    parser.add_argument("--force", action="store_true", help="目标库已有种子数据时，先清空种子表再重建")
    args = parser.parse_args()
    store = PlatformStore(args.database)
    try:
        if args.force:
            clear_seeded_tables(store)
        seed_prototype(store)
        print(json.dumps(
            {"status": "ok", "database": redact_database_url(store.database_url), "revision": SCHEMA_VERSION},
            ensure_ascii=False,
        ))
    finally:
        store.close()


if __name__ == "__main__":
    main()
