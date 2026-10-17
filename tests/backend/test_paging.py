"""服务端分页与筛选的行为约定。

这些用例锁住三件事：
1. 不传 page 时保持旧契约（返回数组），老调用方不受影响；
2. 传 page 时返回 {rows,total,page,pageSize}，total 是「筛选后」的总数；
3. 排序稳定，否则翻页会重复或漏项。
"""

import pytest

from app.services.paging import parse_enabled, paginate, sort_rows, filter_rows

# 纯函数用例，不触库：与 test_contracts_wire 等一样标记 no_database，
# 保证没有 PostgreSQL 的机器上也能跑。
pytestmark = pytest.mark.no_database


def _rows(count: int) -> list[dict]:
    return [{"id": index, "createdAt": f"2026-01-{index + 1:02d}T00:00:00Z"} for index in range(count)]


def test_without_page_returns_plain_list():
    """旧调用（HomeView 的 listAlerts({limit:50})）必须还是拿到数组。"""
    result = paginate(_rows(5), None, None)
    assert isinstance(result, list)
    assert len(result) == 5


def test_limit_applies_when_page_is_absent():
    result = paginate(_rows(30), None, 10)
    assert isinstance(result, list)
    assert len(result) == 10


def test_page_returns_envelope_with_total():
    result = paginate(_rows(25), 2, 10)
    assert result["total"] == 25
    assert result["page"] == 2
    assert result["pageSize"] == 10
    assert [row["id"] for row in result["rows"]] == list(range(10, 20))


def test_last_page_is_partial():
    result = paginate(_rows(25), 3, 10)
    assert len(result["rows"]) == 5


def test_page_size_is_clamped():
    # 前端误传 0 或超大值时不能返回空页或整表
    assert paginate(_rows(10), 1, 0)["pageSize"] == 20
    assert paginate(_rows(10), 1, 100000)["pageSize"] == 500


def test_page_before_first_is_treated_as_first():
    result = paginate(_rows(5), 0, 10)
    assert result["page"] == 1
    assert len(result["rows"]) == 5


def test_sort_rows_is_desc_and_missing_last():
    rows = [{"createdAt": "2026-01-02"}, {"createdAt": ""}, {"createdAt": "2026-01-09"}]
    ordered = sort_rows(rows, "createdAt")
    assert [row["createdAt"] for row in ordered] == ["2026-01-09", "2026-01-02", ""]


def test_parse_enabled_three_states():
    assert parse_enabled("on") is True
    assert parse_enabled("off") is False
    assert parse_enabled(None) is None
    assert parse_enabled("") is None


def test_filter_rows_keyword_is_case_insensitive_across_fields():
    rows = [
        {"taskName": "Night Run", "taskId": "T-1"},
        {"taskName": "上午巡检", "taskId": "T-2"},
    ]
    assert len(filter_rows(rows, keyword="night", fields=("taskName", "taskId"))) == 1
    assert len(filter_rows(rows, keyword="t-2", fields=("taskName", "taskId"))) == 1
    assert len(filter_rows(rows, keyword="", fields=("taskName",))) == 2


def test_filter_rows_equals_skips_empty_dimension():
    rows = [{"level": "critical"}, {"level": "info"}]
    assert len(filter_rows(rows, equals={"level": "critical"})) == 1
    # 空值表示「这一维不筛选」，而不是「筛选空字符串」
    assert len(filter_rows(rows, equals={"level": ""})) == 2


def test_filter_and_paginate_compose():
    rows = [{"status": "open", "name": f"a{index}"} for index in range(12)]
    rows += [{"status": "closed", "name": "b"}]
    filtered = filter_rows(rows, equals={"status": "open"})
    page = paginate(filtered, 2, 5)
    assert page["total"] == 12
    assert len(page["rows"]) == 5
