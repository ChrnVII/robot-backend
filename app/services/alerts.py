"""告警服务：统一读写 ``alerts`` 表，替代旧 ``ui_alerts`` 影子表。

为什么要换：
- ``ui_alerts`` 是工作台的自由 JSON 表，只有急停会写，采集质量告警写在规范表 ``alerts``；
- 结果同一件事在两个表里，首页角标看不见采集告警。

前端契约保持不变（``alertId / level / status / type / content / createdAt / result / taskRunId``），
只在服务层做字段映射：

| alerts（表）        | 前端字段   |
| ------------------ | --------- |
| alert_id           | alertId   |
| severity           | level     |
| state              | status（open / handled / closed） |
| alert_type         | type      |
| message            | content   |
| closed_reason      | result    |
| source_run_id      | taskRunId |
| created_at         | createdAt |
| acknowledged_at    | handledAt |

``state`` 与 ``status`` 不是一一对应：库里是 open / acknowledged / closed，
前端只认 open / handled / closed，acknowledged 要映射成 handled。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.common.clock import utc_now_iso
from app.common.exceptions import AppError, ValidationFailed

ALERT_LEVELS = ("critical", "warning", "info")
_STATUS_TO_STATE = {"open": "open", "handled": "acknowledged", "closed": "closed"}
_STATE_TO_STATUS = {"open": "open", "acknowledged": "handled", "closed": "closed"}

_COLUMNS = ("alert_id, severity, state, alert_type, message, source_run_id,"
            " created_at, acknowledged_by, acknowledged_at, closed_reason")


def _to_public(row) -> dict[str, Any]:
    return {
        "alertId": row["alert_id"],
        "level": row["severity"],
        "status": _STATE_TO_STATUS.get(row["state"], row["state"]),
        "type": row["alert_type"] or "",
        "content": row["message"],
        "result": row["closed_reason"] or "",
        "createdAt": row["created_at"],
        "handledAt": row["acknowledged_at"] or "",
        "handledBy": row["acknowledged_by"] or "",
        "taskRunId": row["source_run_id"] or "",
        # 旧页面的站点/鸡笼/图片列：新链路暂无这些维度，返回空让前端显示占位
        "station": None,
        "cage": None,
        "layer": None,
        "imageName": "",
    }


class AlertsService:
    """告警的读写入口。列表/处理/删除都走这里。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- 读 --------------------------------------------------------------
    def list_alerts(
        self,
        *,
        page: int | None = None,
        page_size: int | None = None,
        keyword: str = "",
        level: str | None = None,
        status: str | None = None,
    ) -> Any:
        where, params = self._filter(keyword, level, status)

        # page 为 None 时返回数组（兼容旧契约：dashboard 取最近 N 条）
        if page is None:
            limit = max(1, min(int(page_size or 500), 500))
            rows = self._session.execute(
                text(f"SELECT {_COLUMNS} FROM alerts{where} ORDER BY created_at DESC LIMIT :limit"),
                {**params, "limit": limit},
            ).mappings()
            return [_to_public(row) for row in rows]

        size = max(1, min(int(page_size or 20), 500))
        offset = (max(1, int(page)) - 1) * size
        total = self._session.execute(
            text(f"SELECT count(*) AS total FROM alerts{where}"), params
        ).mappings().first()
        rows = self._session.execute(
            text(f"SELECT {_COLUMNS} FROM alerts{where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
            {**params, "limit": size, "offset": offset},
        ).mappings()
        return {
            "rows": [_to_public(row) for row in rows],
            "total": int(total["total"]) if total else 0,
            "page": max(1, int(page)),
            "pageSize": size,
        }

    @staticmethod
    def _filter(keyword: str, level: str | None, status: str | None):
        clauses: list[str] = []
        params: dict[str, Any] = {}
        text_value = (keyword or "").strip()
        if text_value:
            clauses.append(
                "(message ILIKE :kw OR alert_id ILIKE :kw OR coalesce(source_run_id, '') ILIKE :kw)"
            )
            params["kw"] = f"%{text_value}%"
        if level:
            clauses.append("severity = :level")
            params["level"] = level
        if status:
            clauses.append("state = :state")
            params["state"] = _STATUS_TO_STATE.get(status, status)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def count_open(self) -> int:
        row = self._session.execute(
            text("SELECT count(*) AS total FROM alerts WHERE state = 'open'")
        ).mappings().first()
        return int(row["total"]) if row else 0

    # -- 写 --------------------------------------------------------------
    def create_alert(self, *, level: str, content: str, alert_type: str = "",
                     trace_id: str = "", source_run_id: str | None = None,
                     evidence_asset_id: str | None = None,
                     commit: bool = True) -> dict[str, Any]:
        if level not in ALERT_LEVELS:
            raise ValidationFailed(f"告警等级非法：{level}（可选 {'/'.join(ALERT_LEVELS)}）")
        alert_id = f"alert-{uuid4().hex[:16]}"
        now = utc_now_iso()
        self._session.execute(
            text("INSERT INTO alerts(alert_id, trace_id, source_run_id, severity, state,"
                 " alert_type, evidence_asset_id, message, created_at)"
                 " VALUES (:alert, :trace, :run, :severity, 'open', :type, :evidence, :message, :now)"),
            {
                "alert": alert_id, "trace": trace_id or f"trace-{uuid4().hex[:12]}",
                "run": source_run_id, "severity": level, "type": alert_type or None,
                "evidence": evidence_asset_id, "message": content, "now": now,
            },
        )
        if commit:
            self._session.commit()
        return {"alertId": alert_id, "level": level, "status": "open",
                "type": alert_type, "content": content, "createdAt": now,
                "taskRunId": source_run_id or ""}

    def handle_alert(self, alert_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self._row(alert_id)
        payload = body or {}
        target = str(payload.get("status") or "handled")
        if target not in _STATUS_TO_STATE:
            raise ValidationFailed(f"处理状态非法：{target}（可选 open/handled/closed）")
        state = _STATUS_TO_STATE[target]
        operator = str(payload.get("operator") or "operator")
        result = str(payload.get("result") or "")
        now = utc_now_iso()

        self._session.execute(
            text("UPDATE alerts SET state = :state,"
                 " acknowledged_by = COALESCE(NULLIF(:operator, ''), acknowledged_by),"
                 " acknowledged_at = :now, closed_reason = :reason WHERE alert_id = :alert"),
            {"state": state, "operator": operator, "now": now,
             "reason": result or row["closed_reason"], "alert": alert_id},
        )
        self._session.commit()
        return _to_public(self._row(alert_id))

    def delete_alert(self, alert_id: str) -> dict[str, Any]:
        row = self._row(alert_id)
        # 安全类告警是审计留痕，删掉就再也查不到"什么时候按过急停"
        if (row["alert_type"] or "") == "safety":
            raise ValidationFailed("安全类告警不可删除（审计留痕），请改为关闭")
        self._session.execute(text("DELETE FROM alerts WHERE alert_id = :alert"), {"alert": alert_id})
        self._session.commit()
        return {"alertId": alert_id, "deleted": True}

    def _row(self, alert_id: str):
        row = self._session.execute(
            text(f"SELECT {_COLUMNS} FROM alerts WHERE alert_id = :alert"), {"alert": alert_id}
        ).mappings().first()
        if row is None:
            raise AppError(f"告警不存在：{alert_id}", code="ALERT_NOT_FOUND", status_code=404)
        return row
