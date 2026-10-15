from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from app.robot_core.common import utc_now


class HealthAggregator:
    def aggregate(self, reports: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
        items = [dict(report) for report in reports]
        states = {str(item.get("state", "unavailable")) for item in items}
        overall = "fault" if "fault" in states else "degraded" if states - {"ok"} else "ok"
        return {"state": overall, "timestamp": utc_now(), "services": items}

