"""管家端商城结算金额取证、6 大规则核验与 Gateway 代理集成测试。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from execution.test_m2_close_contract import NODE_A_TOKEN

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.data.sources import MySQLSource
from aiops_diagnostics.identity.query_scope import QueryScope
from aiops_diagnostics.operator import operator_cpms, operator_evidence, order_settlement
from operations.test_operator_agent import call_tool, finish
from operations.test_operator_cpms import CONTRACT, HEADERS, ORDER, environment, request, terminal


class _FakeSettlementConn:
    """记录执行 SQL 并按表名返回商城结算链路行的测试连接。"""

    def __init__(
        self,
        *,
        charging: list[dict[str, Any]] | None = None,
        sites: list[dict[str, Any]] | None = None,
        mall_orders: list[dict[str, Any]] | None = None,
        mall_items: list[dict[str, Any]] | None = None,
        shops: list[dict[str, Any]] | None = None,
        shop_levels: list[dict[str, Any]] | None = None,
    ) -> None:
        self.charging = charging if charging is not None else []
        self.sites = sites if sites is not None else []
        self.mall_orders = mall_orders if mall_orders is not None else []
        self.mall_items = mall_items if mall_items is not None else []
        self.shops = shops if shops is not None else []
        self.shop_levels = shop_levels if shop_levels is not None else []
        self.executed: list[tuple[str, list[Any]]] = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, list(params or [])))

    def fetchall(self) -> list[dict[str, Any]]:
        sql = self.executed[-1][0]
        if "`ch_order_info`" in sql:
            return list(self.charging)
        if "`ch_site`" in sql:
            return list(self.sites)
        if "`order_info`" in sql:
            return list(self.mall_orders)
        if "`order_item`" in sql:
            return list(self.mall_items)
        if "`shop_info`" in sql:
            return list(self.shops)
        if "`shop_level`" in sql:
            return list(self.shop_levels)
        return []

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _mysql_source(
    monkeypatch: pytest.MonkeyPatch,
    conn: _FakeSettlementConn,
    *,
    scope: QueryScope | None,
    mall_database: str = "qumall_mall",
) -> MySQLSource:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.mysql.mall_database = mall_database
    monkeypatch.setattr("aiops_diagnostics.data.sources.pymysql.connect", lambda **kwargs: conn)
    return MySQLSource(settings, scope=scope)


def _sample_records(
    *,
    order_no: str = ORDER,
    site_id: str = "SITE-1",
    tenant_id: str = "TENANT-A",
    site_settlement_type: int = 0,
    order_settlement_type: int = 0,
    is_pay: str = "1",
    electric_amount: str = "20.00",
    service_amount: str = "10.00",
    service_discount: str = "2.00",
    start_amount: str = "2.00",
    tax_amount: str = "3.00",
    rate_of_payment: str = "0.006",
    insert_gun_fee: str = "10",
    electricity_fee_rate: str = "0",
    service_fee_rate: str = "5",
    order_rate: str = "0",
    per_kwh_fee: str = "0",
    electric_lose: str = "0.00",
    refund_amount: str = "0.00",
    elec_commission: str = "0",
    start_commission: str = "0",
    tax_commission: str = "0",
    override_start_srv: str | None = None,
    override_settlement_amount: str | None = None,
    override_commission_base: str | None = None,
    override_order_service: str | None = None,
    override_fee_channel: str | None = None,
    override_payment_price: str | None = None,
    extra_items: list[dict[str, Any]] | None = None,
    missing_shop_level: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造一组数学自洽的充电详情与商城结算记录（并支持定点注入偏差）。"""
    d = Decimal

    def r2(x: Decimal) -> Decimal:
        return x.quantize(d("0.01"), rounding=ROUND_HALF_UP)

    elec = d(electric_amount)
    srv_gross = d(service_amount)
    disc = d(service_discount)
    srv_pay = max(d("0"), srv_gross - disc)
    start = d(start_amount)
    tax = d(tax_amount)
    rop = d(rate_of_payment)
    ins_rate = d(insert_gun_fee)
    elec_rate = d(electricity_fee_rate)
    srv_rate = d(service_fee_rate)
    ord_rate = d(order_rate)
    lose = d(electric_lose)
    refund = d(refund_amount)

    elec_rop = r2(elec * rop)
    srv_rop = r2(srv_pay * rop)
    start_rop = r2(start * rop)
    tax_rop = r2(tax * rop)

    raw_start_srv = r2(start * ins_rate / d("100"))
    overflow = start - (raw_start_srv + start_rop)
    calc_start_srv = max(d("0"), r2(raw_start_srv + overflow)) if overflow < 0 else raw_start_srv
    start_srv = d(override_start_srv) if override_start_srv is not None else calc_start_srv

    if ord_rate == 0:
        elec_srv = r2(elec * elec_rate / d("100"))
        srv_srv = r2(srv_pay * srv_rate / d("100"))
        calc_order_srv = start_srv + elec_srv + srv_srv
    else:
        elec_srv = r2(elec * ord_rate / d("100"))
        srv_srv = r2(srv_pay * ord_rate / d("100"))
        calc_order_srv = start_srv + r2((elec + srv_pay + start + tax) * ord_rate / d("100"))
    order_srv = d(override_order_service) if override_order_service is not None else calc_order_srv

    fee_channel = (
        d(override_fee_channel)
        if override_fee_channel is not None
        else elec_rop + srv_rop + start_rop + tax_rop
    )

    if elec_commission == "1" and start_commission == "1" and tax_commission == "1":
        mode = 1
    elif elec_commission == "1":
        mode = 2
    else:
        mode = 0

    if mode == 1:
        calc_settle = d("0.00")
        calc_comm = (
            elec + srv_gross + start + tax - elec_rop - srv_rop - start_rop - tax_rop - disc - order_srv
        )
    elif mode == 2:
        calc_settle = max(d("0.00"), start + tax - start_rop - tax_rop - start_srv)
        if ord_rate == 0:
            calc_comm = elec + srv_gross - elec_rop - srv_rop - disc - elec_srv - srv_srv
        else:
            calc_comm = elec + srv_gross - elec_rop - srv_rop - disc - order_srv + calc_start_srv
    else:
        calc_settle = elec + start + tax - elec_rop - start_rop - tax_rop - start_srv
        if elec_rate != 0:
            active_rate = elec_rate if ord_rate == 0 else ord_rate
            calc_settle -= r2(elec * active_rate / d("100"))
        calc_settle = max(d("0.00"), calc_settle)
        if ord_rate == 0:
            calc_comm = srv_gross - srv_rop - disc - srv_srv
        else:
            calc_comm = srv_gross - srv_rop - disc - order_srv + calc_start_srv
    if lose > 0:
        calc_comm -= lose
    calc_comm = max(d("0.00"), calc_comm)

    settle_amt = d(override_settlement_amount) if override_settlement_amount is not None else calc_settle
    comm_amt = d(override_commission_base) if override_commission_base is not None else calc_comm

    total_before_tax = elec + srv_gross + start
    pay_price = (
        d(override_payment_price)
        if override_payment_price is not None
        else total_before_tax - disc + tax - refund
    )

    detail = {
        "orderNo": order_no,
        "siteId": site_id,
        "electricity": "15.00",
        "electricityFee": str(elec),
        "serviceFee": str(srv_gross),
        "dsServiceFee": "0.00",
        "launchFee": str(start),
        "marketAmount": str(disc),
        "vatFee": str(tax),
        "totalAmount": str(total_before_tax),
        "payAmount": str(total_before_tax - disc + tax),
        "refundAmount": str(refund),
    }

    items = [
        {
            "id": "item-1",
            "order_id": "MALL-1",
            "sku_code": "1",
            "spu_name": "充电桩服务费",
            "quantity": 1,
            "status": "0",
            "commission_flag": "1",
            "tax_rate": None,
            "sales_price": str(srv_gross),
            "payment_price": str(srv_pay),
            "refund_amount": "0.00",
            "payment_coupon_price": "0.00",
            "platform_discount_amount": str(disc),
            "platform_balance_reduce": "0.00",
            "shop_balance_reduce": "0.00",
            "market_price": "0.00",
            "service_fee": str(srv_srv),
            "rate_of_payment": str(srv_rop),
            "commission_base_amount": str(comm_amt),
            "settlement_amount": "0.00",
            "goods_cost_price": "0.00",
        },
        {
            "id": "item-2",
            "order_id": "MALL-1",
            "sku_code": "2",
            "spu_name": "充电桩电费",
            "quantity": 1,
            "status": "0",
            "commission_flag": elec_commission,
            "tax_rate": None,
            "sales_price": str(elec),
            "payment_price": str(elec),
            "refund_amount": "0.00",
            "payment_coupon_price": "0.00",
            "platform_discount_amount": "0.00",
            "platform_balance_reduce": "0.00",
            "shop_balance_reduce": "0.00",
            "market_price": "0.00",
            "service_fee": str(elec_srv),
            "rate_of_payment": str(elec_rop),
            "commission_base_amount": "0.00",
            "settlement_amount": str(settle_amt),
            "goods_cost_price": str(elec),
        },
    ]
    if start > 0:
        items.append(
            {
                "id": "item-4",
                "order_id": "MALL-1",
                "sku_code": "4",
                "spu_name": "充电桩插枪费",
                "quantity": 1,
                "status": "0",
                "commission_flag": start_commission,
                "tax_rate": None,
                "sales_price": str(start),
                "payment_price": str(start),
                "refund_amount": "0.00",
                "payment_coupon_price": "0.00",
                "platform_discount_amount": "0.00",
                "platform_balance_reduce": "0.00",
                "shop_balance_reduce": "0.00",
                "market_price": "0.00",
                "service_fee": str(start_srv),
                "rate_of_payment": str(start_rop),
                "commission_base_amount": "0.00",
                "settlement_amount": "0.00",
                "goods_cost_price": "0.00",
            }
        )
    if tax > 0:
        items.append(
            {
                "id": "item-9",
                "order_id": "MALL-1",
                "sku_code": "9",
                "spu_name": "充电桩增值税",
                "quantity": 1,
                "status": "0",
                "commission_flag": tax_commission,
                "tax_rate": "10",
                "sales_price": str(tax),
                "payment_price": str(tax),
                "refund_amount": "0.00",
                "payment_coupon_price": "0.00",
                "platform_discount_amount": "0.00",
                "platform_balance_reduce": "0.00",
                "shop_balance_reduce": "0.00",
                "market_price": "0.00",
                "service_fee": "0.00",
                "rate_of_payment": str(tax_rop),
                "commission_base_amount": "0.00",
                "settlement_amount": "0.00",
                "goods_cost_price": "0.00",
            }
        )
    if extra_items:
        items.extend(extra_items)

    records = {
        "charging": {
            "id": "CH-1",
            "order_no": order_no,
            "tenant_id": tenant_id,
            "site_id": site_id,
            "status": 1,
            "electricity": "15.00",
            "pay_amount": str(total_before_tax - disc + tax),
            "is_pay": 1,
            "settlement_type": order_settlement_type,
        },
        "charging_rows": 1,
        "site": {"id": site_id, "tenant_id": tenant_id, "settlement_type": site_settlement_type},
        "mall_orders": [
            {
                "id": "MALL-1",
                "tenant_id": tenant_id,
                "shop_id": site_id,
                "order_no": order_no,
                "order_type": "11",
                "order_tag": 18,
                "is_pay": is_pay,
                "status": "3",
                "settlement_status": "1",
                "payment_type": "1",
                "total_amount": str(total_before_tax),
                "sales_price": str(total_before_tax),
                "payment_price": str(pay_price),
                "paid_amount": str(pay_price),
                "refund_amount": str(refund),
                "platform_discount_amount": str(disc),
                "platform_balance_reduce": "0.00",
                "payment_coupon_price": "0.00",
                "market_price": "0.00",
                "shop_balance_reduce": "0.00",
                "sliver_deduct_amount": str(lose),
                "service_fee": str(order_srv),
                "rate_of_payment": rate_of_payment,
                "fee_of_pay_channel": str(fee_channel),
                "commission_base_amount": str(comm_amt),
                "actual_commission": "0.00",
                "settlement_amount": str(settle_amt),
            }
        ],
        "mall_order_rows": 1,
        "mall_items": items,
        "shop": None
        if missing_shop_level
        else {"id": site_id, "tenant_id": tenant_id, "level_id": "LVL-1", "configs": "{}"},
        "shop_level": None
        if missing_shop_level
        else {
            "id": "LVL-1",
            "tenant_id": tenant_id,
            "level_name": "标准等级",
            "settle_fee": service_fee_rate,
            "rate_of_settle_fee_for_elect": order_rate,
            "settle_fee_of_elect": per_kwh_fee,
            "config": json.dumps(
                {
                    "insertGunFee": insert_gun_fee,
                    "electricityFee": electricity_fee_rate,
                    "serviceFee": service_fee_rate,
                    "parkFee": "0",
                    "anXinFee": "0",
                }
            ),
        },
        "truncated": [],
    }
    return detail, records


