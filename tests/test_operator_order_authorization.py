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
替身集中在 ``operator_support.py``。真实数据特征用于构造替身：954 行站点中
``ch_site.id ≡ ch_site.shop_id`` 的 953 行、1 行不同；310 个代理商账号中只有
131 个有店铺绑定。见各测试注释。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from operator_support import (
    B_USER_ID,
    C_USER_ID,
    INSIDE_CREDENTIAL,
    ORDER_INSIDE,
    ORDER_MISSING,
    ORDER_OUTSIDE,
    ORDER_OWN_IN,
    ORDERS,
    SITE_IN,
    SITE_OUT,
    TENANT,
    Caller,
    Connection,
    FakeUpmsTransport,
    OperatorScope,
    assistant_app,
    b_subject,
    build_authorizer,
    consumer_context,
    consumer_session,
    operator_context,
    operator_session,
    order_queries,
    patch_transport,
    resolve_session,
    upms_settings,
)

from aiops_diagnostics.query_scope import QueryScope, resolve_query_scope
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeError,
)
from aiops_diagnostics.sources import SourceError
from aiops_diagnostics.third_session_auth import UpmsOperatorSiteScope

_OPERATOR_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "operator"}


# --- 1. 会话身份：self 被替换为运营商站点集合 ---------------------------------


def test_operator_session_scope_is_the_operators_site_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """管家端会话：可见范围 = 租户 ∩ 运营商站点集合，``user_id`` 过滤不再参与。"""
    context, scope = operator_session(
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
    context, _ = operator_session(monkeypatch, shop_ids=("SHOP-2990",), sites={"SHOP-2990": ("SITE-7081",)})

    assert context.data_scope.site_ids == ("SITE-7081",)
    assert "SHOP-2990" not in context.data_scope.site_ids


def test_consumer_session_keeps_the_self_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 B 端账号的会话是消费者端的正常状态：self 范围逐字不变。"""
    context = consumer_session(monkeypatch)

    assert context.subject.b_user_id == f"c:{C_USER_ID}"
    assert context.data_scope == DataScope(type=SCOPE_TYPE_SELF)
    assert resolve_query_scope(context) == QueryScope(tenant_id=TENANT, site_ids=None, user_id=C_USER_ID)


def test_session_path_never_enters_the_delegated_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """陷阱 3：范围类型一改就会触发代查分支（未配 Dis 报错、配了与 Dis 取交集）。

    ``delegated=False`` 让会话路径不进入该分支：消费者会话仍按 C 端用户过滤、
    管家端会话拿到完整运营商站点集合，两者都不需要 Dis。
    """
    consumer = consumer_session(monkeypatch)
    operator, _ = operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})

    assert consumer.delegated is False
    assert operator.delegated is False
    # 未注入 Dis 也不会抛 scope.dis_config_missing。
    assert resolve_query_scope(consumer).user_id == C_USER_ID
    assert resolve_query_scope(operator) == QueryScope(tenant_id=TENANT, site_ids=(SITE_IN,), user_id=None)


def test_unresolvable_c_to_b_mapping_keeps_the_consumer_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到唯一 B 端主体时不猜：仍按 self 限定（消费者端），不是空集合、也不是放行。"""
    context = resolve_session(monkeypatch, records=(b_subject(), b_subject(b_user_id="B-10")))

    assert context.subject.b_subject_reason
    assert context.data_scope == DataScope(type=SCOPE_TYPE_SELF)


def test_a_record_bound_to_another_c_user_is_not_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    """同租户但绑的是别的 C 端用户：不能改写后采用，否则拿到对方的运营商站点集合。

    错的方向是**放行**（fail open），因此它与「歧义 / 跨租户」同属 identity 核对，
    只是错在哪一列上：这一条错在 ``userId``。
    """
    context = resolve_session(monkeypatch, records=(b_subject(c_user_id="C-OTHER"),))

    assert context.subject.b_user_id == f"c:{C_USER_ID}"
    assert context.subject.b_subject_reason
    assert context.data_scope == DataScope(type=SCOPE_TYPE_SELF)


def test_a_cross_tenant_record_does_not_hide_the_only_same_tenant_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条跨租户记录不能把本租户内唯一的主体判成歧义。

    判定必须先按会话租户筛选、再数本租户内几条：否则跨租户记录出现一次，管家端
    就拿不到可用的 B 端身份——正是 PRD 要修掉的那个形状。
    """
    context = resolve_session(
        monkeypatch,
        records=(b_subject(), b_subject(b_user_id="B-2", c_user_id="C-X", tenant_id="T-OTHER")),
    )

    assert context.subject.b_user_id == B_USER_ID
    assert context.subject.b_subject_reason == ""


def test_operator_scope_is_not_resolved_without_a_b_side_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """占位 ``b_user_id`` 绝不能传进店铺查询（id 空间不同，见 #425 陷阱 1）。"""
    scope = OperatorScope(shop_ids=("SHOP-1",), sites={"SHOP-1": (SITE_IN,)})

    context = resolve_session(monkeypatch, records=(), operator_scope=scope)

    assert scope.calls == []
    assert context.data_scope == DataScope(type=SCOPE_TYPE_SELF)


def test_the_operator_site_set_enters_the_scope_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """范围指纹已把数据范围纳入：站点集合变化即指纹变化（权限变化使旧绑定失效）。"""
    first, _ = operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})
    second, _ = operator_session(monkeypatch, sites={"SHOP-1": (SITE_OUT,)})

    assert first.scope_fingerprint != second.scope_fingerprint


