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

from aiops_diagnostics.i18n import clarification_message
from aiops_diagnostics.query_scope import QueryScope, resolve_query_scope
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    SCOPE_TYPE_ALL,
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
    """问句内嵌集合外订单号 → 回落，但**不再无声**（#620），也不是 404。

    集合外的订单与「不是本人的订单」在这里是同一件事：调用者无权查它。
    回落本身没变（不 404、不建诊断），变的是答复**说出了真实原因**。
    """
    client, runtime = _assistant_app(tmp_path, monkeypatch, Connection())

    resp = client.post(
        "/v1/assistant/questions",
        json={"question": f"订单 {ORDER_OUTSIDE} 怎么还没退款"},
        headers=_OPERATOR_HEADERS,
    )

    assert resp.status_code != 404
    assert runtime.diagnoses == []
    body = resp.json()
    assert body["type"] == "clarification"
    assert body["missing_fields"] == []
    assert body["message"] == clarification_message("zh", "not_yours")


# --- #423 回归：运营商站点范围只在管家端入口生效 ---------------------------


def test_consumer_entry_keeps_self_scope_even_with_a_b_account(monkeypatch):
    """消费者入口下，有 B 端映射**也不得**换成运营商站点集合。

    这是 41 实测发现的越权：替换 ``self`` 原先发生在身份层、对两个内容域同时生效，
    于是 ``consumer`` 入口能查到**他人**名下、位于该运营商站点的订单。
    """
    context = resolve_session(
        monkeypatch,
        records=(b_subject(),),
        operator_scope=OperatorScope(("SHOP-1",), {"SHOP-1": ("SITE-IN-1",)}),
        platform_entry="consumer",
    )
    assert context.data_scope.type == SCOPE_TYPE_SELF
    assert context.data_scope.site_ids == ()


def test_unknown_entry_keeps_self_scope(monkeypatch):
    """入口缺失或非法时按最窄范围处理 —— 不放宽，由后续平台决策拒绝请求。"""
    # 注意 "OPERATOR " 不在此列：它是 operator 的合法写法（见下一条用例）；
    # 这里只放真正无法识别成已知入口的值。
    for entry in (None, "", "operator-admin", "consumer-admin"):
        context = resolve_session(
            monkeypatch,
            records=(b_subject(),),
            operator_scope=OperatorScope(("SHOP-1",), {"SHOP-1": ("SITE-IN-1",)}),
            platform_entry=entry,
        )
        assert context.data_scope.type == SCOPE_TYPE_SELF, entry
        assert context.data_scope.site_ids == (), entry


def test_operator_entry_is_normalized_like_the_platform_resolver(monkeypatch):
    """``OPERATOR`` / `` operator `` 与 ``operator`` 同义 —— 上游就是这么判的。

    两侧规范化不一致时会出现最坏的一种：上游认定是管家入口（于是允许该入口，
    也据此选内容域），而这层按"未知"回落 ``self``，同站点的他人订单被 404。
    """
    for entry in ("operator", "OPERATOR", " operator ", "Operator"):
        context = resolve_session(
            monkeypatch,
            records=(b_subject(),),
            operator_scope=OperatorScope(("SHOP-1",), {"SHOP-1": ("SITE-IN-1",)}),
            platform_entry=entry,
        )
        assert context.data_scope.type == SCOPE_TYPE_ORGAN, entry
        assert context.data_scope.site_ids == ("SITE-IN-1",), entry


def test_operator_entry_still_gets_the_operator_site_set(monkeypatch):
    """管家端入口不受影响 —— 回归修复不得收窄管家端。"""
    context = resolve_session(
        monkeypatch,
        records=(b_subject(),),
        operator_scope=OperatorScope(("SHOP-1",), {"SHOP-1": ("SITE-IN-1",)}),
        platform_entry="operator",
    )
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ("SITE-IN-1",)


# --- 顶层账号：没有店铺绑定 ≠ 看不到任何订单 -------------------------------