def _by_label(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["label"]: item for item in result["checks"]}


# --- 1. MySQLSource.operator_settlement_records 范围隔离与快照测试 ---


def test_mysql_operator_settlement_records_enforces_scope_and_consistent_snapshot(monkeypatch):
    _, records = _sample_records()
    conn = _FakeSettlementConn(
        charging=[records["charging"]],
        sites=[records["site"]],
        mall_orders=records["mall_orders"],
        mall_items=records["mall_items"],
        shops=[records["shop"]],
        shop_levels=[records["shop_level"]],
    )
    src = _mysql_source(
        monkeypatch,
        conn,
        scope=QueryScope(tenant_id="TENANT-A", site_ids=("SITE-1", "SITE-2"), user_id=None),
    )
    got = src.operator_settlement_records(ORDER, expected_site_id="SITE-1")
    assert got is not None and got["charging"]["order_no"] == ORDER
    assert len(got["mall_orders"]) == 1 and len(got["mall_items"]) == 4
    assert got["shop_level"]["id"] == "LVL-1"
    sqls = [sql for sql, _ in conn.executed]
    assert any("REPEATABLE READ" in s for s in sqls)
    assert any("WITH CONSISTENT SNAPSHOT, READ ONLY" in s for s in sqls)
    ch_sql, ch_params = next((s, p) for s, p in conn.executed if "`ch_order_info`" in s)
    assert "site_id IN (%s, %s)" in ch_sql and "AND site_id=%s" in ch_sql
    assert ch_params == [ORDER, "TENANT-A", "SITE-1", "SITE-2", "SITE-1"]
    mall_sql, mall_params = next((s, p) for s, p in conn.executed if "`order_info`" in s)
    assert "order_type='11'" in mall_sql and "shop_id=%s" in mall_sql
    assert "total_balance_reduce" not in mall_sql
    assert mall_params == ["TENANT-A", "SITE-1", ORDER]
    item_sql, _ = next((s, p) for s, p in conn.executed if "`order_item`" in s)
    assert "commission_flag" in item_sql and "goods_cost_price" in item_sql
    assert "is_commission" not in item_sql and "cost_amount" not in item_sql
    level_sql, _ = next((s, p) for s, p in conn.executed if "`shop_level`" in s)
    assert "deleted_at IS NULL" in level_sql and "del_flag" not in level_sql


