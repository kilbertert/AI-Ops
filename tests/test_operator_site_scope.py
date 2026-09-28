"""运营商站点范围解析的契约测试（#425，PRD #423）。

接缝是 ``resolve_operator_site_scope(b_user_id, tenant_id, shops=..., mapper=...)``：
给定一个 **B 端** ``SysUser.id``，经后端权威授权所用的同一个端点（``/shopuser/getShops``）
取店铺集合，再经既有站点归属映射（``ch_site.shop_id → ch_site.id``）取站点集合。

本层只读、**不接入任何查询路径**，因此这里的断言只谈两件事：产出的范围值，
以及任何解析失败都必须 fail closed。真实生产数据特征（954 行站点中
``ch_site.id ≡ ch_site.shop_id`` 的有 953 行、1 行不同；310 个代理商账号中 131 个
有店铺绑定）用于构造替身，见各测试的注释。

替身（内存店铺目录、UPMS 传输、行级镜像的假 MySQL）与 #426/#427/#428 共用
``operator_support.py``：各写一份会各自漂移。
"""

from __future__ import annotations

import logging

import pytest
from operator_support import (
    TENANT,
    Connection,
    FakeShops,
    FakeUpmsTransport,
    mysql_settings,
    patch_transport,
)

from aiops_diagnostics.config import UpmsSettings
from aiops_diagnostics.query_scope import (
    MAX_SCOPE_IDS,
    OPERATOR_SCOPE_NO_SHOP_BINDING,
    OPERATOR_SCOPE_SHOP_WITHOUT_SITE,
    QueryScope,
    ScopeError,
    UpmsShopDirectory,
    resolve_operator_site_scope,
    static_site_mapper,
)
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    SHOP_USER_PATH,
)
from aiops_diagnostics.sources import SITE_SCOPE_MAX_ROWS, MySQLSource

B_USER_ID = "B-OPERATOR-9"
INSIDE_CREDENTIAL = "upms-internal-token"
UPMS_BASE_URL = "https://upms.example.test"


def _resolve(
    shop_ids: tuple[str, ...] = ("SHOP-1",),
    sites: dict[str, tuple[str, ...]] | None = None,
    *,
    error: Exception | None = None,
    b_user_id: str = B_USER_ID,
) -> tuple[QueryScope, FakeShops]:
    shops = FakeShops(shop_ids, error=error)
    scope = resolve_operator_site_scope(
        b_user_id,
        TENANT,
        shops=shops,
        mapper=static_site_mapper(sites_by_shop=sites or {}),
    )
    return scope, shops


# --- 正常路径 ---------------------------------------------------------------


def test_b_operator_resolves_to_its_operator_site_set() -> None:
    """B 端 id → 店铺集合 → 站点集合；self 用户过滤不参与（替换而非取交集）。"""
    scope, shops = _resolve(
        ("SHOP-1", "SHOP-2"),
        {"SHOP-1": ("SITE-9081",), "SHOP-2": ("SITE-9082", "SITE-9083")},
    )

    assert scope == QueryScope(
        tenant_id=TENANT, site_ids=("SITE-9081", "SITE-9082", "SITE-9083"), user_id=None
    )
    assert scope.user_id is None  # 运营商范围不由碰巧绑定的 C 端账号定义
    assert not scope.empty_site_scope
    assert shops.calls == [B_USER_ID]


def test_shop_id_is_never_used_as_site_id() -> None:
    """例外行：店铺 ``SHOP-2990`` 名下站点是 ``SITE-7081``（真实数据 954 行中 1 行不同）。

    若把店铺 id 直接当站点 id，结果会是 ``("SHOP-2990",)`` —— 在另外 953 行上恰好
    正确，所以这个错误只能靠例外行暴露，必须走既有站点归属映射。
    """
    scope, _ = _resolve(("SHOP-2990",), {"SHOP-2990": ("SITE-7081",)})

    assert scope.site_ids == ("SITE-7081",)
    assert "SHOP-2990" not in scope.site_ids


