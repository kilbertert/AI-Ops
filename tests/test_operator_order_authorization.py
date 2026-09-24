"""订单授权判定接入运营商维度（#426，PRD #423）。

可见性 = 租户匹配 AND 站点在调用者的运营商站点集合内。判定的对外可观察行为是
「某身份查某订单：能查 / 被拒绝」，因此断言集中在三处，都不测内部调用顺序：

1. **会话身份解析出的** ``ScopeContext`` **与** ``resolve_query_scope(context)``
   —— 它是订单授权判定与证据收集共同的范围来源（#426「替换 self」的落点）；
2. **``ScopedOrderAuthorizer.can_access``** —— PRD 指定的最高接缝：一个函数、
   一个布尔结论，用假 MySQL 连接驱动；
3. **助手入口** —— 显式指名订单 → 202/404，问句内嵌订单号 → 静默回落
   （既有契约的对照组，用真实授权判定而不是替身）。

链路四跳全部以既有接缝驱动：会话 C 端 id → C→B 映射（#424）→ 店铺集合
（``/shopuser/getShops``，#425）→ 站点集合（``ch_site.shop_id → ch_site.id``，#425）。
真实数据特征用于构造替身：954 行站点中 ``ch_site.id ≡ ch_site.shop_id`` 的 953 行、
1 行不同；310 个代理商账号中只有 131 个有店铺绑定。见各测试注释。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import ScopedOrderAuthorizer
from aiops_diagnostics.config import Settings
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.query_scope import (
    QueryScope,
    ScopeError,
    resolve_operator_site_scope,
    resolve_query_scope,
    static_site_mapper,
)
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeContext,
    SubjectRecord,
)
from aiops_diagnostics.sources import SourceError
from aiops_diagnostics.third_session_auth import (
    RedisThirdSessionResolver,
    ThirdSessionSettings,
    UpmsOperatorSiteScope,
)

TENANT = "T-1"
C_USER_ID = "C-1"
B_USER_ID = "B-9"
INSIDE_CREDENTIAL = "upms-internal-token"
UPMS_BASE_URL = "https://upms.example.test"
SESSION_TOKEN = "session-123456789012345"

SITE_IN = "SITE-IN-1"  # 运营商站点集合内
SITE_OUT = "SITE-OUT-2"  # 集合外

#: 四条订单：集合内但挂在别人名下、集合外但只挂在本人名下、本人的两条单。
ORDER_INSIDE = "2096164064667852801"
ORDER_OUTSIDE = "2096164064667852802"
ORDER_OWN_IN = "2096164064667852803"
ORDER_MISSING = "9999999999999999999"

ORDERS = (
    {"order_no": ORDER_INSIDE, "tenant_id": TENANT, "site_id": SITE_IN, "user_id": "C-OTHER"},
    {"order_no": ORDER_OUTSIDE, "tenant_id": TENANT, "site_id": SITE_OUT, "user_id": C_USER_ID},
    {"order_no": ORDER_OWN_IN, "tenant_id": TENANT, "site_id": SITE_IN, "user_id": C_USER_ID},
)


# --- 会话替身：Redis 会话值 + C→B 映射 -------------------------------------


class _Redis:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def get(self, key: str):
        assert key.startswith("app:3rd_session:")
        return self.value


def _java_session(payload: dict[str, Any]) -> bytes:
    """A Redis value as the Java BFF stores it: header + embedded JSON string."""
    return b"\xac\xed\x00\x05t\x00" + json.dumps(payload).encode()


def _redis_session(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None) -> None:
    value = _java_session(payload or {"userId": C_USER_ID, "tenantId": TENANT})
    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: _Redis(value))


def _session_settings() -> ThirdSessionSettings:
    return ThirdSessionSettings(
        "127.0.0.1", 6379, 0, password="secret", service_token="svc", key_prefix="app:3rd_session:"
    )


class _BSubjectDirectory:
    """In-memory C→B mapping directory recording every lookup."""

    def __init__(self, records: tuple[SubjectRecord, ...] = ()) -> None:
        self.records = records
        self.calls: list[str] = []

    def users_by_c_user_id(self, c_user_id: str) -> tuple[SubjectRecord, ...]:
        self.calls.append(c_user_id)
        return self.records


def _b_subject(**overrides: Any) -> SubjectRecord:
    fields: dict[str, Any] = {"b_user_id": B_USER_ID, "c_user_id": C_USER_ID, "tenant_id": TENANT}
    fields.update(overrides)
    return SubjectRecord(**fields)


class _FakeShops:
    """内存店铺归属目录（#425 的 ``ShopDirectory`` 接缝）。"""

    def __init__(self, shop_ids: tuple[str, ...] = (), *, error: Exception | None = None) -> None:
        self.shop_ids = shop_ids
        self.error = error
        self.calls: list[str] = []

    def shop_ids_by_b_user_id(self, b_user_id: str) -> tuple[str, ...]:
        self.calls.append(b_user_id)
        if self.error is not None:
            raise self.error
        return self.shop_ids