@pytest.mark.parametrize(
    "scope",
    [
        None,
        QueryScope(tenant_id="TENANT-A", site_ids=(), user_id=None),
        QueryScope(tenant_id="TENANT-A", site_ids=("SITE-1",), user_id="C-USER-1"),
    ],
)
def test_mysql_operator_settlement_records_refuses_invalid_or_c_end_scope(monkeypatch, scope):
    conn = _FakeSettlementConn()
    src = _mysql_source(monkeypatch, conn, scope=scope)
    assert src.operator_settlement_records(ORDER, expected_site_id="SITE-1") is None
    assert conn.executed == []


def test_mysql_operator_settlement_records_disabled_and_out_of_scope_short_circuit(monkeypatch):
    conn = _FakeSettlementConn(charging=[])
    disabled_src = _mysql_source(
        monkeypatch,
        conn,
        scope=QueryScope(tenant_id="TENANT-A", site_ids=("SITE-1",), user_id=None),
        mall_database="",
    )
    assert disabled_src.operator_settlement_records(ORDER) == {"disabled": True}
    assert conn.executed == []

    scoped_src = _mysql_source(
        monkeypatch,
        conn,
        scope=QueryScope(tenant_id="TENANT-A", site_ids=("SITE-1",), user_id=None),
    )
    miss = scoped_src.operator_settlement_records(ORDER, expected_site_id="SITE-1")
    assert miss is not None and miss["charging"] is None and miss["charging_rows"] == 0
    assert not any("`qumall_mall`" in sql for sql, _ in conn.executed)


