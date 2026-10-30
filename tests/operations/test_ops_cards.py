"""离线桩卡与故障码卡（#698，PRD #693 的 ③ 号路径）。

与失败率卡同一张面、同一套规则：数字确定性、模型不参与、空范围短路、窗外不计、
consumer 不可达。这里只断言**这两张卡自己的**那几条——范围与时间窗语义由
``test_ops_analysis.py`` 覆盖且实现共用，不重复第三遍。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.answer_language import answer_chinese_leak
from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.data.sources import MySQLSource
from aiops_diagnostics.identity.query_scope import QueryScope
from aiops_diagnostics.operator.ops_analysis import (
    REASON_NO_FAULTS,
    REASON_NO_INCIDENTS,
    REASON_PROSE_LEAKED,
    REASON_SCOPE_EMPTY,
    RULE_VERSION,
    FaultCodeAggregate,
    OfflineAggregate,
    build_fault_code_contract,
    build_offline_contract,
)

WINDOW_START = datetime(2026, 10, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 2, tzinfo=UTC)


def _window(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "language": "en",
        "site_ids": ("S-1",),
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }
    base.update(overrides)
    return base


def _offline(**overrides: Any) -> OfflineAggregate:
    fields: dict[str, Any] = {
        "incidents": 12,
        "offline_devices": 9,
        "open_incidents": 4,
        "offline_minutes": 480,
        "devices_total": 100,
    }
    fields.update(overrides)
    return OfflineAggregate(**fields)


def _faults(**overrides: Any) -> FaultCodeAggregate:
    fields: dict[str, Any] = {
        "total_faults": 30,
        "faulty_devices": 7,
        "top_codes": (("E001", 15), ("E002", 9)),
        "devices_total": 100,
    }
    fields.update(overrides)
    return FaultCodeAggregate(**fields)


# --- 1. 离线卡：数字确定性、时长是下界 -----------------------------------------


def test_the_offline_card_is_deterministic_and_names_its_own_card() -> None:
    first = build_offline_contract(_offline(), {"main_cause": "c", "recommendation": "r"}, **_window())
    second = build_offline_contract(_offline(), {"main_cause": "x", "recommendation": "y"}, **_window())

    assert first["card"] == "offline"
    assert first["rule_version"] == RULE_VERSION
    for key in ("incidents", "offline_devices", "open_incidents", "offline_minutes", "offline_ratio", "risk"):
        assert first[key] == second[key], key


def test_the_offline_ratio_uses_the_device_total_as_its_denominator() -> None:
    contract = build_offline_contract(_offline(), None, **_window())

    assert contract["offline_ratio"] == 0.09  # 9 of 100
    assert contract["devices_total"] == 100


def test_a_missing_denominator_is_not_a_zero_ratio() -> None:
    """``0.0`` 说「一台都没坏」；``None`` 说「不知道有多少台」。合并会让缺数据像好消息。"""
    contract = build_offline_contract(_offline(devices_total=None), None, **_window())

    assert contract["offline_ratio"] is None
    assert contract["risk"] == "unavailable"


def test_offline_risk_is_decided_by_open_incidents_not_by_history() -> None:
    """历史累计台数会随窗口变长单调增长；定级必须看**此刻仍在离线中**的那些。"""
    long_window_many_historic = _offline(incidents=500, offline_devices=400, open_incidents=1)
    contract = build_offline_contract(long_window_many_historic, None, **_window())

    assert contract["risk"] == "normal"

    now_mostly_down = _offline(incidents=5, offline_devices=5, open_incidents=40)
    assert build_offline_contract(now_mostly_down, None, **_window())["risk"] == "abnormal"


def test_the_offline_card_says_its_duration_is_a_lower_bound() -> None:
    """仍在离线中的记录只计数、不计时长——这一点必须写在契约里，不靠读代码才知道。"""
    contract = build_offline_contract(_offline(), None, **_window())

    assert contract["definition"]["duration"].startswith("recovered incidents only")
    assert contract["open_incidents"] == 4


def test_an_empty_window_is_unavailable_with_its_own_reason() -> None:
    contract = build_offline_contract(
        _offline(incidents=0, offline_devices=0), None, reason_code=REASON_NO_INCIDENTS, **_window()
    )

    assert contract["incidents"] is None
    assert contract["risk"] == "unavailable"
    assert contract["reason_code"] == REASON_NO_INCIDENTS
    assert contract["main_cause"] is None


def test_an_empty_scope_and_an_empty_window_are_different_reasons() -> None:
    """两张表、两种事实：读的人据此知道该去查什么。"""
    empty_scope = build_offline_contract(
        OfflineAggregate(0, 0, 0, 0), None, reason_code=REASON_SCOPE_EMPTY, **_window()
    )
    empty_window = build_offline_contract(
        OfflineAggregate(0, 0, 0, 0), None, reason_code=REASON_NO_INCIDENTS, **_window()
    )

    assert empty_scope["reason_code"] == REASON_SCOPE_EMPTY
    assert empty_window["reason_code"] == REASON_NO_INCIDENTS


# --- 2. 故障码卡 ---------------------------------------------------------------


def test_the_fault_code_card_ranks_and_is_deterministic() -> None:
    first = build_fault_code_contract(_faults(), {"main_cause": "c", "recommendation": "r"}, **_window())
    second = build_fault_code_contract(_faults(), {"main_cause": "c2", "recommendation": "r2"}, **_window())

    assert first["card"] == "fault_code"
    assert first["top_codes"] == [
        {"error_code": "E001", "fault_count": 15},
        {"error_code": "E002", "fault_count": 9},
    ]
    for key in ("total_faults", "faulty_devices", "faulty_ratio", "risk"):
        assert first[key] == second[key], key


def test_a_model_written_rank_is_discarded() -> None:
    """排行也是数字：模型塞进来的清单没有路径进入契约。"""
    contract = build_fault_code_contract(
        _faults(),
        {
            "main_cause": "c",
            "recommendation": "r",
            "top_codes": [{"error_code": "FAKE", "fault_count": 999}],
            "total_faults": 1,
        },
        **_window(),
    )

    assert contract["top_codes"] == [
        {"error_code": "E001", "fault_count": 15},
        {"error_code": "E002", "fault_count": 9},
    ]
    assert contract["total_faults"] == 30


def test_an_empty_fault_window_is_unavailable_with_its_own_reason() -> None:
    contract = build_fault_code_contract(
        FaultCodeAggregate(0, 0, (), devices_total=100), None, reason_code=REASON_NO_FAULTS, **_window()
    )

    assert contract["total_faults"] is None
    assert contract["top_codes"] is None
    assert contract["reason_code"] == REASON_NO_FAULTS
    assert contract["risk"] == "unavailable"


# --- 3. 语言守卫：两张卡各走同一条路径 -----------------------------------------


def test_a_chinese_narrative_on_the_offline_card_keeps_the_numbers() -> None:
    contract = build_offline_contract(
        _offline(),
        {"main_cause": "设备离线导致充电中断", "recommendation": "检查站点"},
        **_window(),
    )

    assert contract["reason_code"] == REASON_PROSE_LEAKED
    assert answer_chinese_leak(str(contract["main_cause"]), "en") == ""
    assert contract["offline_minutes"] == 480
    assert contract["risk"] == "attention"  # 4 of 100


def test_a_chinese_narrative_on_the_fault_card_keeps_the_numbers() -> None:
    contract = build_fault_code_contract(
        _faults(),
        {"main_cause": "固件版本不兼容", "recommendation": "升级固件"},
        **_window(),
    )

    assert contract["reason_code"] == REASON_PROSE_LEAKED
    assert answer_chinese_leak(str(contract["main_cause"]), "en") == ""
    assert contract["total_faults"] == 30
    assert contract["faulty_ratio"] == 0.07


# --- 4. SQL：窗口、交集、NULL 处理 --------------------------------------------


class _Cursor:
    def __init__(self, by_shape: dict[str, Any]) -> None:
        self.by_shape = by_shape
        self.executed: list[tuple[str, list[Any]]] = []
        self._current: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, list(params or [])))
        # Pick the answer by what the statement reads, not by call order: the
        # cards issue two statements and getting them swapped would be exactly
        # the silent-pass this suite exists to prevent.
        for marker, row in self.by_shape.items():
            if marker in sql:
                self._current = row
                return
        self._current = None

    def fetchone(self) -> Any:
        return self._current

    def fetchall(self) -> Any:
        return self._current if isinstance(self._current, list) else []


class _Source(MySQLSource):
    def __init__(self, scope: QueryScope, by_shape: dict[str, Any]) -> None:
        settings = Settings.from_env()
        settings.mysql.user = "readonly"
        settings.mysql.password = "secret"
        super().__init__(settings, scope=scope)
        self.cursor_obj = _Cursor(by_shape)

    def _cursor(self):  # type: ignore[override]
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield self.cursor_obj

        return _cm()


def _scope(site_ids: tuple[str, ...] | None) -> QueryScope:
    return QueryScope(tenant_id="T-1", site_ids=site_ids, user_id=None)


def test_an_empty_scope_short_circuits_before_any_card_query() -> None:
    for method, shape in (
        ("aggregate_offline_incidents", {"COUNT(*) AS incidents": {}}),
        ("aggregate_fault_codes", {"GROUP BY error_code": []}),
    ):
        source = _Source(_scope(()), shape)
        result = getattr(source, method)(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")
        assert result is None, method
        assert source.cursor_obj.executed == [], method


def test_an_out_of_scope_site_never_reaches_the_offline_sql() -> None:
    source = _Source(
        _scope(("S-1",)),
        {
            "COUNT(*) AS incidents": {
                "incidents": 1,
                "offline_devices": 1,
                "open_incidents": 0,
                "offline_minutes": 5,
            }
        },
    )

    source.aggregate_offline_incidents(("S-1", "S-OUT"), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    sql, params = source.cursor_obj.executed[0]
    assert "tenant_id=%s" in sql
    assert "S-OUT" not in params
    # The clamp parameter comes first (it is in the SELECT list, before WHERE).
    assert params[0] == "2026-10-02 00:00:00"
    assert params[1:] == ["T-1", "S-1", "2026-10-01 00:00:00", "2026-10-02 00:00:00"]


def test_the_offline_sql_clamps_duration_and_separates_open_incidents() -> None:
    source = _Source(
        _scope(("S-1",)),
        {
            "COUNT(*) AS incidents": {
                "incidents": 0,
                "offline_devices": 0,
                "open_incidents": 0,
                "offline_minutes": 0,
            }
        },
    )

    source.aggregate_offline_incidents(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    sql, _params = source.cursor_obj.executed[0]
    assert "recover_time IS NULL THEN 1 ELSE 0 END) AS open_incidents" in sql
    assert "LEAST(recover_time, %s)" in sql
    assert "GREATEST(TIMESTAMPDIFF(MINUTE" in sql


def test_the_fault_rank_is_bounded_and_requires_a_code() -> None:
    """排行要有界，且跳过没有故障码的记录——否则「排行」里会出现一行空字符串。"""
    source = _Source(
        _scope(("S-1",)),
        {
            "COUNT(*) AS total_faults": {"total_faults": 3, "faulty_devices": 2},
            "GROUP BY error_code": [{"error_code": "E001", "fault_count": 3}],
        },
    )

    result = source.aggregate_fault_codes(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    rank_sql, params = source.cursor_obj.executed[1]
    assert "error_code IS NOT NULL" in rank_sql
    assert "LIMIT 7" in rank_sql
    assert "fault_time >= %s" in rank_sql and "fault_time < %s" in rank_sql
    assert params == ["T-1", "S-1", "2026-10-01 00:00:00", "2026-10-02 00:00:00"]
    assert result == {"total_faults": 3, "faulty_devices": 2, "top_codes": (("E001", 3),)}


def test_the_device_total_uses_the_callers_scope_when_no_sites_are_named() -> None:
    source = _Source(_scope(("S-1", "S-2")), {"COUNT(DISTINCT id)": {"devices_total": 42}})

    total = source.count_devices(None)

    sql, params = source.cursor_obj.executed[0]
    assert "tenant_id=%s" in sql
    # `site_ids=None` still narrows to the caller's own sites: "no request-level
    # narrowing" is not "no site filter", and conflating them is what turns a
    # mis-scoped call into a tenant-wide count.
    assert "site_id IN (%s, %s)" in sql
    assert params == ["T-1", "S-1", "S-2"]
    assert total == 42


def test_the_device_total_is_none_when_the_narrowed_set_is_empty(tmp_path: Path) -> None:
    source = _Source(_scope(("S-1",)), {"COUNT(DISTINCT id)": {"devices_total": 42}})

    assert source.count_devices(("S-OUT",)) is None
    assert source.cursor_obj.executed == []