class _OperatorScope:
    """会话身份的运营商站点范围替身：内部走 #425 的真实解析函数。

    只替换两处 I/O（UPMS 店铺集合、充电库站点归属），保留四跳链里唯一有判断的
    那一跳，因此这些用例同时是 #425 解析函数的回归。
    """

    def __init__(
        self,
        shop_ids: tuple[str, ...] = ("SHOP-1",),
        sites: dict[str, tuple[str, ...]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.shops = _FakeShops(shop_ids, error=error)
        self.sites = sites or {}
        self.calls: list[tuple[str, str]] = []

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str) -> QueryScope:
        self.calls.append((b_user_id, tenant_id))
        return resolve_operator_site_scope(
            b_user_id,
            tenant_id,
            shops=self.shops,
            mapper=static_site_mapper(sites_by_shop=self.sites),
        )


def _session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: tuple[SubjectRecord, ...] = (_b_subject(),),
    operator_scope: _OperatorScope | None = None,
) -> ScopeContext:
    """resolve one third-session caller, with the operator directories faked."""
    _redis_session(monkeypatch)
    resolver = RedisThirdSessionResolver(
        _session_settings(),
        b_subject_directory=_BSubjectDirectory(records),
        operator_scope=operator_scope if operator_scope is not None else _OperatorScope(),
    )
    return resolver.resolve("svc", required_scope="aiops:diagnoses:write", third_session=SESSION_TOKEN)


def _operator_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shop_ids: tuple[str, ...] = ("SHOP-1",),
    sites: dict[str, tuple[str, ...]] | None = None,
    error: Exception | None = None,
) -> tuple[ScopeContext, _OperatorScope]:
    scope = _OperatorScope(shop_ids, sites, error=error)
    return _session(monkeypatch, operator_scope=scope), scope


def _consumer_session(monkeypatch: pytest.MonkeyPatch) -> ScopeContext:
    """A C-side caller with no B 端 account: the normal consumer state."""
    return _session(monkeypatch, records=())


# --- 假 MySQL：渲染出的 WHERE 的行级镜像 -------------------------------------


class _Connection:
    """按渲染出的 WHERE 选行的假 MySQL 连接。

    只做范围谓词的行级镜像（租户/站点/用户），不参与任何 SQL 文本断言——断言
    的对象是「能查 / 被拒绝」。
    """

    def __init__(
        self,
        *,
        orders: tuple[dict[str, Any], ...] = ORDERS,
        sites: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.orders = list(orders)
        self.sites = list(sites)
        self.executed: list[tuple[str, list[Any]]] = []

    def cursor(self) -> _Connection:
        return self

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, list(params or [])))

    def fetchall(self) -> list[dict[str, Any]]:
        sql, params = self.executed[-1]
        if "ch_site" in sql:
            tenant_id = str(params[0])
            wanted = {str(value) for value in params[1:]}
            return [
                row
                for row in self.sites
                if str(row.get("tenant_id")) == tenant_id and str(row.get("shop_id")) in wanted
            ]
        where = sql.split(" WHERE ", 1)[1].rsplit(" ORDER BY ", 1)[0]
        if "1=0" in where:
            return []
        rows = list(self.orders)
        remaining = list(params)
        for fragment in where.split(" AND "):
            if fragment.startswith("site_id IN ("):
                sites = {str(remaining.pop(0)) for _ in range(fragment.count("%s"))}
                rows = [row for row in rows if str(row.get("site_id")) in sites]
                continue
            value = str(remaining.pop(0))
            if fragment == "order_no=%s":
                rows = [row for row in rows if str(row.get("order_no")) == value]
            elif fragment == "tenant_id=%s":
                rows = [row for row in rows if str(row.get("tenant_id")) == value]
            elif fragment == "user_id=%s":
                rows = [row for row in rows if str(row.get("user_id")) == value]
            else:
                raise AssertionError(f"unexpected scope fragment: {fragment}")
        return rows

    def fetchone(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _authorizer(monkeypatch: pytest.MonkeyPatch, connection: _Connection) -> ScopedOrderAuthorizer:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.ssh.enabled = False
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)
    return ScopedOrderAuthorizer(settings)