def test_sites_come_from_the_charging_site_mapping_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端走映射：店铺集合经 ``ch_site.shop_id → ch_site.id`` 取站点，含例外行。

    假 MySQL 只做**范围谓词的行级镜像**（租户 + ``shop_id`` 集合），断言的对象是
    产出的站点集合，不参与 SQL 文本断言——因此谓词渲染方式的无害变化不会让用例
    变脆，而「拿店铺 id 当站点 id」会直接转红。954 行里两列同值 953 行、不同 1 行，
    这里两种情形各给一行。
    """
    connection = Connection(
        sites=(
            {"id": "SHOP-1", "tenant_id": TENANT, "shop_id": "SHOP-1"},  # 953 行：两列同值
            {"id": "SITE-7081", "tenant_id": TENANT, "shop_id": "E"},  # 1 行：两列不同
        )
    )
    source = _site_mapper(monkeypatch, connection)

    scope = resolve_operator_site_scope(
        B_USER_ID,
        TENANT,
        shops=FakeShops(("SHOP-1", "E")),
        mapper=source,
    )

    assert scope.site_ids == ("SHOP-1", "SITE-7081")  # 同值行与例外行都经映射取得


def test_site_count_over_the_mapper_limit_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """映射查询自己守住上限：超过 LIMIT 时抛 ``scope.too_large``，而不是静默截断。

    ``SITE_SCOPE_MAX_ROWS`` 与 ``MAX_SCOPE_IDS`` 相等，因此「正好取到这么多行」
    既可能是完整集合、也可能是被截断的集合——上限防线落在查询里，才不会被
    静悄悄绕开（静态替身没有 LIMIT，这一条只有真实 ``MySQLSource`` 能证明）。
    """
    over_limit = tuple(
        {"id": f"SITE-{index}", "tenant_id": TENANT, "shop_id": "SHOP-1"}
        for index in range(SITE_SCOPE_MAX_ROWS + 1)
    )
    source = _site_mapper(monkeypatch, Connection(sites=over_limit))

    with pytest.raises(ScopeError) as excinfo:
        source.site_ids_by_shops(("SHOP-1",), TENANT)

    assert excinfo.value.code == "scope.too_large"


# --- 未绑定店铺 -------------------------------------------------------------


def test_operator_without_shop_binding_resolves_to_empty_scope() -> None:
    """未绑定店铺的用户 → 空集合（短路的范围），不是「全部」，也不是报错。"""
    scope, shops = _resolve((), {})

    assert scope.site_ids == ()
    assert scope.empty_site_scope
    assert shops.calls == [B_USER_ID]


def test_operator_without_shop_binding_differs_from_an_unrestricted_scope() -> None:
    scope, _ = _resolve((), {})

    assert scope.site_ids is not None  # None 才是租户内不限站点


def test_unbound_shop_binding_is_logged_with_a_distinguishable_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """空集合必须可与「站点本就无代理商」区分，供运营补登记绑定。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        _resolve((), {})

    assert f"reason={OPERATOR_SCOPE_NO_SHOP_BINDING}" in caplog.text
    assert B_USER_ID not in caplog.text
    assert TENANT not in caplog.text