# --- 2. 订单授权判定（最高接缝）----------------------------------------------


def test_order_at_an_operator_site_is_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    """站点在运营商站点集合内 → 能查，包括挂在别人名下的订单。"""
    authorizer = build_authorizer(monkeypatch, Connection())

    assert authorizer.can_access(operator_context((SITE_IN,)), ORDER_INSIDE) is True


def test_order_outside_the_operators_sites_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """站点不在集合内 → 拒绝，即使它是本人的订单。"""
    authorizer = build_authorizer(monkeypatch, Connection())

    assert authorizer.can_access(operator_context((SITE_IN,)), ORDER_OUTSIDE) is False


def test_own_order_outside_the_operators_sites_is_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """反例：只挂在本人名下、站点在集合外的订单不可见——替换，不是与 self 叠加。"""
    own_outside = next(row for row in ORDERS if row["order_no"] == ORDER_OUTSIDE)
    assert own_outside["user_id"] == C_USER_ID
    authorizer = build_authorizer(monkeypatch, Connection())

    assert authorizer.can_access(operator_context((SITE_IN,)), ORDER_OUTSIDE) is False


def test_refusal_does_not_distinguish_missing_from_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝不区分「订单不存在」与「无权查看」：订单号不能被用来探测他人业务。"""
    authorizer = build_authorizer(monkeypatch, Connection())
    context = operator_context((SITE_IN,))

    assert authorizer.can_access(context, ORDER_OUTSIDE) is False
    assert authorizer.can_access(context, ORDER_MISSING) is False


def test_operator_without_shop_binding_sees_no_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """未绑定店铺 → 空集合 → 拒绝，不回落为租户级放行，且不发订单查询。"""
    connection = Connection()
    authorizer = build_authorizer(monkeypatch, connection)

    assert authorizer.can_access(operator_context(()), ORDER_INSIDE) is False
    assert order_queries(connection) == []


def test_consumer_scope_still_filters_by_the_callers_own_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """消费者端零回归：self 范围仍只按 C 端用户过滤，站点不参与。"""
    authorizer = build_authorizer(monkeypatch, Connection())
    context = consumer_context()

    assert authorizer.can_access(context, ORDER_OWN_IN) is True
    assert authorizer.can_access(context, ORDER_INSIDE) is False


def test_operator_session_reaches_an_order_placed_by_another_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：会话解析 → 运营商站点集合 → 订单查询；站点在集合内即能查。"""
    context, _ = operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})
    authorizer = build_authorizer(monkeypatch, Connection())
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
        context, _ = operator_session(monkeypatch, error=error)
    authorizer = build_authorizer(monkeypatch, Connection())

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
        operator_session(monkeypatch, shop_ids=())
        caplog.clear()
        operator_session(monkeypatch, shop_ids=("SHOP-1",), sites={})

    assert "reason=shop_without_site" in caplog.text
    assert "reason=no_shop_binding" not in caplog.text
    assert B_USER_ID not in caplog.text
    assert TENANT not in caplog.text


