"""运维分析面的确定性装配与聚合下推（#697，PRD #693 的 ③ 号路径）。

这里断言的只有**对外可观察的性质**：同一份输入两次给出逐字相同的数字、模型写的数字
进不了契约、空站点集合不发 SQL、窗外订单不计入、叙述漏中文时数字不被改写。

刻意不测实现细节（某函数调了几次）：那类断言在实现换形状时全绿，而这四条里每一条
都对应一种会静默交付错数字的退化。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.answer_language import answer_chinese_leak
from aiops_diagnostics.config import Settings
from aiops_diagnostics.ops_analysis import (
    ABNORMAL_STATUS,
    REASON_NO_ORDERS,
    REASON_PROSE_INVALID,
    REASON_PROSE_LEAKED,
    REASON_SCOPE_EMPTY,
    RULE_VERSION,
    AnalysisAggregate,
    build_failure_rate_contract,
    risk_level,
)
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import MySQLSource

WINDOW_START = datetime(2026, 10, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 2, tzinfo=UTC)


def _aggregate(*, total: int = 50, abnormal: int = 9, normal: int = 40) -> AnalysisAggregate:
    return AnalysisAggregate(
        total=total,
        abnormal_ended=abnormal,
        normal_ended=normal,
        top_sites=(("S-2", abnormal),) if abnormal else (),
    )


def _contract(aggregate: AnalysisAggregate, payload: Any, **overrides: Any) -> dict[str, Any]:
    """装配一份契约，默认填上本文件统一的站点/窗口（#697）。"""
    kwargs: dict[str, Any] = {
        "language": "en",
        "site_ids": ("S-1",),
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }
    kwargs.update(overrides)
    return build_failure_rate_contract(aggregate, payload, **kwargs)


def _narrative(main_cause: str = "Device connectivity dropped", recommendation: str = "Inspect S-2") -> Any:
    return {"main_cause": main_cause, "recommendation": recommendation}


# --- 1. 确定性：数字两次运行逐字相同，且与叙述无关 ------------------------------


def test_the_same_input_produces_the_same_every_number() -> None:
    first = _contract(_aggregate(), _narrative())
    second = _contract(_aggregate(), _narrative())

    for key in ("failure_rate", "risk", "counts", "secondary", "rule_version", "definition"):
        assert first[key] == second[key], key