def test_bound_shop_without_site_is_logged_as_a_different_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """绑了店铺但站点表里查不到：与「未绑定店铺」是两种成因。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        _resolve(("SHOP-1",), {})

    assert f"reason={OPERATOR_SCOPE_SHOP_WITHOUT_SITE}" in caplog.text
    assert f"reason={OPERATOR_SCOPE_NO_SHOP_BINDING}" not in caplog.text


# --- fail closed ------------------------------------------------------------


class _UnexpectedMapper:
    """一旦被调用就失败的站点归属替身：证明超界在映射之前就已失败关闭。"""

    def site_ids_by_shops(self, shop_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        raise AssertionError("店铺数量超界时必须先失败关闭，不得进入站点归属映射")

    def site_ids_by_points(self, point_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        raise AssertionError("运营商范围不走 Dis 点位归属")


def test_shop_count_over_limit_fails_closed_before_the_site_mapping() -> None:
    shops = FakeShops(tuple(f"SHOP-{index}" for index in range(MAX_SCOPE_IDS + 1)))

    with pytest.raises(ScopeError) as excinfo:
        resolve_operator_site_scope(B_USER_ID, TENANT, shops=shops, mapper=_UnexpectedMapper())

    assert excinfo.value.code == "scope.too_large"
    assert shops.calls == [B_USER_ID]


def test_site_count_over_limit_fails_closed() -> None:
    sites = {f"SHOP-{index}": (f"SITE-{index}",) for index in range(MAX_SCOPE_IDS + 1)}

    with pytest.raises(ScopeError) as excinfo:
        resolve_operator_site_scope(
            B_USER_ID,
            TENANT,
            shops=FakeShops(tuple(sites)),
            mapper=static_site_mapper(sites_by_shop=sites),
        )

    assert excinfo.value.code == "scope.too_large"


def test_unreachable_upstream_fails_closed() -> None:
    with pytest.raises(ScopeError) as excinfo:
        _resolve(
            ("SHOP-1",),
            {"SHOP-1": ("SITE-1",)},
            error=ScopeError("UPMS 请求失败", code=SCOPE_ERROR_UPMS_UNAVAILABLE),
        )

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


# --- 既有来源：店铺集合取后端权威授权所用的同一个接口 -----------------------


def _upms_shops(payload) -> tuple[UpmsShopDirectory, FakeUpmsTransport]:
    transport = FakeUpmsTransport(payload)
    directory = UpmsShopDirectory(UpmsSettings(base_url=UPMS_BASE_URL, timeout_seconds=5), INSIDE_CREDENTIAL)
    return directory, transport


def _site_mapper(monkeypatch: pytest.MonkeyPatch, connection: Connection) -> MySQLSource:
    """站点归属查询走真实 ``MySQLSource``，只替换连接这一处 I/O。"""
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)
    return MySQLSource(mysql_settings(), scope=None)


def test_shops_come_from_the_endpoint_the_backend_authorization_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不新增服务端接口：店铺集合取 ``/shopuser/getShops``（``@ShopDataScope`` 的来源）。"""
    directory, transport = _upms_shops(["SHOP-1", "SHOP-2"])
    patch_transport(monkeypatch, transport)

    scope = resolve_operator_site_scope(
        B_USER_ID,
        TENANT,
        shops=directory,
        mapper=static_site_mapper(sites_by_shop={"SHOP-1": ("SITE-1",), "SHOP-2": ("SITE-2",)}),
    )

    assert scope.site_ids == ("SITE-1", "SITE-2")
    path, headers = transport.requests[-1]
    assert path == f"{SHOP_USER_PATH}?userId={B_USER_ID}"
    assert headers["Authorization"] == f"Bearer {INSIDE_CREDENTIAL}"


def test_b_side_id_is_passed_through_without_rewriting(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点要 B 端 ``sys_user.id``；会话的 C 端 id 在这里不参与。"""
    directory, transport = _upms_shops(["SHOP-1"])
    patch_transport(monkeypatch, transport)

    directory.shop_ids_by_b_user_id("B-OTHER-42")

    path, _ = transport.requests[-1]
    assert path == f"{SHOP_USER_PATH}?userId=B-OTHER-42"


def test_shop_response_shape_must_be_readable_or_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    directory, transport = _upms_shops({"unexpected": "shape"})
    patch_transport(monkeypatch, transport)

    with pytest.raises(ScopeError) as excinfo:
        resolve_operator_site_scope(B_USER_ID, TENANT, shops=directory, mapper=static_site_mapper())

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


def test_credential_never_reaches_the_resolved_scope_or_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    directory, transport = _upms_shops([])
    patch_transport(monkeypatch, transport)

    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        scope = resolve_operator_site_scope(B_USER_ID, TENANT, shops=directory, mapper=static_site_mapper())

    assert scope == QueryScope(tenant_id=TENANT, site_ids=(), user_id=None)
    assert INSIDE_CREDENTIAL not in caplog.text
    assert INSIDE_CREDENTIAL not in repr(scope)
