from __future__ import annotations

from typing import Any

import pytest

from aiops_diagnostics.config import Settings
from aiops_diagnostics.order_visibility import (
    TenantVisibility,
    VisibilityProfile,
    normalize_tenant,
    visible_orders,
)
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import MySQLSource

TENANT = "TENANT-A"
SITES = ("SITE-A-1", "SITE-A-2")
USER = "C-TARGET-2"


class _ScopedConnection:
    """记录最后执行的 SQL/参数并返回可配置的行。"""

    def __init__(
        self,
        *,
        orders: list[dict[str, Any]] | None = None,
        fee: dict[str, Any] | None = None,
        occupy: list[dict[str, Any]] | None = None,
        device: dict[str, Any] | None = None,
        sites: list[dict[str, Any]] | None = None,
        occupy_columns: list[str] | None = None,
        exists_first_row: bool = True,
    ) -> None:
        self.orders = orders or []
        self.fee = fee
        self.occupy = occupy or []
        self.device = device
        self.sites = sites or [{"id": "SITE-A-1"}, {"id": "SITE-A-2"}]
        self.occupy_columns = occupy_columns or [
            "id",
            "orderId",
            "order_no",
            "device_id",
            "device_code",
            "child_device_id",
            "child_device_code",
            "site_id",
            "userId",
            "free_time",
            "timeout",
            "occupy_amount",
            "pay_amount",
            "status",
            "out_trade_no",
            "is_pay",
            "is_sync_mall_order",
            "pay_time",
            "tenant_id",
            "startTime",
            "endTime",
            "operator_id",
            "refund_status",
            "refund_amount",
            "refund_time",
            "refundRemark",
        ]
        self.exists_first_row = exists_first_row
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
        last_sql = self.executed[-1][0]
        if "information_schema.columns" in last_sql:
            return [{"column_name": column} for column in self.occupy_columns]
        if "ch_site" in last_sql:
            return self.sites
        if "ch_occupy_order_info" in last_sql:
            return self.occupy
        return self.orders

    def fetchone(self):
        last_sql = self.executed[-1][0]
        if "ch_fee_template_record" in last_sql:
            return self.fee
        if "SELECT 1 FROM" in last_sql:
            return {"1": "1"} if self.exists_first_row else None
        if "ch_order_info" in last_sql and "ch_fee" not in last_sql:
            return self.orders[0] if self.orders else None
        return self.device

    def rollback(self):
        return None

    def close(self):
        return None


def _source(
    connection: _ScopedConnection, scope: QueryScope | None = None
) -> tuple[MySQLSource, _ScopedConnection]:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    src = MySQLSource(settings, scope=scope)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **kwargs: connection)
    return src, connection


def _rows_selected_by(rows: list[dict[str, Any]], where: str, params: list[Any]) -> list[dict[str, Any]]:
    """在 Python 里按真实渲染出的 WHERE 片段选行，作为 SQL 下推的行级镜像。

    只解释共享规则渲染的租户谓词（``1=0`` / ``=`` / ``IN``）并按同一套归一化
    比较行内租户；站点/用户片段不属于租户可见性，直接跳过。若 SQL 渲染与行级
    规则不是同一处定义，两者选出的行集就会在这里分叉。
    """
    if where == "1=0":
        return []
    remaining = list(params)
    for fragment in where.split(" AND "):
        if fragment == "tenant_id=%s":
            allowed = {str(remaining.pop(0))}
        elif fragment.startswith("tenant_id IN ("):
            allowed = {str(remaining.pop(0)) for _ in range(fragment.count("%s"))}
        else:
            continue
        rows = [row for row in rows if normalize_tenant(row.get("tenant_id")) in allowed]
    return rows


def test_scoped_order_query_pushes_tenant_and_site_filters() -> None:
    connection = _ScopedConnection(orders=[{"order_no": "O-1", "tenant_id": TENANT, "site_id": "SITE-A-1"}])
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    rows = source.get_orders("O-1")

    sql, params = conn.executed[-1]
    assert "tenant_id=%s" in sql
    assert "site_id IN (%s, %s)" in sql
    assert params == ["O-1", TENANT, "SITE-A-1", "SITE-A-2"]
    assert rows[0]["order_no"] == "O-1"


def test_scoped_order_query_ignores_caller_tenant_argument() -> None:
    connection = _ScopedConnection(orders=[{"order_no": "O-1"}])
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    source.get_orders("O-1", tenant_id="TENANT-B")

    sql, params = conn.executed[-1]
    assert "TENANT-B" not in params
    assert params[1] == TENANT


