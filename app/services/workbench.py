"""工作台服务：任务 / 模板 / 运行 / 告警 / 地图 / 参数 / 预置点 / 升降。

迁移来源：UiApplicationService.java + UiController.java。
原 Java 实现为「内存态 + JSON 文件」；本迁移改为 SQLAlchemy 落库（app.db.crud.workbench），
载荷结构保持自由 JSON，前端契约零改动。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.common.clock import utc_now_iso
from app.common.config import Settings
from app.common.exceptions import ValidationFailed
from app.db import crud

_DEFAULT_LIFT: dict[str, Any] = {
    "height": 0, "faultCode": 0, "enabled": False, "motion": "idle",
    "speed": 4096, "accel": 100, "downLimit": -20, "upLimit": 1140,
}

_TASK_COMMAND_STATUS = {
    "motion.pause": 2,
    "motion.stop": 100,
    "motion.clear_fault": 100,
    "motion.clear_alarm": 100,
    "motion.resume": 1,
    "motion.start_task": 1,
    "motion.goto_station": 1,
}


def _short_id() -> str:
    return uuid4().hex[:12]


def _default_station() -> dict[str, Any]:
    return {
        "stationId": "st-1", "station": 1, "type": "inspection", "cage": 1,
        "x": 0, "y": 0, "yaw": 0, "returnStation": False, "nextSpeed": 0.45,
        "heightCheck": True, "waitMs": 3000, "soundInterference": False,
        "takePhoto": True, "collectEnvironment": True, "recordAudio": False,
    }


class WorkbenchService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ---------- tasks ----------

    def list_tasks(self) -> list[dict[str, Any]]:
        return crud.workbench.list_tasks(self._session)

    def save_task(self, body: dict[str, Any] | None) -> dict[str, Any]:
        now = utc_now_iso()
        source = dict(body or {})
        task_id = str(source.get("taskId") or f"task-{_short_id()}")
        existing = crud.workbench.get_task(self._session, task_id)
        created_at = source.get("createdAt")
        if not created_at:
            created_at = (existing.payload or {}).get("createdAt") if existing is not None else None
        created_at = created_at or now
        value = {**source, "taskId": task_id, "createdAt": created_at}
        value.setdefault("enabled", True)
        value["updatedAt"] = now
        return crud.workbench.upsert_task(
            self._session, task_id, value, bool(value.get("enabled", True)),
            str(created_at), now,
        )

    def delete_task(self, task_id: str) -> None:
        crud.workbench.delete_task(self._session, task_id)

    # ---------- task templates ----------

    def list_templates(self) -> list[dict[str, Any]]:
        return crud.workbench.list_templates(self._session)

    def save_template(self, body: dict[str, Any] | None) -> dict[str, Any]:
        now = utc_now_iso()
        source = dict(body or {})
        template_id = str(source.get("templateId") or f"tmpl-{_short_id()}")
        value = {**source, "templateId": template_id}
        value.setdefault("createdAt", now)
        value["updatedAt"] = now
        return crud.workbench.upsert_template(
            self._session, template_id, value, str(value.get("createdAt", "")), now,
        )

    def delete_template(self, template_id: str) -> None:
        crud.workbench.delete_template(self._session, template_id)

    # ---------- runs ----------

    def list_runs(self) -> list[dict[str, Any]]:
        return crud.workbench.list_runs(self._session)

    def resume_run(self, run_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
        now = utc_now_iso()
        row = crud.workbench.get_run(self._session, run_id)
        payload = dict(row.payload or {}) if row is not None else {"taskRunId": run_id}
        payload["taskRunId"] = payload.get("taskRunId", run_id)
        payload["status"] = 1
        payload["updatedAt"] = now
        if body:
            payload.update(body)
        return crud.workbench.upsert_run(
            self._session, run_id, row.task_id if row is not None else payload.get("taskId"),
            1, payload, str(payload.get("startTime", now)), now,
        )

    def delete_run(self, run_id: str) -> None:
        crud.workbench.delete_run(self._session, run_id)

    def create_run(self, task_id: str) -> dict[str, Any]:
        run_id = f"run-{_short_id()}"
        now = utc_now_iso()
        payload = {
            "taskRunId": run_id, "taskId": task_id, "status": 1,
            "startTime": now, "currentPosition": 0, "stationTotal": 0,
        }
        return crud.workbench.upsert_run(self._session, run_id, task_id, 1, payload, now, now)

    def apply_task_command(self, task_id: str | None, action: str, result: dict[str, Any] | None) -> None:
        """把命令生命周期映射到运行记录（对齐旧 applyTaskCommand）。"""
        if not task_id:
            return
        row = crud.workbench.find_run_by_task(self._session, task_id)
        if row is None:
            self.create_run(task_id)
            row = crud.workbench.find_run_by_task(self._session, task_id)
        if row is None:  # pragma: no cover - defensive guard
            return
        payload = dict(row.payload or {})
        status = _TASK_COMMAND_STATUS.get(action)
        if status is not None:
            payload["status"] = status
        payload["updatedAt"] = utc_now_iso()
        if result is not None:
            payload["lastCommand"] = result
        crud.workbench.upsert_run(
            self._session, row.run_id, task_id, int(payload.get("status", row.status)),
            payload, row.created_at, payload["updatedAt"],
        )

    # ---------- alerts ----------

    def list_alerts(self) -> list[dict[str, Any]]:
        return crud.workbench.list_alerts(self._session)

    def create_alert(self, level: str, message: str, source: str) -> dict[str, Any]:
        now = utc_now_iso()
        alert_id = f"alert-{_short_id()}"
        value = {
            "alertId": alert_id, "level": level, "status": "open",
            "message": message, "source": source, "createdAt": now,
        }
        row = crud.workbench.get_or_create_alert(self._session, alert_id)
        return crud.workbench.save_alert(
            self._session, row, value, level=level, status="open", message=message, source=source,
            updated_at=now,
        )

    def handle_alert(self, alert_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
        now = utc_now_iso()
        row = crud.workbench.get_or_create_alert(self._session, alert_id)
        payload = dict(row.payload or {})
        payload.setdefault("alertId", alert_id)
        if body:
            payload.update(body)
        payload["updatedAt"] = now
        status = payload.get("status") if isinstance(payload.get("status"), str) else None
        return crud.workbench.save_alert(self._session, row, payload, status=status, updated_at=now)

    def delete_alert(self, alert_id: str) -> None:
        crud.workbench.delete_alert(self._session, alert_id)

    # ---------- config ----------

    def get_config(self) -> dict[str, Any]:
        defaults = {
            "robotId": self._settings.robot_id,
            "name": "巡检机器人",
            "type": "inspection",
            "linkFlag": "1",
            "clientUrl": "",
        }
        stored = crud.workbench.get_config(self._session)
        return {**defaults, **stored}

    def inspection_config(self) -> dict[str, Any]:
        return {
            "environments": [
                {"value": "simulated", "label": "simulated（默认）", "detail": "支持当前模拟程序运行"},
                {"value": "hardware", "label": "hardware", "detail": "生产巡检环境"},
            ],
            "environment": self._settings.mode,
            "modelPath": "models/best.pt",
            "configPath": "configs/inspection.yaml",
            "resultPath": "results/inspection",
        }

    def save_config(self, values: dict[str, Any] | None) -> dict[str, Any]:
        updates = {key: value for key, value in (values or {}).items() if key != "robotId" and value is not None}
        if updates:
            crud.workbench.save_config(self._session, updates)
        return self.get_config()

    # ---------- map ----------

    def get_map(self) -> dict[str, Any]:
        row = crud.workbench.get_or_create_map(self._session)
        stations = list(row.stations or [])
        if not stations:
            stations = [_default_station()]
        return {"mapVersion": row.map_version, "stations": stations}

    def save_map(self, body: dict[str, Any] | None) -> dict[str, Any]:
        row = crud.workbench.get_or_create_map(self._session)
        requested = (body or {}).get("mapVersion")
        if requested is not None and int(requested) != row.map_version:
            raise ValidationFailed("地图版本已变化，请重新读取后再保存")
        stations = (body or {}).get("stations")
        if isinstance(stations, list):
            row_stations = [dict(item) for item in stations if isinstance(item, dict)]
        else:
            row_stations = list(row.stations or [])
        crud.workbench.save_map(self._session, row, row_stations, row.map_version + 1)
        return self.get_map()

    # ---------- presets ----------

    def list_presets(self) -> list[dict[str, Any]]:
        rows = crud.workbench.list_presets(self._session)
        if not rows:
            # 与旧实现一致：预置点为空时给出默认「预置点 1」
            crud.workbench.get_or_create_preset(self._session, 1)
            rows = crud.workbench.list_presets(self._session)
        return rows

    def save_preset(self, preset_id: int) -> dict[str, Any]:
        now = utc_now_iso()
        row = crud.workbench.get_or_create_preset(self._session, preset_id)
        payload = dict(row.payload or {})
        payload.setdefault("presetId", preset_id)
        payload.setdefault("name", row.name or f"预置点 {preset_id}")
        return crud.workbench.save_preset(self._session, row, payload, str(payload["name"]), now)

    def delete_preset(self, preset_id: int) -> None:
        crud.workbench.delete_preset(self._session, preset_id)

    # ---------- lift ----------

    def lift_state(self) -> dict[str, Any]:
        row = crud.workbench.get_or_create_lift(self._session)
        state = {**_DEFAULT_LIFT, **dict(row.state or {})}
        return state

    def apply_lift(self, action: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        row = crud.workbench.get_or_create_lift(self._session)
        state = {**_DEFAULT_LIFT, **dict(row.state or {})}
        payload = payload or {}
        if action == "lift.set_params":
            state.update(payload)
        elif action == "lift.move_to":
            state["height"] = payload.get("height")
            state["motion"] = "positioning"
        elif action == "lift.up":
            state["motion"] = "moving_up"
        elif action == "lift.down":
            state["motion"] = "moving_down"
        elif action == "lift.home":
            state["height"] = 0
            state["motion"] = "homing"
        elif action == "lift.enable":
            state["enabled"] = payload.get("enabled")
        elif action == "lift.clear_fault":
            state["faultCode"] = 0
        crud.workbench.save_lift(self._session, row, state)
        return state
