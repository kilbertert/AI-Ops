"""支付卡（#699，PRD #693 的 ③ 号路径）——本期唯一真缺口。

这张卡的验收项与另外三张**不同**：它不是「把数字算对」，而是「**算不出来时说清楚**」。
公司的订单表上只有结果态（``is_pay`` 是布尔、``refund_status`` 是 0/1/2），一次被支付
渠道拒绝、随后重试成功的支付在这两列上不留痕迹。因此交付的是四个可复核的桶，
``failure_rate`` 位明确标 ``unavailable`` 并把原因指回数据源。

**判据的形状**：断言的是「这个平台上没有这个事实」被如实说出来，而不是「有没有一个
看起来合理的失败率」——后者在没有该字段时唯一可能的来源是模型编造。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiops_diagnostics.answer_language import answer_chinese_leak
from aiops_diagnostics.config import Settings
from aiops_diagnostics.data.sources import MySQLSource
from aiops_diagnostics.identity.query_scope import QueryScope
from aiops_diagnostics.operator.ops_analysis import (
    REASON_NO_ORDERS,
    REASON_NO_PAYMENT_FAILURE_SOURCE,
    REASON_PROSE_LEAKED,
    RULE_VERSION,
    PaymentAggregate,
    build_payment_contract,
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


def _payment(**overrides: Any) -> PaymentAggregate:
    fields: dict[str, Any] = {
        "total": 100,
        "paid": 80,
        "paid_refunded": 20,
        "paid_net": 60,
        "unpaid": 20,
    }
    fields.update(overrides)
    return PaymentAggregate(**fields)


# --- 1. 本票的核心判据：算不出来就说清楚，而不是编一个 ---------------------------


def test_the_card_refuses_to_invent_a_failure_rate() -> None:
    """``failure_rate`` 恒为 ``None``，原因码指回数据源。

    这是本票与另外三张卡的**根本区别**：它们算不出来时是「范围为空」，本卡算不出来
    是因为**平台没有这个事实**。两者都不是 0，也都不是让模型补一个。
    """
    contract = build_payment_contract(_payment(), None, **_window())

    assert contract["failure_rate"] is None
    assert contract["risk"] == "unavailable"
    assert contract["reason_code"] == REASON_NO_PAYMENT_FAILURE_SOURCE


def test_a_model_written_failure_rate_has_no_way_into_the_contract() -> None:
    """模型在这张卡上写一个失败率也进不来——连可写的位置都没有。"""
    contract = build_payment_contract(
        _payment(),
        {"main_cause": "c", "recommendation": "r", "failure_rate": 0.31, "risk": "abnormal"},
        **_window(),
    )

    assert contract["failure_rate"] is None
    assert contract["risk"] == "unavailable"


def test_the_gap_is_named_in_the_contract_not_left_blank() -> None:
    """ "留空"与"明确说缺口在哪"是两回事：前者会被读成这张卡没算。"""
    contract = build_payment_contract(_payment(), None, **_window())

    assert contract["definition"]["buckets"] == "paid = paid_refunded + paid_net; paid + unpaid = total"
    assert "is_pay IS NOT TRUE" in contract["definition"]["unpaid"]
    assert contract["rule_version"] == RULE_VERSION


def test_the_buckets_still_add_up_when_the_rate_cannot_be_computed() -> None:
    """缺口不阻断交付：四个桶是能对上账的，因此照常给出。"""
    contract = build_payment_contract(_payment(), None, **_window())

    assert contract["paid"] == contract["paid_refunded"] + contract["paid_net"]
    assert contract["paid"] + contract["unpaid"] == contract["total"]


def test_an_empty_window_keeps_its_own_reason_code() -> None:
    """「窗内没有订单」与「平台没有这个事实」是两个不同的原因，不能被后者盖掉。"""
    contract = build_payment_contract(
        PaymentAggregate(0, 0, 0, 0, 0), None, reason_code=REASON_NO_ORDERS, **_window()
    )

    assert contract["reason_code"] == REASON_NO_ORDERS
    assert contract["total"] is None


# --- 2. 退款占比：分母是**已支付**，不是总数 -----------------------------------


def test_the_refund_share_uses_paid_as_its_denominator() -> None:
    """用错分母会把「这个区间订单少」读成「退款少」。"""
    contract = build_payment_contract(_payment(), None, **_window())

    assert contract["refund_share"] == 0.25  # 20 of 80 paid, not of 100 total
    assert "denominator is PAID" in contract["definition"]["refund_share"]


def test_no_paid_orders_means_no_share_not_a_zero_share() -> None:
    contract = build_payment_contract(_payment(paid=0, paid_refunded=0, paid_net=0), None, **_window())

    assert contract["refund_share"] is None


def test_the_card_is_deterministic() -> None:
    first = build_payment_contract(_payment(), {"main_cause": "c", "recommendation": "r"}, **_window())
    second = build_payment_contract(_payment(), {"main_cause": "x", "recommendation": "y"}, **_window())

    assert first["card"] == "payment"
    for key in ("total", "paid", "paid_refunded", "paid_net", "unpaid", "refund_share", "risk"):
        assert first[key] == second[key], key


# --- 3. 语言守卫：本卡仍走同一条散文路径 ---------------------------------------


def test_a_chinese_narrative_keeps_the_buckets() -> None:
    contract = build_payment_contract(
        _payment(),
        {"main_cause": "支付失败较多", "recommendation": "检查支付通道"},
        **_window(),
    )

    assert contract["reason_code"] == REASON_PROSE_LEAKED
    assert answer_chinese_leak(str(contract["main_cause"]), "en") == ""
    assert contract["paid"] == 80
    assert contract["unpaid"] == 20


# --- 4. SQL：三态、交集、窗口 --------------------------------------------------


class _Cursor:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.executed: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, list(params or [])))

    def fetchone(self) -> dict[str, Any]:
        return self.row


class _Source(MySQLSource):
    def __init__(self, scope: QueryScope, row: dict[str, Any]) -> None:
        settings = Settings.from_env()
        settings.mysql.user = "readonly"
        settings.mysql.password = "secret"
        super().__init__(settings, scope=scope)
        self.cursor_obj = _Cursor(row)

    def _cursor(self):  # type: ignore[override]
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield self.cursor_obj

        return _cm()


def _scope(site_ids: tuple[str, ...] | None) -> QueryScope:
    return QueryScope(tenant_id="T-1", site_ids=site_ids, user_id=None)


def test_the_payment_sql_treats_a_null_is_pay_as_unpaid() -> None:
    """``is_pay`` 可空：三态写死，且两个桶对得上总数。"""
    zeros = {"total": 0, "paid": 0, "paid_refunded": 0, "paid_net": 0, "unpaid": 0}
    source = _Source(_scope(("S-1",)), zeros)

    source.aggregate_payment_split(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    sql, _params = source.cursor_obj.executed[0]
    assert "is_pay IS TRUE THEN 1" in sql
    assert "is_pay IS NOT TRUE THEN 1" in sql
    assert "refund_status <> 0" in sql


def test_the_payment_sql_is_scoped_and_windowed() -> None:
    zeros = {"total": 0, "paid": 0, "paid_refunded": 0, "paid_net": 0, "unpaid": 0}
    source = _Source(_scope(("S-1",)), zeros)

    source.aggregate_payment_split(("S-1", "S-OUT"), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    sql, params = source.cursor_obj.executed[0]
    assert "tenant_id=%s" in sql
    assert "site_id IN (%s)" in sql
    assert "created_time >= %s" in sql and "created_time < %s" in sql
    assert params == ["T-1", "S-1", "2026-10-01 00:00:00", "2026-10-02 00:00:00"]


def test_an_empty_scope_short_circuits() -> None:
    zeros = {"total": 0, "paid": 0, "paid_refunded": 0, "paid_net": 0, "unpaid": 0}
    source = _Source(_scope(()), zeros)

    assert source.aggregate_payment_split(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00") is None
    assert source.cursor_obj.executed == []


def test_the_sql_nulls_read_as_zero() -> None:
    source = _Source(
        _scope(("S-1",)),
        {"total": 0, "paid": None, "paid_refunded": None, "paid_net": None, "unpaid": None},
    )

    result = source.aggregate_payment_split(("S-1",), "2026-10-01 00:00:00", "2026-10-02 00:00:00")

    assert result == {"total": 0, "paid": 0, "paid_refunded": 0, "paid_net": 0, "unpaid": 0}