@pytest.mark.parametrize(
    "scope",
    [
        QueryScope(tenant_id=TENANT, site_ids=None, user_id=None),
        # 带空白的租户：SQL 绑定值必须与行级规则同一套归一化，否则同一订单在
        # 两个渲染里得到不同结论。
        QueryScope(tenant_id=f" {TENANT} ", site_ids=None, user_id=None),
        # 站点/用户下推并存时，租户谓词仍是那一处定义。
        QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=USER),
        # 无法归一化的租户不是可用身份：渲染为"什么都不可见"，不绑定空值。
        QueryScope(tenant_id="   ", site_ids=None, user_id=None),
    ],
)
def test_scope_where_selects_the_same_rows_as_the_shared_row_rule(scope: QueryScope) -> None:
    rows: list[dict[str, Any]] = [
        {"order_no": "O-1", "tenant_id": TENANT},
        {"order_no": "O-2", "tenant_id": "TENANT-B"},
        {"order_no": "O-3", "tenant_id": b"TENANT-A"},
        {"order_no": "O-4", "tenant_id": f" {TENANT} "},
        {"order_no": "O-5"},
    ]
    source, _ = _source(_ScopedConnection(), scope)
    where, params = source._scope_where()

    tenant = normalize_tenant(scope.tenant_id)
    visibility = TenantVisibility(
        profile=VisibilityProfile.CALLER,
        allowed=frozenset({tenant}) if tenant else frozenset(),
    )
    via_sql = _rows_selected_by(rows, where, params)
    via_rule = list(visible_orders(rows, visibility).rows)
    assert [row["order_no"] for row in via_sql] == [row["order_no"] for row in via_rule]


def test_scoped_order_query_applies_user_filter_for_self_scope() -> None:
    connection = _ScopedConnection(orders=[])
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=None, user_id=USER))

    source.get_orders("O-1")

    sql, params = conn.executed[-1]
    assert "user_id=%s" in sql
    assert USER in params


def test_scoped_order_query_short_circuits_on_empty_site_scope() -> None:
    # 空站点范围是"什么都不可见"的另一种输入：直连面短路（不发 SQL），与共享
    # 规则的空范围渲染同一结论。
    source, conn = _source(_ScopedConnection(), QueryScope(tenant_id=TENANT, site_ids=(), user_id=None))

    rows = source.get_orders("O-1")

    assert rows == []
    assert conn.executed == []  # 不发起任何 SQL


def test_scoped_fee_template_checks_order_existence_within_scope() -> None:
    connection = _ScopedConnection(
        fee={"order_no": "O-1", "tenant_id": TENANT, "fee_template": "{}"},
        exists_first_row=True,
    )
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    record = source.get_fee_template_record("O-1")

    assert record is not None
    # 索引 0/1 是 SET SESSION 与 START TRANSACTION
    assert "SELECT 1 FROM" in conn.executed[2][0]
    assert conn.executed[2][1] == ["O-1", TENANT, "SITE-A-1", "SITE-A-2"]
    assert "AND tenant_id=%s" in conn.executed[3][0]
    assert conn.executed[3][1] == ["O-1", TENANT]


def test_scoped_fee_template_returns_none_when_order_not_in_scope() -> None:
    connection = _ScopedConnection(fee={"order_no": "O-1"}, exists_first_row=False)
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    record = source.get_fee_template_record("O-1")

    assert record is None
    assert len([e for e in conn.executed if "SELECT" in e[0]]) == 1  # 只执行存在性检查