def test_mysql_operator_settlement_records_marks_truncated_items(monkeypatch):
    _, records = _sample_records()
    many_items = [{**records["mall_items"][0], "id": f"item-{idx}"} for idx in range(31)]
    conn = _FakeSettlementConn(
        charging=[records["charging"]],
        sites=[records["site"]],
        mall_orders=records["mall_orders"],
        mall_items=many_items,
        shops=[records["shop"]],
        shop_levels=[records["shop_level"]],
    )
    src = _mysql_source(
        monkeypatch, conn, scope=QueryScope(tenant_id="TENANT-A", site_ids=None, user_id=None)
    )
    got = src.operator_settlement_records(ORDER)
    assert got is not None and got["truncated"] == ["mall_items"] and len(got["mall_items"]) == 30


# --- 2. 6 大清分规则全分支核验 ---


@pytest.mark.parametrize(
    "case_name,kwargs,expected_mode",
    [
        (
            "mode0_rate0_no_elec_cut",
            dict(order_settlement_type=0, elec_commission="0", electricity_fee_rate="0", order_rate="0"),
            0,
        ),
        (
            "mode0_rate0_with_elec_cut",
            dict(order_settlement_type=0, elec_commission="0", electricity_fee_rate="3", order_rate="0"),
            0,
        ),
        (
            "mode0_order_rate_nonzero",
            dict(order_settlement_type=0, elec_commission="0", electricity_fee_rate="0", order_rate="4"),
            0,
        ),
        (
            "mode1_whole_order_follows_rate0",
            dict(
                order_settlement_type=1,
                site_settlement_type=1,
                elec_commission="1",
                start_commission="1",
                tax_commission="1",
                order_rate="0",
            ),
            1,
        ),
        (
            "mode1_whole_order_follows_rate_nonzero",
            dict(
                order_settlement_type=1,
                site_settlement_type=1,
                elec_commission="1",
                start_commission="1",
                tax_commission="1",
                order_rate="4",
            ),
            1,
        ),
        (
            "mode2_elec_follows_rate0",
            dict(
                order_settlement_type=1,
                site_settlement_type=1,
                elec_commission="1",
                start_commission="0",
                tax_commission="0",
                electricity_fee_rate="3",
                order_rate="0",
            ),
            2,
        ),
        (
            "mode2_elec_follows_rate_nonzero",
            dict(
                order_settlement_type=1,
                site_settlement_type=1,
                elec_commission="1",
                start_commission="0",
                tax_commission="0",
                order_rate="4",
            ),
            2,
        ),
        (
            "mode0_with_electric_lose_and_floor_zero",
            dict(
                order_settlement_type=0,
                elec_commission="0",
                service_amount="2.00",
                service_discount="1.50",
                electric_lose="1.00",
            ),
            0,
        ),
    ],
)
def test_verify_all_six_rules_pass_across_modes_and_rates(case_name, kwargs, expected_mode):
    detail, records = _sample_records(**kwargs)
    projected = order_settlement.project_settlement(records)
    assert projected["settlement_mode"]["effective_mode"] == expected_mode
    # 服务费单项抽成应取 sku_code='1' 自身，而非总平台服务费
    assert projected["settlement"]["serviceItemServiceFee"] == records["mall_items"][0]["service_fee"]

    result = order_settlement.verify(detail, projected)
    assert all(c["status"] == "通过" for c in result["checks"]), f"{case_name}: {result['checks']}"
    assert "算术对照一致" in result["conclusion"]
    assert any("shop_level" in item for item in result["limitations"])
    assert any("实际银行打款到账" in item for item in result["limitations"])