def _context(data_scope: DataScope, *, c_user_id: str | None = C_USER_ID) -> ScopeContext:
    subject = SubjectRecord(b_user_id=B_USER_ID, c_user_id=c_user_id, tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=data_scope,
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def _operator_context(sites: tuple[str, ...]) -> ScopeContext:
    return _context(DataScope(type=SCOPE_TYPE_ORGAN, site_ids=sites))


def _order_queries(connection: _Connection) -> list[tuple[str, list[Any]]]:
    return [item for item in connection.executed if "ch_order_info" in item[0]]


# --- 1. 会话身份：self 被替换为运营商站点集合 ---------------------------------


def test_operator_session_scope_is_the_operators_site_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """管家端会话：可见范围 = 租户 ∩ 运营商站点集合，``user_id`` 过滤不再参与。"""
    context, scope = _operator_session(
        monkeypatch,
        shop_ids=("SHOP-1", "SHOP-2"),
        sites={"SHOP-1": (SITE_IN,), "SHOP-2": ("SITE-IN-2",)},
    )

    assert context.data_scope == DataScope(type=SCOPE_TYPE_ORGAN, site_ids=("SITE-IN-1", "SITE-IN-2"))
    assert context.delegated is False
    assert resolve_query_scope(context) == QueryScope(
        tenant_id=TENANT, site_ids=("SITE-IN-1", "SITE-IN-2"), user_id=None
    )
    # 运营商范围由 B 端主体的授权数据定义，不由碰巧绑定的 C 端账号定义。
    assert scope.calls == [(B_USER_ID, TENANT)]
    assert scope.shops.calls == [B_USER_ID]


def test_shop_id_is_never_used_as_site_id_in_the_session_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """例外行：店铺 ``SHOP-2990`` 名下站点是 ``SITE-7081``（954 行中 1 行不同）。"""
    context, _ = _operator_session(
        monkeypatch, shop_ids=("SHOP-2990",), sites={"SHOP-2990": ("SITE-7081",)}
    )

    assert context.data_scope.site_ids == ("SITE-7081",)
    assert "SHOP-2990" not in context.data_scope.site_ids


def test_consumer_session_keeps_the_self_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 B 端账号的会话是消费者端的正常状态：self 范围逐字不变。"""
    context = _consumer_session(monkeypatch)

    assert context.subject.b_user_id == f"c:{C_USER_ID}"
    assert context.data_scope == DataScope(type=SCOPE_TYPE_SELF)
    assert resolve_query_scope(context) == QueryScope(tenant_id=TENANT, site_ids=None, user_id=C_USER_ID)


def test_session_path_never_enters_the_delegated_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """陷阱 3：范围类型一改就会触发代查分支（未配 Dis 报错、配了与 Dis 取交集）。

    ``delegated=False`` 让会话路径不进入该分支：消费者会话仍按 C 端用户过滤、
    管家端会话拿到完整运营商站点集合，两者都不需要 Dis。
    """
    consumer = _consumer_session(monkeypatch)
    operator, _ = _operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})

    assert consumer.delegated is False
    assert operator.delegated is False
    # 未注入 Dis 也不会抛 scope.dis_config_missing。
    assert resolve_query_scope(consumer).user_id == C_USER_ID
    assert resolve_query_scope(operator) == QueryScope(tenant_id=TENANT, site_ids=(SITE_IN,), user_id=None)


def test_unresolvable_c_to_b_mapping_keeps_the_consumer_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到唯一 B 端主体时不猜：仍按 self 限定（消费者端），不是空集合、也不是放行。"""
    context = _session(monkeypatch, records=(_b_subject(), _b_subject(b_user_id="B-10")))

    assert context.subject.b_subject_reason
    assert context.data_scope == DataScope(type=SCOPE_TYPE_SELF)


def test_operator_scope_is_not_resolved_without_a_b_side_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """占位 ``b_user_id`` 绝不能传进店铺查询（id 空间不同，见 #425 陷阱 1）。"""
    scope = _OperatorScope(shop_ids=("SHOP-1",), sites={"SHOP-1": (SITE_IN,)})

    context = _session(monkeypatch, records=(), operator_scope=scope)

    assert scope.calls == []
    assert context.data_scope == DataScope(type=SCOPE_TYPE_SELF)


def test_the_operator_site_set_enters_the_scope_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """范围指纹已把数据范围纳入：站点集合变化即指纹变化（权限变化使旧绑定失效）。"""
    first, _ = _operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})
    second, _ = _operator_session(monkeypatch, sites={"SHOP-1": (SITE_OUT,)})

    assert first.scope_fingerprint != second.scope_fingerprint