def test_tenant_level_account_without_shop_binding_sees_the_whole_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``type='1'`` 租户主账号、零店铺绑定 → 可见范围是**整个租户**，不是空集合。

    实测（2026-10-09，41 生产）：``ulink`` 是 ``type='1'``、``tenant_id=1942105476598861824``、
    ``sys_user_shop`` 0 行、``/shopuser/getShops`` 返回 ``[]``。它要查的那张订单**就在它的
    租户里**，却一律 404 —— 因为「没有店铺子集」被读成了「看不到任何订单」。

    公司自己的店铺隔离门对这两个类型（``-1``/``1``）**不做店铺隔离**，所以这里正确的
    读法是「不受站点维度约束」。租户谓词照常下推，放宽的只有站点维度。
    """
    context = resolve_session(
        monkeypatch,
        # 顶层账号经**会话**链进来（C→B 映射要求 c_user_id 与会话一致，因此会话形态
        # 仍带 C 绑定；无 C 绑定的租户主账号只能走公司令牌链，见 test_company_token_auth）。
        records=(b_subject(user_type="1"),),
        # 店铺集合为空（真实值）：它若走运营商分支就会得到空站点。
        operator_scope=OperatorScope(()),
    )

    assert context.data_scope.type == SCOPE_TYPE_ALL
    assert resolve_query_scope(context) == QueryScope(tenant_id=TENANT, site_ids=None, user_id=None)


def test_platform_account_type_is_tenant_level_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """``type='-1'``（平台）与 ``type='1'`` 同属顶层：公司隔离门对两者一视同仁。"""
    context = resolve_session(
        monkeypatch,
        records=(b_subject(user_type="-1"),),
        operator_scope=OperatorScope(()),
    )
    assert context.data_scope.type == SCOPE_TYPE_ALL


def test_an_operator_account_without_shop_binding_stays_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``type='5'`` 代理商空绑定 → **仍是空集合拒绝**：这一条不放宽。

    「没有店铺绑定」对顶层账号（整个租户）与运营商账号（看不到任何订单）含义相反，
    只有 ``sys_user.type`` 能区分。把这一条改宽会让所有未登记的代理商账号看到全租户。
    """
    context = resolve_session(
        monkeypatch,
        records=(b_subject(user_type="5"),),
        operator_scope=OperatorScope(()),
    )
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ()
    assert resolve_query_scope(context).empty_site_scope


def test_missing_user_type_is_treated_as_the_narrow_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到 ``type`` 时不猜：按普通运营商账号处理（空集合 → 拒绝），不放宽成整租户。"""
    context = resolve_session(
        monkeypatch,
        records=(b_subject(),),  # user_type 默认空串
        operator_scope=OperatorScope(()),
    )
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ()


def test_a_tenant_level_account_never_needs_the_shop_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """顶层账号的范围与店铺集合无关 ⇒ 一条 ``/shopuser/getShops`` 都不该发。

    这一条同时钉住「提前返回」不是靠运气：``operator_scope`` 传一个**会抛错**的替身，
    若实现仍走运营商分支就会在这里炸。
    """
    scope = OperatorScope(("SHOP-1",), {"SHOP-1": (SITE_IN,)}, error=AssertionError("不应调用店铺目录"))
    context = resolve_session(
        monkeypatch,
        records=(b_subject(user_type="1"),),
        operator_scope=scope,
    )
    assert context.data_scope.type == SCOPE_TYPE_ALL
    assert scope.calls == []  # 站点范围解析器一次都没被调用
    assert scope.shops.calls == []  # 店铺目录一次都没被调用


def test_consumer_entry_keeps_self_for_a_tenant_level_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """顶层账号走**消费者入口**时逐字不变：仍是 ``self``，不放宽成整租户。

    顶层范围的授予条件是「顶层账号 **且** operator 入口」；少了入口这一维，
    一个恰好也有 C 端身份的租户主账号会在消费者侧看到全租户的订单。
    """
    context = resolve_session(
        monkeypatch,
        records=(b_subject(user_type="1"),),
        operator_scope=OperatorScope(()),
        platform_entry="consumer",
    )
    assert context.data_scope.type == SCOPE_TYPE_SELF


def test_the_operators_own_consumer_order_is_not_visible_to_the_operator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一账号两种身份对**同一张自己下的单**给出相反答案 —— 这是口径，不是缺陷。

    产品 2026-10-10 裁定：管家端里「自己作为消费者下的单」**不应可见**。
    运营商范围是**替换** ``self`` 而不是取并集，因此这两个集合不相交。

    实测（41）：账号 ``13928110252`` 作为消费者有一张单，``consumer`` 入口 202、
    ``operator`` 入口拒。这里把它钉成**断言不等**——将来若有人把两个范围改成并集，
    本用例转红，那时要问的是「口径是否变了」，而不是直接接受这个改动。
    """
    order = {"order_no": ORDER_OWN_IN, "tenant_id": TENANT, "site_id": SITE_OUT, "user_id": C_USER_ID}
    connection = Connection(orders=(order,))

    # 同一张单、同一个 C 端用户：消费者入口按 self 可见……
    consumer = consumer_session(monkeypatch)
    consumer_authorizer = build_authorizer(monkeypatch, connection)
    assert consumer_authorizer.can_access(consumer, ORDER_OWN_IN) is True

    # ……管家入口按运营商站点集合判定，站点不在集合内 ⇒ 拒。
    operator, _ = operator_session(monkeypatch, shop_ids=("SHOP-1",), sites={"SHOP-1": (SITE_IN,)})
    operator_authorizer = build_authorizer(monkeypatch, connection)
    assert operator_authorizer.can_access(operator, ORDER_OWN_IN) is False

    # 两个范围是两种东西，不是一个的子集关系。
    assert resolve_query_scope(consumer).user_id == C_USER_ID
    assert resolve_query_scope(operator).user_id is None