def test_rule2_start_fee_overflow_deduction_and_backend_unit_bug_detection():
    # 场景 A：后端已正确扣减通道费溢出（1.00 - (0.50 + 0.70) = -0.20 -> 0.30）
    detail_ok, rec_ok = _sample_records(
        start_amount="1.00",
        rate_of_payment="0.7",
        insert_gun_fee="50",
    )
    proj_ok = order_settlement.project_settlement(rec_ok)
    res_ok = _by_label(order_settlement.verify(detail_ok, proj_ok))
    assert res_ok["规则2 启动费服务费"]["calculated"] == Decimal("0.30")
    assert res_ok["规则2 启动费服务费"]["status"] == "通过"

    # 场景 B（已确认口径 2）：insertGunFee=99.8% 且 rateOfPayment=0.006，后端 99.8+0.006<=100 漏扣 0.01 溢出
    detail_bug, rec_bug = _sample_records(
        start_amount="1.00",
        rate_of_payment="0.006",
        insert_gun_fee="99.8",
        override_start_srv="1.00",
    )
    proj_bug = order_settlement.project_settlement(rec_bug)
    res_bug = order_settlement.verify(detail_bug, proj_bug)
    r2 = _by_label(res_bug)["规则2 启动费服务费"]
    assert r2["calculated"] == Decimal("0.99")
    assert r2["recorded"] == Decimal("1.00")
    assert r2["difference"] == Decimal("0.01")
    assert r2["status"] == "不一致"
    assert "未扣减通道费溢出" in r2["reason"]
    assert "发现商城结算单账面清分不一致" in res_bug["conclusion"]