# --- 2. 订单授权判定（最高接缝）----------------------------------------------


def test_order_at_an_operator_site_is_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    """站点在运营商站点集合内 → 能查，包括挂在别人名下的订单。"""
    authorizer = _authorizer(monkeypatch, _Connection())

    assert authorizer.can_access(_operator_context((SITE_IN,)), ORDER_INSIDE) is True


def test_order_outside_the_operators_sites_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """站点不在集合内 → 拒绝，即使它是本人的订单。"""
    authorizer = _authorizer(monkeypatch, _Connection())

    assert authorizer.can_access(_operator_context((SITE_IN,)), ORDER_OUTSIDE) is False


def test_own_order_outside_the_operators_sites_is_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """反例：只挂在本人名下、站点在集合外的订单不可见——替换，不是与 self 叠加。"""
    authorizer = _authorizer(monkeypatch, _Connection())
    own_outside = next(row for row in ORDERS if row["order_no"] == ORDER_OUTSIDE)
    assert own_outside["user_id"] == C_USER_ID

    assert authorizer.can_access(_operator_context((SITE_IN,)), ORDER_OUTSIDE) is False


def test_refusal_does_not_distinguish_missing_from_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝不区分「订单不存在」与「无权查看」：订单号不能被用来探测他人业务。"""
    authorizer = _authorizer(monkeypatch, _Connection())
    context = _operator_context((SITE_IN,))

    assert authorizer.can_access(context, ORDER_OUTSIDE) is False
    assert authorizer.can_access(context, ORDER_MISSING) is False


def test_operator_without_shop_binding_sees_no_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """未绑定店铺 → 空集合 → 拒绝，不回落为租户级放行，且不发订单查询。"""
    connection = _Connection()
    authorizer = _authorizer(monkeypatch, connection)

    assert authorizer.can_access(_operator_context(()), ORDER_INSIDE) is False
    assert _order_queries(connection) == []


def test_consumer_scope_still_filters_by_the_callers_own_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """消费者端零回归：self 范围仍只按 C 端用户过滤，站点不参与。"""
    authorizer = _authorizer(monkeypatch, _Connection())
    context = _context(DataScope(type=SCOPE_TYPE_SELF))

    assert authorizer.can_access(context, ORDER_OWN_IN) is True
    assert authorizer.can_access(context, ORDER_INSIDE) is False


def test_operator_session_reaches_an_order_placed_by_another_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：会话解析 → 运营商站点集合 → 订单查询；站点在集合内即能查。"""
    context, _ = _operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})
    authorizer = _authorizer(monkeypatch, _Connection())
    placed_by_another = next(row for row in ORDERS if row["order_no"] == ORDER_INSIDE)
    assert placed_by_another["user_id"] == "C-OTHER"

    assert authorizer.can_access(context, ORDER_INSIDE) is True