def test_a_different_narrative_cannot_move_a_single_number() -> None:
    """换叙述只影响叙述字段——这是「模型不参与数字」的直接判据。"""
    baseline = _contract(_aggregate(), _narrative())
    other = build_failure_rate_contract(
        _aggregate(),
        _narrative(main_cause="A completely different explanation", recommendation="Do something else"),
        language="en",
        site_ids=("S-1",),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert other["main_cause"] != baseline["main_cause"]
    for key in ("failure_rate", "risk", "counts", "secondary"):
        assert other[key] == baseline[key], key


def test_a_number_written_by_the_model_is_discarded() -> None:
    """模型回包里塞数字（含伪造的失败率与等级）也进不了契约。"""
    contract = build_failure_rate_contract(
        _aggregate(),
        {
            "main_cause": "cause",
            "recommendation": "do it",
            # Neither key is read; both would change the answer if they were.
            "failure_rate": 0.99,
            "risk": "abnormal",
            "counts": {"total": 1, "abnormal_ended": 1, "normal_ended": 0},
        },
        language="en",
        site_ids=("S-1",),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert contract["failure_rate"] == 0.18
    assert contract["counts"] == {"total": 50, "abnormal_ended": 9, "normal_ended": 40}


def test_the_rounding_rule_is_fixed() -> None:
    contract = build_failure_rate_contract(
        _aggregate(total=3, abnormal=1, normal=2),
        _narrative(),
        language="en",
        site_ids=("S-1",),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert contract["failure_rate"] == 0.3333


def test_the_abnormal_definition_ships_with_the_number() -> None:
    """读的人必须能知道这个数字算的是什么——否则会读成「任何异常」。"""
    contract = _contract(_aggregate(), _narrative())

    assert contract["definition"]["abnormal"] == f"order_status={ABNORMAL_STATUS}"
    assert contract["rule_version"] == RULE_VERSION


# --- 2. 空范围 / 窗外没有订单：诚实空，不是全租户数字 --------------------------


@pytest.mark.parametrize("reason", [REASON_SCOPE_EMPTY, REASON_NO_ORDERS])
def test_an_empty_result_is_unavailable_and_never_a_zero_rate(reason: str) -> None:
    """「没人查过的范围」不是「没有异常」——两者在契约里必须分得开。"""
    contract = build_failure_rate_contract(
        AnalysisAggregate(0, 0, 0, ()),
        _narrative(main_cause="this must not be used"),
        language="en",
        site_ids=("S-1",),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        reason_code=reason,
    )

    assert contract["failure_rate"] is None
    assert contract["risk"] == "unavailable"
    assert contract["reason_code"] == reason
    assert contract["counts"] == {"total": None, "abnormal_ended": None, "normal_ended": None}
    # The model is not consulted at all: there is no cause to explain.
    assert contract["main_cause"] is None
    assert contract["recommendation"] is None


def test_risk_levels_come_from_one_place_and_are_total() -> None:
    assert risk_level(None, 0) == "unavailable"
    assert risk_level(0.0, 10) == "normal"
    assert risk_level(0.05, 10) == "attention"
    assert risk_level(0.5, 10) == "abnormal"


# --- 3. 输出语言守卫（第 5 个调用点） ------------------------------------------


def test_a_chinese_narrative_under_english_is_replaced_but_the_numbers_survive() -> None:
    """判定共享、处置按面不同：本面没有修复回路，因此换兜底文案。

    **数字不被改写**——把 ``failure_rate`` 换成「暂不可用」等于对外声明数据源当时
    不可用，那是一句关于数据的假陈述（ADR-0007 划下的那条线）。
    """
    contract = build_failure_rate_contract(
        _aggregate(),
        _narrative(main_cause="设备离线导致充电中断", recommendation="检查站点 S-2"),
        language="en",
        site_ids=("S-1",),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert contract["reason_code"] == REASON_PROSE_LEAKED
    assert answer_chinese_leak(str(contract["main_cause"]), "en") == ""
    assert contract["recommendation"] is None
    # The numbers are exactly what the aggregate said.
    assert contract["failure_rate"] == 0.18
    assert contract["counts"] == {"total": 50, "abnormal_ended": 9, "normal_ended": 40}
    assert contract["risk"] == "abnormal"


def test_a_chinese_narrative_under_chinese_is_kept() -> None:
    """守卫只在目标语言不是中文时判——中文请求拿到中文正文是**正确**的。"""
    contract = build_failure_rate_contract(
        _aggregate(),
        _narrative(main_cause="设备离线导致充电中断", recommendation="检查站点 S-2"),
        language="zh",
        site_ids=("S-1",),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert contract["reason_code"] is None
    assert contract["main_cause"] == "设备离线导致充电中断"


def test_a_non_text_narrative_is_refused_not_serialised() -> None:
    """形状漂移（provider 回了 dict）不得被当成正文交付。

    **数字不被撤**：#698 把三张卡的散文路径收成一条之后，散文出事与「什么都没算」
    分成两种处置——前者换散文，后者才撤数字。契约里已有的 ``failure_rate`` 是**算过的
    事实**，把它改写成「暂不可用」会是一句关于数据源的假陈述。
    """
    contract = build_failure_rate_contract(
        _aggregate(),
        {"main_cause": {"nested": "object"}, "recommendation": ["a", "list"]},
        language="en",
        site_ids=("S-1",),
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert contract["reason_code"] == REASON_PROSE_INVALID
    # The dict/list never becomes prose, and the computed numbers survive.
    assert isinstance(contract["main_cause"], str)
    assert "nested" not in str(contract["main_cause"])
    assert contract["failure_rate"] == 0.18
    assert contract["counts"] == {"total": 50, "abnormal_ended": 9, "normal_ended": 40}


# --- 4. 聚合 SQL：范围下推、站点集合求交、空集合短路 ---------------------------


class _RecordingCursor:
    """两段 SQL 各自的应答：计数查询与「最集中的站点」查询形状不同。

    按 SQL 里有没有 ``site_id`` 区分应答，而不是按调用次序——后者会把两段语句的
    应答错位，而那正好是这里最不该出现的假通过。
    """

    def __init__(self, summary: dict[str, Any], top: dict[str, Any] | None) -> None:
        self.summary = summary
        self.top = top
        self.executed: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, list(params or [])))
        self._current = self.top if "site_id," in sql else self.summary

    def fetchone(self) -> dict[str, Any] | None:
        return self._current

    def fetchall(self) -> list[dict[str, Any]]:
        return [self._current] if self._current else []


class _FakeSource(MySQLSource):
    """真实的 ``aggregate_order_statuses``，只有连接被换掉。

    范围谓词的渲染、站点集合的拼装、短路分支全部走生产代码——那正是最容易写错的一处
    （作业的站点集合若被传进 ``_scope_where``，租户谓词会被**替换**掉）。
    """

    def __init__(self, scope: QueryScope, summary: dict[str, Any], top: dict[str, Any] | None = None) -> None:
        settings = Settings.from_env()
        settings.mysql.user = "readonly"
        settings.mysql.password = "secret"
        super().__init__(settings, scope=scope)
        self.cursor_obj = _RecordingCursor(summary, top)

    def _cursor(self):  # type: ignore[override]
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield self.cursor_obj

        return _cm()


def _scope(site_ids: tuple[str, ...] | None) -> QueryScope:
    return QueryScope(tenant_id="T-1", site_ids=site_ids, user_id=None)


def test_an_empty_site_scope_issues_no_sql_at_all() -> None:
    """空站点集合是「查了也没有」而不是「查全库」——这条判据只看有没有发语句。"""
    source = _FakeSource(_scope(()), {"total": 0, "abnormal_ended": 0, "normal_ended": 0})

    result = source.aggregate_order_statuses(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    assert result is None
    assert source.cursor_obj.executed == []


def test_an_explicitly_empty_site_set_issues_no_sql_at_all() -> None:
    source = _FakeSource(_scope(("S-1",)), {"total": 0, "abnormal_ended": 0, "normal_ended": 0})

    result = source.aggregate_order_statuses((), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    assert result is None
    assert source.cursor_obj.executed == []


def test_the_job_site_set_narrows_the_scope_instead_of_replacing_it() -> None:
    """租户谓词永远下推；请求的站点集合先与可见站点取交集，再成为筛选条件。

    「请求里有一个看不见的站点」时不能把整条集合原样下推——那样统计出来的数字会
    包含调用者无权看到的站点，而查询成功、数字"合理"。所以这条断言的是
    **被剔除的那个站点没有出现在参数里**。
    """
    source = _FakeSource(
        _scope(("S-1", "S-2", "S-3")),
        {"total": 10, "abnormal_ended": 2, "normal_ended": 8},
        {"site_id": "S-2", "abnormal_ended": 2},
    )

    # S-OUT is not in the scope: it must be dropped before the SQL is built.
    result = source.aggregate_order_statuses(("S-1", "S-OUT"), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    sql, params = source.cursor_obj.executed[0]
    assert "tenant_id=%s" in sql
    assert "site_id IN (%s)" in sql
    assert "created_time >= %s" in sql and "created_time < %s" in sql
    assert params == ["T-1", "S-1", "2026-10-01 00:00:00", "2026-10-02 00:00:00"]
    assert result is not None and result["top_sites"] == (("S-2", 2),)


def test_the_window_is_half_open_in_the_sql() -> None:
    """半开区间写在 SQL 里，而不是靠读代码的人推断。"""
    source = _FakeSource(
        _scope(("S-1",)),
        {"total": 0, "abnormal_ended": 0, "normal_ended": 0},
    )

    source.aggregate_order_statuses(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    for sql, _params in source.cursor_obj.executed:
        assert "created_time >= %s AND created_time < %s" in sql


def test_the_aggregate_reads_status_nulls_as_zero() -> None:
    """``SUM(CASE ...)`` 在零行时返回 NULL；NULL 与「说不清」是两回事。"""
    source = _FakeSource(_scope(("S-1",)), {"total": 0, "abnormal_ended": None, "normal_ended": None})

    result = source.aggregate_order_statuses(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    assert result == {"total": 0, "abnormal_ended": 0, "normal_ended": 0, "top_sites": ()}


# --- 5. 作业行：种类判别、窗口校验 --------------------------------------------


def test_the_analysis_window_must_be_a_real_bounded_interval(tmp_path: Path) -> None:
    from aiops_diagnostics.gateway_store import GatewayStore

    store = GatewayStore(tmp_path / "gateway.db")
    base = dict(site_ids=("S-1",), rule_version=RULE_VERSION)

    with pytest.raises(ValueError):
        store.create_analysis_job(
            "scope-1", window_start="2026-10-02T00:00:00Z", window_end="2026-10-01T00:00:00Z", **base
        )
    with pytest.raises(ValueError):
        store.create_analysis_job(
            "scope-1", window_start="not-a-timestamp", window_end="2026-10-02T00:00:00Z", **base
        )
    with pytest.raises(ValueError):
        store.create_analysis_job(
            "scope-1",
            window_start="2026-10-01T00:00:00Z",
            window_end="2026-10-02T00:00:00Z",
            site_ids=(),
            rule_version=RULE_VERSION,
        )


def test_an_analysis_row_is_not_an_order_diagnosis(tmp_path: Path) -> None:
    """同一张表上的两种作业：列表接口只列诊断，id 前缀各自认得。"""
    from aiops_diagnostics.gateway_store import GatewayStore

    store = GatewayStore(tmp_path / "gateway.db")
    diagnosis = store.create_standard_diagnosis("scope-1", "O-1", "why", None)
    analysis = store.create_analysis_job(
        "scope-1",
        site_ids=("S-1",),
        window_start="2026-10-01T00:00:00Z",
        window_end="2026-10-02T00:00:00Z",
        rule_version=RULE_VERSION,
    )

    listed = [row["diagnosis_id"] for row in store.list_standard_diagnoses("scope-1")]

    assert listed == [diagnosis["diagnosis_id"]]
    assert analysis["diagnosis_id"].startswith("an_")
    assert analysis["kind"] == "analysis"
    assert json.loads(analysis["site_ids_json"]) == ["S-1"]
    # Both id kinds are fetchable; a qa_ id still is not.
    assert store.get_standard_diagnosis(diagnosis["diagnosis_id"], "scope-1") is not None
    assert store.get_standard_diagnosis(analysis["diagnosis_id"], "scope-1") is not None
    assert store.get_standard_diagnosis("qa_" + "0" * 32, "scope-1") is None