def test_occupy_orders_requires_exactly_one_lookup_key() -> None:
    source, _ = _source(_ScopedConnection(), QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    with pytest.raises(ValueError, match="必须且只能提供"):
        source.get_occupy_orders()
    with pytest.raises(ValueError, match="必须且只能提供"):
        source.get_occupy_orders(order_id="1", order_no="O-1")


def test_scoped_occupy_orders_lookup_by_order_id_and_scope() -> None:
    connection = _ScopedConnection(occupy=[{"orderId": "100", "order_no": "O-1", "tenant_id": TENANT}])
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    source.get_occupy_orders(order_id="100")

    sql, params = conn.executed[-1]
    assert "orderId=%s" in sql
    assert "userId=%s" not in sql  # user 过滤仅 self 范围才下推
    assert params == ["100", TENANT, "SITE-A-1", "SITE-A-2"]


def test_occupy_orders_maps_snake_case_columns_to_contract_names() -> None:
    snake_columns = [
        "id",
        "order_id",
        "order_no",
        "device_id",
        "device_code",
        "child_device_id",
        "child_device_code",
        "site_id",
        "user_id",
        "free_time",
        "timeout",
        "occupy_amount",
        "pay_amount",
        "status",
        "out_trade_no",
        "is_pay",
        "is_sync_mall_order",
        "pay_time",
        "tenant_id",
        "start_time",
        "end_time",
        "operator_id",
        "refund_status",
        "refund_amount",
        "refund_time",
        "refund_remark",
    ]
    connection = _ScopedConnection(occupy_columns=snake_columns)
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    source.get_occupy_orders(order_id="100")

    sql, params = conn.executed[-1]
    assert "order_id=%s" in sql
    assert "`order_id` AS `orderId`" in sql
    assert "`start_time`" in sql
    assert "`user_id` AS `userId`" in sql
    assert params == ["100", TENANT, "SITE-A-1", "SITE-A-2"]


def test_scoped_occupy_orders_applies_user_filter_for_self_scope() -> None:
    connection = _ScopedConnection(occupy=[])
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=None, user_id=USER))

    source.get_occupy_orders(order_no="O-1")

    sql, params = conn.executed[-1]
    assert "order_no=%s" in sql
    assert "userId=%s" in sql
    assert USER in params


def test_occupy_orders_limits_to_fixed_ceiling() -> None:
    connection = _ScopedConnection(occupy=[{"orderId": f"{i}", "tenant_id": TENANT} for i in range(25)])
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    rows = source.get_occupy_orders(order_id="100")

    assert "LIMIT 20" in conn.executed[-1][0]
    assert len(rows) == 25  # SQL 侧 LIMIT 由服务端强制，测试记录原始行数


def test_scoped_device_query_filters_by_site_without_user_column() -> None:
    connection = _ScopedConnection(device={"id": "D-1", "tenant_id": TENANT, "site_id": "SITE-A-1"})
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    device = source.get_device("D-1", None, "TENANT-IGNORED")

    assert device is not None
    sql, params = conn.executed[-1]
    assert "site_id IN (%s, %s)" in sql
    assert "user_id" not in sql
    assert params[0] == "D-1"
    assert params[1] == TENANT


def test_scoped_device_query_short_circuits_on_empty_site_scope() -> None:
    source, conn = _source(_ScopedConnection(), QueryScope(tenant_id=TENANT, site_ids=(), user_id=None))

    device = source.get_device(None, "PILE-1")

    assert device is None
    assert conn.executed == []


def test_site_ids_by_shops_resolves_sites_bound_and_read_only() -> None:
    connection = _ScopedConnection(sites=[{"id": "SITE-B-1"}, {"id": "SITE-B-2"}])
    source, conn = _source(connection)

    site_ids = source.site_ids_by_shops(("SHOP-B-1", "SHOP-B-2"), TENANT)

    assert site_ids == ("SITE-B-1", "SITE-B-2")
    sql, params = conn.executed[-1]
    assert "ch_site" in sql
    assert "shop_id IN (%s, %s)" in sql
    assert "LIMIT 1000" in sql
    assert params == [TENANT, "SHOP-B-1", "SHOP-B-2"]


def test_site_ids_by_points_resolves_sites_bound_and_read_only() -> None:
    connection = _ScopedConnection(sites=[{"id": "SITE-C-1"}])
    source, conn = _source(connection)

    site_ids = source.site_ids_by_points(("P-1",), TENANT)

    assert site_ids == ("SITE-C-1",)
    sql, _ = conn.executed[-1]
    assert "dis_point_id IN (%s)" in sql


def test_site_ids_by_shops_rejects_unbounded_input_value() -> None:
    source, _ = _source(_ScopedConnection())

    with pytest.raises(ValueError, match="不允许的字符"):
        source.site_ids_by_shops(("SHOP'--",), TENANT)


def test_scoped_query_does_not_fallback_to_unfiltered_when_scope_fails() -> None:
    connection = _ScopedConnection()
    source, conn = _source(connection, QueryScope(tenant_id=TENANT, site_ids=SITES, user_id=None))

    source.get_orders("O-1")

    assert all("tenant_id=%s" in sql for sql, _ in conn.executed if "SELECT" in sql)