# --- 3. 失败关闭与可区分记录 -------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        # 上游不可达 / 响应形状非法 / 数量超界（#425 的 ScopeError）
        ScopeError("UPMS 请求失败", code=SCOPE_ERROR_UPMS_UNAVAILABLE),
        # 充电库不可用、范围 ID 不可用：链路自己的逃逸方式
        SourceError("MySQL 连接失败: OperationalError"),
        ValueError("范围 ID 包含不允许的字符"),
    ],
)
def test_every_operator_scope_failure_denies_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """链路上每一种逃逸方式都失败关闭为拒绝，不会把授权故障变成 500。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.third_session_auth"):
        context, _ = _operator_session(monkeypatch, error=error)
    authorizer = _authorizer(monkeypatch, _Connection())

    assert context.data_scope == DataScope(type=SCOPE_TYPE_ORGAN, site_ids=())
    assert authorizer.can_access(context, ORDER_INSIDE) is False
    # 与「站点不在集合内」不是同一类原因：日志带错误码或异常类型。
    assert "operator_scope_unavailable" in caplog.text
    assert (getattr(error, "code", None) or type(error).__name__) in caplog.text
    for identifier in (C_USER_ID, B_USER_ID, TENANT, INSIDE_CREDENTIAL, "svc"):
        assert identifier not in caplog.text


def test_the_two_empty_scope_causes_are_logged_apart(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """两种成因相反的空集合分开记录：#425 的 no_shop_binding / shop_without_site。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        _operator_session(monkeypatch, shop_ids=())
        caplog.clear()
        _operator_session(monkeypatch, shop_ids=("SHOP-1",), sites={})

    assert "reason=shop_without_site" in caplog.text
    assert "reason=no_shop_binding" not in caplog.text
    assert B_USER_ID not in caplog.text
    assert TENANT not in caplog.text


def test_unbound_operator_account_is_logged_as_a_missing_binding(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """账号漏登记（应补绑定）是它自己的原因，与「站点不在集合内」不能混。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        _operator_session(monkeypatch, shop_ids=())

    assert "reason=no_shop_binding" in caplog.text


# --- 4. 生产实现：同一事实来源 ----------------------------------------------


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _FakeUpmsTransport:
    """Serve the documented ``/shopuser/getShops`` contract."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: Any, timeout: int | None = None) -> _FakeResponse:
        split = urlsplit(request.full_url)
        path = f"{split.path}?{split.query}" if split.query else split.path
        self.requests.append((path, dict(request.header_items())))
        return _FakeResponse({"code": 0, "msg": "ok", "data": self.payload})


def _upms_settings() -> Settings:
    settings = Settings.from_env()
    settings.upms.base_url = UPMS_BASE_URL
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.ssh.enabled = False
    return settings


def test_upms_operator_site_scope_uses_the_backend_authorization_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """店铺集合取 ``/shopuser/getShops``（后端权威授权同一端点），站点经 ``ch_site`` 映射。"""
    transport = _FakeUpmsTransport(["SHOP-1"])
    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", transport)
    connection = _Connection(sites=({"id": SITE_IN, "tenant_id": TENANT, "shop_id": "SHOP-1"},))
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)

    scope = UpmsOperatorSiteScope(_upms_settings(), INSIDE_CREDENTIAL).site_scope_by_b_user_id(
        B_USER_ID, TENANT
    )

    assert scope == QueryScope(tenant_id=TENANT, site_ids=(SITE_IN,), user_id=None)
    path, headers = transport.requests[-1]
    assert path == f"/shopuser/getShops?userId={B_USER_ID}"
    assert headers["Authorization"] == f"Bearer {INSIDE_CREDENTIAL}"