def test_rule3_and_rule4_detect_payment_and_channel_mismatches():
    detail, records = _sample_records(override_payment_price="30.00", override_fee_channel="0.99")
    detail["payAmount"] = "33.00"
    records["mall_items"][1]["rate_of_payment"] = "0.50"
    proj = order_settlement.project_settlement(records)
    checks = _by_label(order_settlement.verify(detail, proj))
    assert checks["规则3-1 结算单实付款组成"]["status"] == "不一致"
    assert checks["规则3-2 充电单与结算单实付款一致"]["status"] == "不一致"
    assert checks["规则4-1 电费支付渠道费"]["status"] == "不一致"
    assert checks["规则4-4 支付通道费合计"]["status"] == "不一致"


# --- 3. 已确认口径 1：附加科目、按度抽成、缺配置、退款、未支付订单的“待验证”保护 ---


def test_extra_fee_items_mark_affected_summaries_unverified_without_false_mismatch():
    extra = [
        {
            "id": "item-3",
            "order_id": "MALL-1",
            "sku_code": "3",
            "spu_name": "充电桩停车费",
            "quantity": 1,
            "status": "0",
            "commission_flag": "0",
            "tax_rate": None,
            "sales_price": "5.00",
            "payment_price": "5.00",
            "refund_amount": "0.00",
            "payment_coupon_price": "0.00",
            "platform_discount_amount": "0.00",
            "platform_balance_reduce": "0.00",
            "shop_balance_reduce": "0.00",
            "market_price": "0.00",
            "service_fee": "0.50",
            "rate_of_payment": "0.03",
            "commission_base_amount": "0.00",
            "settlement_amount": "4.47",
            "goods_cost_price": "0.00",
        }
    ]
    detail, records = _sample_records(
        extra_items=extra,
        override_settlement_amount="26.00",
        override_order_service="1.10",
        override_fee_channel="0.23",
    )
    proj = order_settlement.project_settlement(records)
    assert proj["settlement"]["parkingFeesAmount"] == "5.00"
    res = order_settlement.verify(detail, proj)
    checks = _by_label(res)
    for affected in (
        "规则1 订单结算金额",
        "规则4-4 支付通道费合计",
        "规则5 平台服务费",
        "规则6 订单分佣基数",
    ):
        assert checks[affected]["status"] == "待验证"
        assert "停车费" in checks[affected]["reason"]
    # 独立分项核验不受停车费影响，保持精准通过
    assert checks["规则2 启动费服务费"]["status"] == "通过"
    assert checks["规则4-1 电费支付渠道费"]["status"] == "通过"
    assert "部分规则因附加科目或前置条件待验证" in res["conclusion"]


