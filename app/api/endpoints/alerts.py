"""告警接口：统一读写 alerts 表（替代旧 ui_alerts）。

路径与前端契约保持不变，因此首页角标、首页最近告警、告警数据页三处会自动
显示采集质量告警与急停告警，无需改动前端。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.deps import get_alerts_service
from app.common.responses import ok, trace_of
from app.services.alerts import AlertsService

router = APIRouter()


@router.get("/alerts", tags=["alerts"])
def list_alerts(
    request: Request,
    page: int | None = Query(default=None, ge=1, description="页码从 1 开始；不传返回全量数组"),
    pageSize: int | None = Query(default=None, ge=1, le=500, description="每页条数"),
    limit: int | None = Query(default=None, ge=1, le=500, description="pageSize 的别名，供首页取最近 N 条"),
    keyword: str = Query(default="", description="按告警内容 / 告警 ID / 执行 ID 模糊搜索"),
    level: str | None = Query(default=None, description="critical / warning / info，缺省不过滤"),
    status: str | None = Query(default=None, description="open / handled / closed，缺省不过滤"),
    service: AlertsService = Depends(get_alerts_service),
) -> dict:
    return ok(
        service.list_alerts(
            page=page,
            page_size=pageSize or limit,
            keyword=keyword,
            level=level,
            status=status,
        ),
        trace_of(request),
    )


@router.get("/alerts/open-count", tags=["alerts"])
def open_alert_count(
    request: Request,
    service: AlertsService = Depends(get_alerts_service),
) -> dict:
    """待处理数量：首页角标用它，避免为拿一个数字拉全表。"""
    return ok({"total": service.count_open()}, trace_of(request))


@router.post("/alerts/{alert_id}", tags=["alerts"])
def handle_alert(
    request: Request,
    alert_id: str,
    body: dict[str, Any] | None = Body(default=None),
    service: AlertsService = Depends(get_alerts_service),
) -> dict:
    return ok(service.handle_alert(alert_id, body), trace_of(request))


@router.delete("/alerts/{alert_id}", tags=["alerts"])
def delete_alert(
    request: Request,
    alert_id: str,
    service: AlertsService = Depends(get_alerts_service),
) -> dict:
    return ok(service.delete_alert(alert_id), trace_of(request))