def test_upms_operator_site_scope_reads_the_site_mapping_inside_the_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """站点归属映射与受限直连共用同一条跳板隧道：不为授权另开一条连接路径。"""
    tunnel_calls: list[bool] = []

    class _Tunnel:
        def __init__(self, settings: Settings, *, include_direct_backends: bool = True) -> None:
            self.settings = settings
            tunnel_calls.append(include_direct_backends)

        def __enter__(self) -> Settings:
            return self.settings

        def __exit__(self, *_args: Any) -> bool:
            return False

    transport = _FakeUpmsTransport(["SHOP-1"])
    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", transport)
    monkeypatch.setattr("aiops_diagnostics.sources._ssh_tunnel", _Tunnel)
    monkeypatch.setattr(
        "aiops_diagnostics.sources.pymysql.connect",
        lambda **_: _Connection(sites=({"id": SITE_IN, "tenant_id": TENANT, "shop_id": "SHOP-1"},)),
    )

    scope = UpmsOperatorSiteScope(_upms_settings(), INSIDE_CREDENTIAL).site_scope_by_b_user_id(
        B_USER_ID, TENANT
    )

    assert scope.site_ids == (SITE_IN,)
    assert tunnel_calls == [True]


# --- 5. 助手入口：显式指名 vs 静默回落（既有契约的对照组）---------------------


class _AssistantRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def shutdown(self) -> None:
        return None

    def start_standard_diagnosis(
        self, context: ScopeContext, order_no: str, question: str, indicator_code, language="zh"
    ):
        del context, indicator_code
        self.calls.append((order_no, question))
        return {
            "diagnosis_id": "dx_test000000000000000000000000000001",
            "order_no": order_no,
            "question": question,
            "indicator_code": None,
            "status": "queued",
            "result": None,
            "error_code": None,
            "error_message": None,
            "created_at": "2026-09-07T00:00:00+00:00",
            "updated_at": "2026-09-07T00:00:00+00:00",
            "completed_at": None,
        }

    def get_standard_diagnosis(self, context: ScopeContext, diagnosis_id: str):
        del context, diagnosis_id
        return None

    classified = None

    def classify_lightweight(self, question: str, *, language: str = "zh", tenant_id: str | None = None):
        del question, language, tenant_id
        return self.classified

    def start_assistant_qa(self, context: ScopeContext, question: str, **kwargs: Any):
        del context, kwargs
        return {
            "qa_id": "qa_test00000000000000000000000000000001",
            "question": question,
            "status": "queued",
            "result": None,
        }

    def get_assistant_qa(self, context: ScopeContext, qa_id: str):
        del context, qa_id
        return None

    def list_assistant_qa(self, context: ScopeContext, *, limit: int = 50):
        del context, limit
        return []


class _OperatorCaller:
    """A resolver that answers with an operator-scoped session identity (#426)."""

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del token, third_session
        return _operator_context((SITE_IN,))


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord(B_USER_ID, c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def _assistant_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connection: _Connection
) -> tuple[TestClient, _AssistantRuntime]:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    runtime = _AssistantRuntime()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_OperatorCaller(),
        order_authorizer=_authorizer(monkeypatch, connection),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), runtime


def _operator_headers() -> dict[str, str]:
    return {"Authorization": "Bearer service", "X-Business-Entry": "operator"}


def test_assistant_explicit_order_inside_the_operator_scope_starts_a_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指名集合内订单 → 202 诊断（管家端替同事/站点处理问题的正路径）。"""
    client, runtime = _assistant_client(tmp_path, monkeypatch, _Connection())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "你好", "order_no": ORDER_INSIDE},
        headers=_operator_headers(),
    )

    assert resp.status_code == 202
    assert resp.json()["type"] == "diagnosis"
    assert runtime.calls == [(ORDER_INSIDE, "你好")]


def test_assistant_explicit_order_outside_the_operator_scope_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指名集合外订单 → 明确 404，不改写为普通问答、不泄漏订单是否存在。"""
    client, runtime = _assistant_client(tmp_path, monkeypatch, _Connection())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "你好", "order_no": ORDER_OUTSIDE},
        headers=_operator_headers(),
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert runtime.calls == []


def test_assistant_embedded_order_outside_the_operator_scope_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """问句内嵌集合外订单号 → 静默回落（既有契约），不是 404。"""
    client, runtime = _assistant_client(tmp_path, monkeypatch, _Connection())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": f"订单 {ORDER_OUTSIDE} 怎么还没退款"},
        headers=_operator_headers(),
    )

    assert resp.status_code == 202
    assert resp.json()["type"] == "qa"
    assert runtime.calls == []