def test_per_kwh_mode_missing_config_mode_conflict_and_refund_mark_unverified():
    # 按度定额抽成
    d1, r1 = _sample_records(per_kwh_fee="0.05", override_order_service="0.95")
    c1 = _by_label(order_settlement.verify(d1, order_settlement.project_settlement(r1)))
    assert c1["规则5 平台服务费"]["status"] == "待验证"
    assert "settleFeeOfElect" in c1["规则5 平台服务费"]["reason"]

    # 缺失 shop_level
    d2, r2 = _sample_records(missing_shop_level=True)
    c2 = _by_label(order_settlement.verify(d2, order_settlement.project_settlement(r2)))
    assert c2["规则2 启动费服务费"]["status"] == "待验证"
    assert c2["规则5 平台服务费"]["status"] == "待验证"

    # 模式冲突：ch_order_info.settlement_type=0 但商城电费子项 is_commission=1
    d3, r3 = _sample_records(
        order_settlement_type=0, elec_commission="1", start_commission="0", tax_commission="0"
    )
    p3 = order_settlement.project_settlement(r3)
    assert p3["settlement_mode"]["mode_conflict"] is True
    c3 = _by_label(order_settlement.verify(d3, p3))
    assert c3["规则1 订单结算金额"]["status"] == "待验证"
    assert c3["规则6 订单分佣基数"]["status"] == "待验证"

    # 非零退款
    d4, r4 = _sample_records(refund_amount="1.00")
    c4 = _by_label(order_settlement.verify(d4, order_settlement.project_settlement(r4)))
    assert c4["规则1 订单结算金额"]["status"] == "待验证"
    assert c4["规则3-2 充电单与结算单实付款一致"]["status"] == "待验证"


def test_unsynced_or_unpaid_mall_order_returns_unverified():
    detail, records = _sample_records()
    records["mall_orders"] = []
    records["mall_order_rows"] = 0
    records["charging"]["is_pay"] = 0
    proj = order_settlement.project_settlement(records)
    assert proj["settlement_state"] == "not_synced"
    res = order_settlement.verify(detail, proj)
    assert all(item["status"] == "待验证" for item in res["checks"])
    assert "尚未生成已支付的商城结算单" in res["conclusion"]

    detail2, records2 = _sample_records(is_pay="0")
    proj2 = order_settlement.project_settlement(records2)
    assert proj2["settlement_state"] == "unpaid_mall_order"
    res2 = order_settlement.verify(detail2, proj2)
    assert all(item["status"] == "待验证" for item in res2["checks"])


# --- 4. Gateway /cpms 代理端到端与证据降级渲染集成测试 ---


def test_gateway_proxy_settlement_flow_and_prerequisite_guards(tmp_path, monkeypatch):
    detail, records = _sample_records()

    class _StubSource:
        def operator_settlement_records(self, order_no: str, *, expected_site_id: str | None = None):
            assert order_no == ORDER and expected_site_id == "SITE-1"
            return records

    @contextmanager
    def _stub_mysql(_settings, _scope):
        yield _StubSource()

    monkeypatch.setattr(order_settlement, "scoped_mysql_source", _stub_mysql)

    with environment(tmp_path, monkeypatch) as f:

        def dispatch(_, _config, run_id):
            head = {"Authorization": "Bearer " + NODE_A_TOKEN, **CONTRACT}
            pub = f["client"].post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
            head.update(
                {
                    "X-AI-Ops-Exec-Config": pub.json()["sha256"],
                    "X-AI-Ops-Tool-Contract": operator_cpms.OBJECT_CONTRACT,
                }
            )
            # 1) 未读 detail 直接调 cpms_order_settlement 应被 OPERATOR_DETAIL_REQUIRED 拒绝
            grant_early = (
                f["client"]
                .post(
                    f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_settlement/authorization",
                    headers=head,
                    json={"arguments": {"order_no": ORDER}},
                )
                .json()
            )
            early_resp = f["client"].get(
                grant_early["base_url"] + grant_early["request"]["path"],
                headers={"Authorization": "Bearer " + grant_early["bearer"]},
            )
            assert early_resp.status_code == 403 and "OPERATOR_DETAIL_REQUIRED" in early_resp.text

            # 2) 先交付 detail，未交付 settlement 时调 settlement_check 应报 OPERATOR_EVIDENCE_MISSING
            call_tool(f, run_id, "cpms_order_detail", {"order_no": ORDER})
            grant_check_early = (
                f["client"]
                .post(
                    f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_settlement_check/authorization",
                    headers=head,
                    json={"arguments": {}},
                )
                .json()
            )
            early_check = f["client"].get(
                grant_check_early["base_url"] + grant_check_early["request"]["path"],
                headers={"Authorization": "Bearer " + grant_check_early["bearer"]},
            )
            assert early_check.status_code == 403 and "OPERATOR_EVIDENCE_MISSING" in early_check.text

            # 3) 正常链路：detail -> settlement -> settlement_check
            settle_ev, _, _ = call_tool(f, run_id, "cpms_order_settlement", {"order_no": ORDER})
            assert settle_ev["data"]["settlement_state"] == "ready"
            assert settle_ev["data"]["settlement_mode"]["effective_mode"] == 0

            check_ev, headers, _ = call_tool(f, run_id, "cpms_order_settlement_check", {})
            assert check_ev["data"]["settlement_mode"]["effective_mode"] == 0
            assert "limitations" in check_ev["data"]
            assert any(item["label"] == "规则1 订单结算金额" for item in check_ev["data"]["checks"])

            finish(
                f,
                run_id,
                headers,
                "本单按电费单独结算，已完成商城结算单核验，不代表实际打款到账。",
                ["cpms_order_settlement_check"],
            )

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        submitted = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": f"帮我分析一下订单 {ORDER} 的结算金额和分佣基数"},
        )
        assert submitted.status_code == 202, submitted.text
        final = terminal(f, submitted.json()["qa_id"]).json()
        assert final["status"] == "completed", final
        titles = [item["title"] for item in final["result"]["citations"]]
        assert "订单商城结算与抽成配置" in titles
        assert "订单结算金额核验" in titles


