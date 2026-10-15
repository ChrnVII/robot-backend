"""工作台数据访问（ui_* 表）：写操作显式提交，返回纯 dict 载荷（保持旧前端契约）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ConfigEntry,
    LiftState,
    MapState,
    Preset,
    TaskRecord,
    TaskTemplate,
    UiAlert,
    UiRun,
)


# ---------- tasks ----------

def list_tasks(session: Session) -> list[dict[str, Any]]:
    return [dict(row.payload or {}) for row in session.scalars(select(TaskRecord)).all()]


def get_task(session: Session, task_id: str) -> TaskRecord | None:
    return session.get(TaskRecord, task_id)


def upsert_task(session: Session, task_id: str, payload: dict[str, Any], enabled: bool,
                created_at: str, updated_at: str) -> dict[str, Any]:
    row = session.get(TaskRecord, task_id)
    if row is None:
        row = TaskRecord(task_id=task_id, created_at=created_at)
        session.add(row)
    row.payload = payload
    row.enabled = enabled
    row.updated_at = updated_at
    session.commit()
    return dict(row.payload or {})


def delete_task(session: Session, task_id: str) -> bool:
    row = session.get(TaskRecord, task_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---------- task templates ----------

def list_templates(session: Session) -> list[dict[str, Any]]:
    return [dict(row.payload or {}) for row in session.scalars(select(TaskTemplate)).all()]


def upsert_template(session: Session, template_id: str, payload: dict[str, Any],
                    created_at: str, updated_at: str) -> dict[str, Any]:
    row = session.get(TaskTemplate, template_id)
    if row is None:
        row = TaskTemplate(template_id=template_id, created_at=created_at)
        session.add(row)
    row.payload = payload
    row.updated_at = updated_at
    session.commit()
    return dict(row.payload or {})


def delete_template(session: Session, template_id: str) -> bool:
    row = session.get(TaskTemplate, template_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---------- runs ----------

def list_runs(session: Session) -> list[dict[str, Any]]:
    return [dict(row.payload or {}) for row in session.scalars(select(UiRun)).all()]


def get_run(session: Session, run_id: str) -> UiRun | None:
    return session.get(UiRun, run_id)


def find_run_by_task(session: Session, task_id: str) -> UiRun | None:
    statement = select(UiRun).where(UiRun.task_id == task_id).order_by(UiRun.created_at.desc())
    return session.scalars(statement).first()


def upsert_run(session: Session, run_id: str, task_id: str | None, status: int,
               payload: dict[str, Any], created_at: str, updated_at: str) -> dict[str, Any]:
    row = session.get(UiRun, run_id)
    if row is None:
        row = UiRun(run_id=run_id, task_id=task_id, created_at=created_at)
        session.add(row)
    if task_id is not None:
        row.task_id = task_id
    row.status = status
    row.payload = payload
    row.updated_at = updated_at
    session.commit()
    return dict(row.payload or {})


def delete_run(session: Session, run_id: str) -> bool:
    row = session.get(UiRun, run_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---------- alerts ----------

def list_alerts(session: Session) -> list[dict[str, Any]]:
    return [dict(row.payload or {}) for row in session.scalars(select(UiAlert)).all()]


def get_or_create_alert(session: Session, alert_id: str) -> UiAlert:
    row = session.get(UiAlert, alert_id)
    if row is None:
        row = UiAlert(alert_id=alert_id, payload={})
        session.add(row)
        session.flush()
    return row


def save_alert(session: Session, row: UiAlert, payload: dict[str, Any], *, level: str | None = None,
               status: str | None = None, message: str | None = None, source: str | None = None,
               updated_at: str = "") -> dict[str, Any]:
    row.payload = payload
    if level is not None:
        row.level = level
    if status is not None:
        row.status = status
    if message is not None:
        row.message = message
    if source is not None:
        row.source = source
    if updated_at:
        row.updated_at = updated_at
    session.commit()
    return dict(row.payload or {})


def delete_alert(session: Session, alert_id: str) -> bool:
    row = session.get(UiAlert, alert_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---------- presets ----------

def list_presets(session: Session) -> list[dict[str, Any]]:
    statement = select(Preset).order_by(Preset.preset_id)
    return [dict(row.payload or {}) for row in session.scalars(statement).all()]


def get_or_create_preset(session: Session, preset_id: int) -> Preset:
    row = session.get(Preset, preset_id)
    if row is None:
        name = f"预置点 {preset_id}"
        row = Preset(preset_id=preset_id, name=name, payload={"presetId": preset_id, "name": name})
        session.add(row)
        session.commit()
    return row


def save_preset(session: Session, row: Preset, payload: dict[str, Any], name: str, updated_at: str) -> dict[str, Any]:
    row.payload = payload
    row.name = name
    row.updated_at = updated_at
    session.commit()
    return dict(row.payload or {})


def delete_preset(session: Session, preset_id: int) -> bool:
    row = session.get(Preset, preset_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---------- config (kv) ----------

def get_config(session: Session) -> dict[str, Any]:
    return {row.key: row.value for row in session.scalars(select(ConfigEntry)).all()}


def save_config(session: Session, values: dict[str, Any]) -> None:
    for key, value in values.items():
        row = session.get(ConfigEntry, key)
        if row is None:
            row = ConfigEntry(key=key)
            session.add(row)
        row.value = value
    session.commit()


# ---------- map ----------

def get_or_create_map(session: Session) -> MapState:
    row = session.get(MapState, 1)
    if row is None:
        row = MapState(id=1, map_version=1, stations=[])
        session.add(row)
        session.commit()
    return row


def save_map(session: Session, row: MapState, stations: list[dict[str, Any]], map_version: int) -> None:
    row.stations = stations
    row.map_version = map_version
    session.commit()


# ---------- lift ----------

def get_or_create_lift(session: Session) -> LiftState:
    row = session.get(LiftState, 1)
    if row is None:
        row = LiftState(id=1, state={})
        session.add(row)
        session.commit()
    return row


def save_lift(session: Session, row: LiftState, state: dict[str, Any]) -> None:
    row.state = state
    session.commit()
