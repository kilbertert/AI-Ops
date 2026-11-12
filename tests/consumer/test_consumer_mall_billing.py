"""商城侧支付证据：来源原值、逐项相等核对、缺口如实标注。

判据来自 docs/validation/2026-11-12-consumer-mall-payment.md 的已核实链路与真实行，
不是从候选输出反推：4.62 税前费用、0.09 商城税项、4.71 成功支付记录是同一份
授权证据读到的三个值。
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.consumer.consumer_mall_billing import mall_payment_evidence
from aiops_diagnostics.data.sources import MySQLSource
from aiops_diagnostics.identity.query_scope import QueryScope

TENANT = "TENANT-A"
USER = "USER-A"
CHARGING_ORDER = "TEST-CHARGE-001"
OUT_TRADE_NO = "TEST-MALL-001"
PARENT_ID = "PARENT-001"
CHILD_ID = "CHILD-001"


def charging(**overrides):
    row = {
        "id": "CHARGING-ID",
        "order_no": CHARGING_ORDER,
        "tenant_id": TENANT,
        "out_trade_no": OUT_TRADE_NO,
        "total_amount": Decimal("4.62"),
        "pay_amount": Decimal("4.71"),
        "vat": Decimal("2.00"),
        "vat_fee": Decimal("0.09"),
        "is_pay": 1,
        "refund_status": 0,
        "sync_mall_order": None,
        "user_id": USER,
        "consumer_id": None,
        "market_amount": Decimal("0"),
        "reduce_balance": Decimal("0"),
    }
    row.update(overrides)
    return row


def parent(**overrides):
    row = {
        "id": PARENT_ID,
        "order_ids": '["CHILD-001"]',
        "order_no": OUT_TRADE_NO,
        "sales_price": Decimal("4.71"),
        "payment_price": Decimal("4.71"),
        "paid_amount": Decimal("4.62"),
        "payment_time": None,
        "status": "1",
        "create_time": None,
    }
    row.update(overrides)
    return row


def child(**overrides):
    row = {
        "id": CHILD_ID,
        "parent_id": PARENT_ID,
        "order_no": CHARGING_ORDER,
        "third_order_sn": None,
        "order_type": "11",
        "order_tag": 18,
        "status": "3",
        "sales_price": Decimal("4.71"),
        "total_amount": Decimal("4.62"),
        "payment_price": Decimal("4.71"),
        "paid_amount": Decimal("4.71"),
        "refund_amount": Decimal("0.00"),
        "payment_time": None,
        "create_time": None,
    }
    row.update(overrides)
    return row


def tax_item(**overrides):
    row = {
        "order_id": CHILD_ID,
        "sku_code": "9",
        "tax_rate": Decimal("2.00"),
        "sales_price": Decimal("0.09"),
        "payment_price": Decimal("0.09"),
        "refund_amount": Decimal("0.00"),
        "status": "0",
    }
    row.update(overrides)
    return row


def fee_item(sku, price):
    return {
        "order_id": CHILD_ID,
        "sku_code": sku,
        "tax_rate": None,
        "sales_price": Decimal(price),
        "payment_price": Decimal(price),
        "refund_amount": Decimal("0.00"),
        "status": "0",
    }


def payment(**overrides):
    row = {
        "id": "PAYMENT-001",
        "order_no": None,
        "out_trade_no": OUT_TRADE_NO,
        "sub_out_trade_no": None,
        "scene": "ORDER",
        "state": 1,
        "payment_type": "3",
        "amount": Decimal("4.71"),
        "sub_amount": Decimal("0.00"),
        "refund_amount": Decimal("0.00"),
        "pay_time": None,
        "create_time": None,
    }
    row.update(overrides)
    return row


def snapshot(**overrides):
    return {
        "order_no": CHARGING_ORDER,
        "order_amount": "4.62",
        "recorded_payment_amount": "4.71",
        "refund_status": "0",
        "recorded_refund_amount": "0.00",
        "fee_components": {"vat": "2.00", "vatFee": "0.09"},
        **overrides,
    }


def project(rows):
    return mall_payment_evidence(rows, snapshot())


def payload(**overrides):
    base = {
        "charging": charging(),
        "charging_rows": 1,
        "parents": [parent()],
        "orders": [child()],
        "items": [fee_item("1", "1.09"), tax_item()],
        "payments": [payment()],
        "truncated": [],
    }
    base.update(overrides)
    return base


def test_linked_evidence_preserves_source_values_and_confirms_tax_in_payment():
    evidence = project(payload())

    assert evidence["status"] == "linked"
    assert evidence["payment_includes_tax"] == "confirmed"
    assert evidence["checks"] == {
        "displayed_snapshot": "match",
        "fee_base": "match",
        "payment_amount": "match",
        "tax": "match",
    }
    assert evidence["parent_order"]["payment_price"] == "4.71"
    assert "paid_amount" not in evidence["parent_order"], "被覆盖字段不投影"
    assert evidence["orders"][0]["paid_amount"] == "4.71"
    assert evidence["tax_items"][0]["payment_price"] == "0.09"
    assert evidence["payments"][0]["amount"] == "4.71"
    assert "status" not in evidence["parent_order"]
    assert all("status" not in row for row in evidence["orders"] + evidence["tax_items"])
    assert evidence["parent_paid_consistency"] == "conflicting"
    assert evidence["excluded_sources"] == ["mall.order_info_parent.paid_amount"]
    assert evidence["refund_present"] is False
    assert evidence["limitations"]


def test_parent_paid_matching_successful_payment_reports_consistent():
    evidence = project(payload(parents=[parent(paid_amount=Decimal("4.71"))]))
    assert evidence["parent_paid_consistency"] == "consistent"
    assert "paid_amount" not in evidence["parent_order"]


@pytest.mark.parametrize(
    "rows,expected",
    [
        (payload(orders=[], items=[]), "unconfirmed"),
        (payload(payments=[]), "unconfirmed"),
        (payload(items=[fee_item("1", "1.09")]), "unconfirmed"),
        # 重复成功支付不能归属税费，仍并列原值。
        (payload(payments=[payment(), payment(id="2")]), "unconfirmed"),
        # 费用基数不一致：商城子单与充电订单总额不同。
        (payload(orders=[child(total_amount=Decimal("9.99"))]), "unconfirmed"),
        # 税项金额与充电回写税额不同。
        (payload(items=[tax_item(payment_price=Decimal("1.00"))]), "unconfirmed"),
        (payload(truncated=["payments"]), "unconfirmed"),
    ],
)
def test_unconfirmed_when_association_incomplete_or_inconsistent(rows, expected):
    evidence = project(rows)
    assert evidence["status"] == "linked"
    assert evidence["payment_includes_tax"] == expected


def test_refund_records_are_flagged_not_derived():
    evidence = project(payload(payments=[payment(refund_amount=Decimal("1.00"))]))
    assert evidence["refund_present"] is True
    assert evidence["payments"][0]["refund_amount"] == "1.00"


def test_tax_item_and_unreadable_refunds_are_not_reported_as_none():
    """税项也带 refund_amount（独立 Hack 反例）；读不出的值不能报「无退款」。"""
    evidence = project(payload(items=[tax_item(refund_amount=Decimal("0.50"))]))
    assert evidence["refund_present"] is True
    evidence = project(payload(orders=[child(refund_amount="not-a-number")]))
    assert evidence["refund_present"] == "unknown"


def test_untied_successful_payment_cannot_drive_paid_consistency():
    """防御性投影：仅靠充电单号相似的支付不属于本父单。"""
    foreign = payment(order_no=CHARGING_ORDER, out_trade_no="OTHER-TRADE")
    evidence = project(payload(payments=[foreign]))
    assert evidence["status"] == "linked"
    assert evidence["parent_paid_consistency"] == "unknown"
    assert evidence["payment_includes_tax"] == "unconfirmed"
    assert evidence["successful_payment_count"] == 0
    assert evidence["payments"] == []
    assert evidence["refund_present"] == "unknown"


def test_missing_or_non_dict_fee_components_degrade_to_unknown():
    result = mall_payment_evidence(payload(), snapshot(fee_components=None))
    assert result["checks"]["displayed_snapshot"] == "unknown"
    assert result["payment_includes_tax"] == "unconfirmed"


def test_empty_linkage_values_do_not_tie_payment_to_parent():
    """None/'' 相等是空匹配不是归属证据（复验反例 N5/N6）。"""
    evidence = project(
        payload(
            parents=[parent(order_no=None)],
            payments=[payment(order_no=None, out_trade_no=None)],
        )
    )
    assert evidence["parent_paid_consistency"] == "unknown"
    assert evidence["payment_includes_tax"] == "unconfirmed"
    empty = project(
        payload(
            parents=[parent(order_no="")],
            payments=[payment(order_no="", out_trade_no=None)],
        )
    )
    assert empty["parent_paid_consistency"] == "unknown"


def test_not_linked_and_ambiguous_and_unavailable_states():
    assert project(None)["status"] == "unavailable"
    assert project({"disabled": True})["status"] == "disabled"
    assert project(payload(charging=None, charging_rows=0))["status"] == "unavailable"
    assert project(payload(charging=None, charging_rows=2))["status"] == "ambiguous"
    no_ref = project(payload(charging=charging(out_trade_no=None)))
    assert no_ref["status"] == "not_linked" and no_ref["reason"] == "no_out_trade_no"
    assert project(payload(parents=[]))["status"] == "not_linked"
    dup = project(payload(parents=[parent(), parent(id="2")]))
    assert dup["status"] == "ambiguous" and dup["reason"] == "multiple_mall_parents"


def test_invalid_values_are_flagged_not_rewritten():
    evidence = project(payload(orders=[child(paid_amount="not-a-number", refund_amount=Decimal("1.00"))]))
    order = evidence["orders"][0]
    assert order["paid_amount"] is None and "paid_amount" in order["invalid_fields"]
    assert evidence["checks"]["payment_amount"] == "match", "paid_amount不参与支付链路核对"
    assert evidence["refund_present"] is True


# ---------------------------------------------------------------------------
# MySQLSource.mall_billing_records：SQL 形状与身份边界
# ---------------------------------------------------------------------------


class _MallConnection:
    """按 SQL 中的表名分发夹具行；记录每次执行的语句与参数。"""

    def __init__(self, *, charging_rows=None, parents=None, orders=None, items=None, payments=None):
        self.rows = {
            "ch_order_info": list(charging_rows or []),
            "order_info_parent": list(parents or []),
            "order_item": list(items or []),
            "pay_record_info": list(payments or []),
        }
        # order_info 既是被 JOIN 的子单也是 join 目标：按不含 "order_item" 的 SQL 区分。
        self.children = list(orders or [])
        self.executed: list[tuple[str, list[Any]]] = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, list(params or [])))

    def fetchall(self):
        sql = self.executed[-1][0]
        if "order_item" in sql:
            return self.rows["order_item"]
        if "order_info_parent" in sql:
            return self.rows["order_info_parent"]
        if "pay_record_info" in sql:
            return self.rows["pay_record_info"]
        if "ch_order_info" in sql:
            return self.rows["ch_order_info"]
        return self.children

    def fetchone(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _source(monkeypatch, connection, scope, *, mall_database="qumall_mall"):
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.mysql.mall_database = mall_database
    source = MySQLSource(settings, scope=scope)
    monkeypatch.setattr("aiops_diagnostics.data.sources.pymysql.connect", lambda **kwargs: connection)
    return source


def _scope(user_id=USER):
    return QueryScope(tenant_id=TENANT, site_ids=None, user_id=user_id)


def test_mall_records_requires_self_scope_user(monkeypatch):
    connection = _MallConnection(charging_rows=[charging()])
    assert _source(monkeypatch, connection, None).mall_billing_records(CHARGING_ORDER) is None
    blocked = QueryScope(TENANT, (), USER)
    assert _source(monkeypatch, connection, blocked).mall_billing_records(CHARGING_ORDER) is None
    assert connection.executed == [], "无主体用户绝不发起商城查询"


def test_mall_records_disabled_when_database_unconfigured(monkeypatch):
    connection = _MallConnection(charging_rows=[charging()])
    result = _source(monkeypatch, connection, _scope(), mall_database="").mall_billing_records(CHARGING_ORDER)
    assert result == {"disabled": True}


def test_mall_records_link_chain_uses_order_derived_keys_in_one_snapshot(monkeypatch):
    connection = _MallConnection(
        charging_rows=[charging()],
        parents=[parent()],
        orders=[child()],
        items=[tax_item()],
        payments=[payment()],
    )
    result = _source(monkeypatch, connection, _scope()).mall_billing_records(CHARGING_ORDER)

    assert result["charging"]["out_trade_no"] == OUT_TRADE_NO
    assert result["parents"][0]["order_no"] == OUT_TRADE_NO
    assert result["orders"] == [child()]
    assert result["items"] == [tax_item()]
    assert result["payments"] == [payment()]

    statements = [sql for sql, _ in connection.executed]
    assert statements[2] == "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
    charge_sql, charge_params = connection.executed[3]
    assert "ch_order_info" in charge_sql and "LIMIT 2" in charge_sql
    # self 范围的工单认领谓词与主体用户一起下推。
    assert "consumer_id" in charge_sql and charge_params == [CHARGING_ORDER, TENANT, "-1", USER]

    parent_sql, parent_params = connection.executed[4]
    assert "qumall_mall" in parent_sql and "order_info_parent" in parent_sql
    assert parent_params == [TENANT, USER, OUT_TRADE_NO], "商城行必须同租户同调用者用户"

    child_sql, child_params = connection.executed[5]
    assert "parent_id=%s" in child_sql and "LIMIT 11" in child_sql
    assert child_params == [TENANT, USER, PARENT_ID]

    item_sql, item_params = connection.executed[6]
    assert "order_item" in item_sql and "LIMIT 31" in item_sql
    assert item_params == [TENANT, USER, CHILD_ID]

    pay_sql, pay_params = connection.executed[7]
    assert "pay_record_info" in pay_sql and "LIMIT 16" in pay_sql
    # 支付记录只按已核实的商城父单号匹配。
    assert pay_params == [TENANT, USER, OUT_TRADE_NO, OUT_TRADE_NO]


def test_mall_records_stop_at_missing_or_duplicate_chain_links(monkeypatch):
    connection = _MallConnection(charging_rows=[])
    result = _source(monkeypatch, connection, _scope()).mall_billing_records(CHARGING_ORDER)
    assert result["charging"] is None and result["charging_rows"] == 0
    assert len(connection.executed) == 4, "没有订单就不读商城"

    connection = _MallConnection(charging_rows=[charging()], parents=[parent(), parent()])
    result = _source(monkeypatch, connection, _scope()).mall_billing_records(CHARGING_ORDER)
    assert len(result["parents"]) == 2 and result["orders"] == []
    assert len(connection.executed) == 5, "父单不唯一就不读子单/明细/支付"


def test_mall_records_flags_truncated_tables(monkeypatch):
    connection = _MallConnection(
        charging_rows=[charging()],
        parents=[parent()],
        orders=[child() for _ in range(11)],
        items=[tax_item() for _ in range(31)],
        payments=[payment() for _ in range(16)],
    )
    result = _source(monkeypatch, connection, _scope()).mall_billing_records(CHARGING_ORDER)
    assert sorted(result["truncated"]) == ["items", "orders", "payments"]
    assert len(result["orders"]) == 10 and len(result["payments"]) == 15


# ---------------------------------------------------------------------------
# _authorized_consumer_snapshot 接线：mall_payment 进账务快照
# ---------------------------------------------------------------------------


def test_gateway_attaches_mall_payment_to_billing_snapshot(tmp_path, monkeypatch):
    from dify.test_consumer_dependencies import settings as gateway_settings

    from aiops_diagnostics.consumer import consumer_billing
    from aiops_diagnostics.consumer.consumer_knowledge_grants import ConsumerEndpoints
    from aiops_diagnostics.gateway import gateway_api as api

    settings = gateway_settings(tmp_path, server_config_file=tmp_path / "production.env")
    (tmp_path / "production.env").write_text("# synthetic\n")
    (tmp_path / "production.env").chmod(0o600)
    ConsumerEndpoints(settings.database_file).register(
        "TENANT-A", "env-a", "https://a.example.test", "platform"
    )
    caller = SimpleNamespace(
        effective_tenant_id="TENANT-A",
        subject=SimpleNamespace(c_user_id=USER),
        data_scope=SimpleNamespace(type="self", site_ids=(), shop_ids=()),
        delegated=False,
    )
    monkeypatch.setattr(
        consumer_billing,
        "read_billing_record",
        lambda *args, **kwargs: snapshot(),
    )

    @contextlib.contextmanager
    def mysql(_settings, _scope):
        yield SimpleNamespace(mall_billing_records=lambda *_: payload())

    monkeypatch.setattr(api, "scoped_mysql_source", mysql)
    result = api._authorized_consumer_snapshot(
        settings, caller, CHARGING_ORDER, "billing", include_composition=True
    )
    assert result["order_no"] == CHARGING_ORDER
    assert result["mall_payment"]["status"] == "linked"
    assert result["mall_payment"]["payment_includes_tax"] == "confirmed"

    def broken(*_args, **_kwargs):
        raise api.SourceError("synthetic mall read failure")

    monkeypatch.setattr(api, "scoped_mysql_source", broken)
    result = api._authorized_consumer_snapshot(
        settings, caller, CHARGING_ORDER, "billing", include_composition=True
    )
    assert result["mall_payment"]["status"] == "unavailable", "证据读取失败只降级不打断账务"

    def tunnel_failure(*_args, **_kwargs):
        raise RuntimeError("synthetic tunnel failure")

    monkeypatch.setattr(api, "scoped_mysql_source", tunnel_failure)
    result = api._authorized_consumer_snapshot(
        settings, caller, CHARGING_ORDER, "billing", include_composition=True
    )
    assert result["mall_payment"]["status"] == "unavailable", "非信封异常同样只降级"


@pytest.mark.parametrize(
    "rows",
    [
        payload(items=[tax_item(), tax_item()]),
        payload(orders=[child(), child(id="OTHER-CHILD")]),
        payload(orders=[child(order_type="99")]),
        payload(orders=[child(order_no="OTHER-ORDER")]),
        payload(payments=[payment(refund_amount="1.00")]),
        payload(payments=[payment(refund_amount=None)]),
        payload(payments=[payment(sub_amount="1.00")]),
        payload(payments=[payment(scene="RECHARGE")]),
        payload(charging=charging(is_pay=0)),
        payload(charging=charging(market_amount="1.00")),
    ],
)
def test_unverified_shapes_never_confirm_tax_relation(rows):
    assert project(rows)["payment_includes_tax"] == "unconfirmed"


def test_http_snapshot_drift_does_not_confirm_from_sql_alone():
    result = mall_payment_evidence(payload(), snapshot(recorded_payment_amount="9.00"))
    assert result["checks"]["displayed_snapshot"] == "mismatch"
    assert result["payment_includes_tax"] == "unconfirmed"


def test_payment_state_and_source_time_survive_projection():
    result = project(payload(payments=[payment(state=0)]))
    assert result["payments"][0]["state"] == 0
    assert result["observed_at"]


@pytest.mark.parametrize(
    "query_scope,owner",
    [
        (QueryScope(TENANT, ("SITE-A",), None), charging()),
        (_scope(), charging(user_id="-1", consumer_id=USER)),
    ],
)
def test_authorized_preview_and_work_order_keep_mall_claimant(monkeypatch, query_scope, owner):
    connection = _MallConnection(charging_rows=[owner], parents=[parent()])
    source = _source(monkeypatch, connection, query_scope)
    source.mall_billing_records(CHARGING_ORDER)
    assert connection.executed[4][1] == [TENANT, USER, OUT_TRADE_NO]
    if query_scope.site_ids:
        assert "site_id IN" in connection.executed[3][0]
        assert "SITE-A" in connection.executed[3][1]


def test_legacy_callback_does_not_read_mall(tmp_path, monkeypatch):
    from dify.test_consumer_dependencies import settings as gateway_settings

    from aiops_diagnostics.consumer import consumer_billing
    from aiops_diagnostics.consumer.consumer_knowledge_grants import ConsumerEndpoints
    from aiops_diagnostics.gateway import gateway_api as api

    settings = gateway_settings(tmp_path)
    ConsumerEndpoints(settings.database_file).register(TENANT, "env-a", "https://a.example.test", "platform")
    caller = SimpleNamespace(effective_tenant_id=TENANT, subject=SimpleNamespace(c_user_id=USER))
    monkeypatch.setattr(consumer_billing, "read_billing_record", lambda *a, **k: snapshot())

    def must_not_read(*a, **k):
        raise AssertionError("legacy callback must not read mall")

    monkeypatch.setattr(api, "_consumer_mall_payment", must_not_read)
    assert "mall_payment" not in api._authorized_consumer_snapshot(
        settings, caller, CHARGING_ORDER, "billing"
    )


@pytest.mark.parametrize(
    "rows",
    [
        payload(orders=[child(refund_amount=None)]),
        payload(items=[tax_item(refund_amount=None)]),
        payload(truncated=["items"]),
        payload(orders=[], items=[], payments=[]),
    ],
)
def test_missing_or_truncated_refunds_are_unknown(rows):
    assert project(rows)["refund_present"] == "unknown"


def test_invalid_success_amount_does_not_claim_parent_conflict():
    result = project(payload(payments=[payment(amount="invalid")]))
    assert result["parent_paid_consistency"] == "unknown"


def test_unrelated_payment_refund_is_not_projected_as_this_order():
    result = project(
        payload(
            payments=[payment(), payment(order_no=CHARGING_ORDER, out_trade_no="OTHER", refund_amount="0.50")]
        )
    )
    assert len(result["payments"]) == 1
    assert result["refund_present"] == "unknown"
    assert result["payment_includes_tax"] == "unconfirmed"


@pytest.mark.parametrize(
    "query_scope,work_order,visible",
    [
        (QueryScope(TENANT, None, USER), False, True),
        (QueryScope(TENANT, None, USER), True, True),
        (QueryScope(TENANT, ("SITE-A",), None), False, True),
        (QueryScope("OTHER-TENANT", None, USER), False, False),
        (QueryScope(TENANT, None, "OTHER-USER"), False, False),
        (QueryScope(TENANT, ("OTHER-SITE",), None), False, False),
    ],
)
def test_actual_sql_filters_tenant_owner_site_and_unrelated_payment(
    monkeypatch, query_scope, work_order, visible
):
    """SQLite执行同一SELECT/WHERE/JOIN，独立数据行验证权限，不由假游标直接返回期望行。"""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    for name in ("charging", "mall"):
        db.execute(f"ATTACH DATABASE ':memory:' AS {name}")
    order = charging(user_id="-1" if work_order else USER, consumer_id=USER if work_order else None)
    tables = {
        "charging.ch_order_info": [{**order, "site_id": "SITE-A"}],
        "mall.order_info_parent": [parent()],
        "mall.order_info": [child()],
        "mall.order_item": [tax_item()],
        "mall.pay_record_info": [
            payment(),
            payment(order_no=CHARGING_ORDER, out_trade_no="OTHER", refund_amount="0.50"),
        ],
    }
    for table, rows in tables.items():
        rows = [{"tenant_id": TENANT, "user_id": USER, **row} for row in rows]
        keys = list(rows[0])
        db.execute(f"CREATE TABLE {table} ({', '.join(keys)})")
        db.executemany(
            f"INSERT INTO {table} VALUES ({', '.join('?' for _ in keys)})",
            [[str(v) if isinstance(v, Decimal) else v for v in row.values()] for row in rows],
        )
    calls = []

    class Cursor:
        def execute(self, sql, params):
            calls.append(sql)
            self.cursor = db.execute(sql.replace("%s", "?"), params)

        def fetchall(self):
            return [dict(row) for row in self.cursor.fetchall()]

    @contextlib.contextmanager
    def cursor(**kwargs):
        yield Cursor()

    settings = Settings.from_env()
    settings.mysql.database, settings.mysql.mall_database = "charging", "mall"
    source = MySQLSource(settings, query_scope)
    monkeypatch.setattr(source, "_cursor", cursor)
    try:
        result = source.mall_billing_records(CHARGING_ORDER)
        if visible:
            assert len(result["payments"]) == 1
            assert result["payments"][0]["out_trade_no"] == OUT_TRADE_NO
            assert project(result)["payment_includes_tax"] == "confirmed"
        else:
            assert result["charging"] is None
            assert len(calls) == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"refund_status": "1", "recorded_refund_amount": "0.50"},
        {"refund_status": None},
        {"recorded_refund_amount": None},
    ],
)
def test_http_refund_record_prevents_no_refund_tax_confirmation(fields):
    assert mall_payment_evidence(payload(), snapshot(**fields))["payment_includes_tax"] == "unconfirmed"


def test_truncated_payments_cannot_prove_unique_payment_consistency():
    result = project(payload(truncated=["payments"]))
    assert result["parent_paid_consistency"] == "unknown"
    assert result["checks"]["payment_amount"] == "unknown"