def test_unbound_operator_account_is_logged_as_a_missing_binding(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """账号漏登记（应补绑定）是它自己的原因，与「站点不在集合内」不能混。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        operator_session(monkeypatch, shop_ids=())

    assert "reason=no_shop_binding" in caplog.text


# --- 4. 生产实现：同一事实来源 ----------------------------------------------


def test_upms_operator_site_scope_uses_the_backend_authorization_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """店铺集合取 ``/shopuser/getShops``（后端权威授权同一端点），站点经 ``ch_site`` 映射。"""
    transport = FakeUpmsTransport(["SHOP-1"])
    patch_transport(monkeypatch, transport)
    connection = Connection(sites=({"id": SITE_IN, "tenant_id": TENANT, "shop_id": "SHOP-1"},))
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)

    scope = UpmsOperatorSiteScope(upms_settings(), INSIDE_CREDENTIAL).site_scope_by_b_user_id(
        B_USER_ID, TENANT
    )

    assert scope == QueryScope(tenant_id=TENANT, site_ids=(SITE_IN,), user_id=None)
    path, headers = transport.requests[-1]
    assert path == f"/shopuser/getShops?userId={B_USER_ID}"
    assert headers["Authorization"] == f"Bearer {INSIDE_CREDENTIAL}"


def test_upms_operator_site_scope_asks_the_tunnel_for_the_one_forward_it_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """站点归属映射与受限直连共用同一条跳板隧道，且只请求 mysql 这一条转发。

    按 ``permitopen`` 收紧后的 SSH 策略，多要 TDengine/Redis 转发会让
    ``ExitOnForwardFailure=yes`` 杀掉 ssh 进程——于是整个管家端授权链因为一个
    与它无关的拒绝而失败关闭。
    """
    tunnel_calls: list[tuple[str, ...]] = []

    class _Tunnel:
        def __init__(self, settings, *, forwards=("mysql", "tdengine", "redis")) -> None:
            self.settings = settings
            tunnel_calls.append(forwards)

        def __enter__(self):
            return self.settings

        def __exit__(self, *_args: Any) -> bool:
            return False

    transport = FakeUpmsTransport(["SHOP-1"])
    patch_transport(monkeypatch, transport)
    monkeypatch.setattr("aiops_diagnostics.sources._ssh_tunnel", _Tunnel)
    monkeypatch.setattr(
        "aiops_diagnostics.sources.pymysql.connect",
        lambda **_: Connection(sites=({"id": SITE_IN, "tenant_id": TENANT, "shop_id": "SHOP-1"},)),
    )

    scope = UpmsOperatorSiteScope(upms_settings(), INSIDE_CREDENTIAL).site_scope_by_b_user_id(
        B_USER_ID, TENANT
    )

    assert scope.site_ids == (SITE_IN,)
    assert tunnel_calls == [("mysql",)]


# --- 5. 助手入口：显式指名 vs 静默回落（既有契约的对照组）---------------------


def _assistant_app(tmp_path, monkeypatch, connection: Connection):
    return assistant_app(tmp_path, monkeypatch, Caller(operator_context((SITE_IN,))), connection)


def test_assistant_explicit_order_inside_the_operator_scope_starts_a_diagnosis(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指名集合内订单 → 202 诊断（管家端替同事/站点处理问题的正路径）。"""
    client, runtime = _assistant_app(tmp_path, monkeypatch, Connection())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "你好", "order_no": ORDER_INSIDE},
        headers=_OPERATOR_HEADERS,
    )

    assert resp.status_code == 202
    assert resp.json()["type"] == "diagnosis"
    assert runtime.diagnoses == [(ORDER_INSIDE, "你好")]


def test_assistant_explicit_order_outside_the_operator_scope_is_refused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指名集合外订单 → 明确 404，不改写为普通问答、不泄漏订单是否存在。"""
    client, runtime = _assistant_app(tmp_path, monkeypatch, Connection())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": "你好", "order_no": ORDER_OUTSIDE},
        headers=_OPERATOR_HEADERS,
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert runtime.diagnoses == []


def test_assistant_embedded_order_outside_the_operator_scope_falls_through(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """问句内嵌集合外订单号 → 静默回落（既有契约），不是 404。"""
    client, runtime = _assistant_app(tmp_path, monkeypatch, Connection())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": f"订单 {ORDER_OUTSIDE} 怎么还没退款"},
        headers=_OPERATOR_HEADERS,
    )

    assert resp.status_code == 202
    assert resp.json()["type"] == "qa"
    assert runtime.diagnoses == []
