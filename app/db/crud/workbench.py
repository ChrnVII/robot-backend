"""工作台数据访问（ui_* 表）：写操作显式提交，返回纯 dict 载荷（保持旧前端契约）。

四类同构实体（任务 / 模板 / 运行 / 告警）此前各自手写 list/upsert/delete 样板，
现经泛型辅助函数收敛；对外函数签名保持不变，service 层无需感知实现变化。
"""

from __future__ import annotations

from typing import Any, Mapping

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


def _payloads(session: Session, model: Any) -> list[dict[str, Any]]:
    """返回全表 payload 列表（工作台接口的统一返回形态）。"""
    return [dict(row.payload or {}) for row in session.scalars(select(model)).all()]


def _upsert_payload(
    session: Session,
    model: Any,
    key_name: str,
    key_value: Any,
    payload: dict[str, Any],
    *,
    create_only: Mapping[str, Any] | None = None,
    update: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按主键 upsert 一行 payload 实体；create_only 字段仅新建时写入。"""
    row = session.get(model, key_value)
    if row is None:
        row = model(**{key_name: key_value, **(create_only or {})})
        session.add(row)
    row.payload = payload
    for name, value in (update or {}).items():
        setattr(row, name, value)
    session.commit()
    return dict(row.payload or {})


def _delete_row(session: Session, model: Any, key_value: Any) -> bool:
    row = session.get(model, key_value)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


# ---------- tasks ----------

def list_tasks(session: Session) -> list[dict[str, Any]]:
    return _payloads(session, TaskRecord)


def get_task(session: Session, task_id: str) -> TaskRecord | None:
    return session.get(TaskRecord, task_id)


def upsert_task(session: Session, task_id: str, payload: dict[str, Any], enabled: bool,
                created_at: str, updated_at: str) -> dict[str, Any]:
    return _upsert_payload(
        session, TaskRecord, "task_id", task_id, payload,
        create_only={"created_at": created_at},
        update={"enabled": enabled, "updated_at": updated_at},
    )


def delete_task(session: Session, task_id: str) -> bool:
    return _delete_row(session, TaskRecord, task_id)


# ---------- task templates ----------

def list_templates(session: Session) -> list[dict[str, Any]]:
    return _payloads(session, TaskTemplate)


def upsert_template(session: Session, template_id: str, payload: dict[str, Any],
                    created_at: str, updated_at: str) -> dict[str, Any]:
    return _upsert_payload(
        session, TaskTemplate, "template_id", template_id, payload,
        create_only={"created_at": created_at},
        update={"updated_at": updated_at},
    )


def delete_template(session: Session, template_id: str) -> bool:
    return _delete_row(session, TaskTemplate, template_id)


# ---------- runs ----------

def list_runs(session: Session) -> list[dict[str, Any]]:
    return _payloads(session, UiRun)


def get_run(session: Session, run_id: str) -> UiRun | None:
    return session.get(UiRun, run_id)


def find_run_by_task(session: Session, task_id: str) -> UiRun | None:
    statement = select(UiRun).where(UiRun.task_id == task_id).order_by(UiRun.created_at.desc())
    return session.scalars(statement).first()


def upsert_run(session: Session, run_id: str, task_id: str | None, status: int,
               payload: dict[str, Any], created_at: str, updated_at: str) -> dict[str, Any]:
    update: dict[str, Any] = {"status": status, "updated_at": updated_at}
    if task_id is not None:
        update["task_id"] = task_id
    return _upsert_payload(
        session, UiRun, "run_id", run_id, payload,
        create_only={"task_id": task_id, "created_at": created_at},
        update=update,
    )


def delete_run(session: Session, run_id: str) -> bool:
    return _delete_row(session, UiRun, run_id)


# ---------- alerts ----------

def list_alerts(session: Session) -> list[dict[str, Any]]:
    return _payloads(session, UiAlert)


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
    return _delete_row(session, UiAlert, alert_id)


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
    return _delete_row(session, Preset, preset_id)


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
