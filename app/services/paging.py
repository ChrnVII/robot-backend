"""工作台列表的服务端筛选与分页。

为什么在服务层做而不是下推到 SQL：
``ui_tasks`` / ``ui_runs`` / ``ui_alerts`` 的业务字段放在自由 JSON ``payload`` 列里，
下推需要维护一套 ``payload ->> 'field'`` 的 SQL，字段随前端契约变化就会失配。
这几类数据在千级，Python 侧过滤的成本远低于维护 JSON SQL；
接口形状（rows/total/page/pageSize）保持不变，后续数据量上来时可无痛替换实现。

契约兼容：``page`` 缺省时返回数组（旧行为），只有显式传分页参数才返回分页对象，
因此 HomeView 的 ``listAlerts({limit: 50})`` 之类的旧调用不受影响。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: 单页上限：既防止前端误传 0 或超大值，也限制单次响应体积。
MAX_PAGE_SIZE = 500


def parse_enabled(value: Any) -> bool | None:
    """把 ``on/off/true/false/1/0`` 归一成三态：True / False / None（不限）。"""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"on", "true", "1", "yes"}:
        return True
    if text in {"off", "false", "0", "no"}:
        return False
    return None


def matches_keyword(row: Mapping[str, Any], keyword: str, fields: Sequence[str]) -> bool:
    """子串匹配多个字段，与前端此前的 ``includes`` 语义一致（大小写不敏感）。"""
    text = str(keyword or "").strip().lower()
    if not text:
        return True
    return any(text in str(row.get(field) or "").lower() for field in fields)


def matches_equals(row: Mapping[str, Any], equals: Mapping[str, Any]) -> bool:
    """精确匹配若干字段；值为 None / 空串表示该维度不筛选。"""
    for field, expected in equals.items():
        if expected is None or expected == "":
            continue
        if str(row.get(field) or "") != str(expected):
            return False
    return True


def sort_rows(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """按时间字段倒序，缺失值的行排在最后。

    服务端分页必须有稳定顺序，否则翻页会出现重复或漏项；
    原实现在客户端不做排序（依赖数据库返回顺序），分页后必须显式排。
    """
    return sorted(rows, key=lambda row: str(row.get(field) or ""), reverse=True)


def paginate(rows: list[dict[str, Any]], page: int | None, page_size: int | None) -> Any:
    """按需求返回分页对象或数组。

    - ``page`` 为 None：返回数组（兼容旧调用），``page_size`` 作为 limit 生效；
    - 否则返回 ``{rows, total, page, pageSize}``，total 为筛选后的总条数。
    """
    if page is None:
        if page_size:
            return rows[: max(1, min(int(page_size), MAX_PAGE_SIZE))]
        return rows

    size = max(1, min(int(page_size or 20), MAX_PAGE_SIZE))
    start = (max(1, int(page)) - 1) * size
    return {
        "rows": rows[start : start + size],
        "total": len(rows),
        "page": max(1, int(page)),
        "pageSize": size,
    }


def filter_rows(
    rows: Iterable[dict[str, Any]],
    *,
    keyword: str = "",
    fields: Sequence[str] = (),
    equals: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """关键词 + 精确匹配的组合筛选，返回新列表。"""
    return [
        row
        for row in rows
        if matches_keyword(row, keyword, fields) and matches_equals(row, equals or {})
    ]