@pytest.mark.parametrize(
    "returned,expected_code",
    [({"disabled": True}, "OPERATOR_SETTLEMENT_UNAVAILABLE"), (None, "CPMS_SCOPE_MISMATCH")],
)
def test_gateway_proxy_settlement_refuses_disabled_and_out_of_scope(
    tmp_path, monkeypatch, returned, expected_code
):
    class _StubSource:
        def __init__(self, payload):
            self._payload = payload

        def operator_settlement_records(self, order_no: str, *, expected_site_id: str | None = None):
            return self._payload

    @contextmanager
    def _stub_mysql(_settings, _scope):
        yield _StubSource(returned)

    monkeypatch.setattr(order_settlement, "scoped_mysql_source", _stub_mysql)
    with environment(tmp_path, monkeypatch) as f:
        seen: list[tuple[int, str]] = []

        def dispatch(_, _config, run_id):
            _, head, _ = call_tool(f, run_id, "cpms_order_detail", {"order_no": ORDER})
            head["X-AI-Ops-Tool-Contract"] = operator_cpms.OBJECT_CONTRACT
            grant = (
                f["client"]
                .post(
                    f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_settlement/authorization",
                    headers=head,
                    json={"arguments": {"order_no": ORDER}},
                )
                .json()
            )
            resp = f["client"].get(
                grant["base_url"] + grant["request"]["path"],
                headers={"Authorization": "Bearer " + grant["bearer"]},
            )
            seen.append((resp.status_code, resp.text))
            finish(f, run_id, head, "暂时无法核验商城结算明细。", ["cpms_order_detail"])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        qa_id = request(f["client"]).json()["qa_id"]
        terminal(f, qa_id)
        assert seen and seen[0][0] == 403 and expected_code in seen[0][1]


def test_render_evidence_distinguishes_settlement_raw_and_computed():
    rendered = operator_evidence.render_evidence(
        {
            "cpms_order_detail": {
                "data": {"orderNo": ORDER, "status": 1, "payAmount": "33.00"},
                "observed_at": "2026-11-28T10:00:00+00:00",
            },
            "cpms_order_settlement": {
                "data": {"orderNo": ORDER, "settlement_state": "ready"},
                "observed_at": "2026-11-28T10:00:01+00:00",
            },
            "cpms_order_settlement_check": {
                "data": {"conclusion": "商城结算单 6 类账面清分规则算术对照一致"},
                "observed_at": "2026-11-28T10:00:02+00:00",
            },
        },
        "zh",
    )
    text = rendered["blocks"][0]["text"]
    assert "订单商城结算与抽成配置\n来源记录摘录" in text
    assert "订单结算金额核验\n核验工具已有输出" in text
