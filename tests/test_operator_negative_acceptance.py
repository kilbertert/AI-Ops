"""管家端负向与回归验收（#428，PRD #423）。

本片不写产品代码，产出是**证据**：把 PRD #428 负向 + 回归轨道逐条钉成用例，
让「越权必被拒且不区分存在性」「无运营商身份必被拒」「活跃订单每轮重新校验」
「数据完整性四类情形的行为」「消费者端零回归」在代码里可回归，而不是只写在
文档里。正向轨道（真实运营商会话查到真实订单）无法在本会话构造，如实记为
待业务条件，不在这里用消费者会话或人造夹具冒充。

断言的都是对外可观察行为：HTTP 状态与响应类型、订单可见性（能查/被拒）、
有没有启动作业、按钮与提示文案。不测调用顺序，不断言 SQL 文本。助手入口用
**真实**授权判定与真实范围下推驱动（复用 #426 的替身与 #427 的入口栈），
所以「回落到仅本人」「把店铺 id 当站点 id」这类实现退化会直接让用例转红。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# 与 #426 共用同一套替身（假 MySQL 行级镜像、真实授权判定、运营商会话身份），
# 与 #427 共用入口栈（发布动作、平台身份、记录作业的运行时）。两份拷贝会
# 各自漂移，因此都不重写，直接复用。
from test_operator_entry_actions import (
    _CONSUMER_HEADERS,
    _OPERATOR_HEADERS,
    _Directory,
    _publish,
    _settings,
    _StubRuntime,
)
from test_operator_order_authorization import (
    B_USER_ID,
    C_USER_ID,
    ORDER_INSIDE,
    ORDER_MISSING,
    ORDER_OUTSIDE,
    SITE_IN,
    SITE_OUT,
    TENANT,
    _authorizer,
    _Connection,
    _operator_session,
    _session,
)

from aiops_diagnostics.caller_auth import ScopedOrderAuthorizer
from aiops_diagnostics.config import Settings
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.query_scope import resolve_operator_site_scope
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore
from aiops_diagnostics.sources import MySQLSource

#: 「订单检测」的点击问句：不带订单号，由动作的 requires_order 决定去向。
DIAGNOSIS_QUESTION = "帮我检测这个订单的充电异常"
#: 追问问句：只含订单业务线索，因此走活跃订单分支而不是普通问答。
FOLLOWUP_QUESTION = "我刚才那笔充电订单为什么突然停了"
FOLLOWUP_AFTER_LOSS = "那笔订单现在怎么还不退款"
AGENT_VERSION_KEY = "agt_abcdef1234567890#v1"


class _Caller:
    """把每个请求都解析成给定的会话身份；身份可在轮与轮之间替换。

    换身份就是「权限变了」的真实形状：下一次请求重新解析会话，得到一个新的
    站点集合（或一个新的范围指纹），而不是沿用上一轮的上下文。
    """

    def __init__(self, context: Any) -> None:
        self.context = context

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> Any:
        del token, third_session, required_scope
        return self.context


def _app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: _Caller,
    connection: _Connection,
    *,
    published_entries: tuple[str, ...] = ("operator",),
) -> tuple[TestClient, _StubRuntime]:
    """助手入口应用：真实授权判定 + 真实范围下推 + 已发布的入口动作。"""
    settings = _settings(tmp_path)
    store = GatewayStore(settings.database_file)
    shortcuts = ShortcutManager(ShortcutStore(settings.database_file))
    _publish(shortcuts, published_entries)
    runtime = _StubRuntime()
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=caller,
        order_authorizer=_authorizer(monkeypatch, connection),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
        shortcut_manager=shortcuts,
    )
    return TestClient(app), runtime


def _operator_caller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sites: dict[str, tuple[str, ...]],
    shop_ids: tuple[str, ...] = ("SHOP-1",),
) -> _Caller:
    """#426 的运营商会话身份：店铺集合 → 站点集合（经 #425 的真实解析函数）。"""
    return _Caller(_operator_session(monkeypatch, shop_ids=shop_ids, sites=sites)[0])


def _click_order_diagnosis(
    client: TestClient, order_no: str, *, headers: dict[str, str] | None = None
) -> Any:
    """前端路径：点击「订单检测」并带上订单选择器给出的订单号。"""
    return client.post(
        "/v1/assistant/questions",
        json={"question": DIAGNOSIS_QUESTION, "shortcut_code": "smart_diagnosis", "order_no": order_no},
        headers=headers if headers is not None else _OPERATOR_HEADERS,
    )


# --- 1. 越权必被拒，且不区分「不存在」与「无权」-----------------------------


def test_clicked_order_diagnosis_of_another_operators_order_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式指名集合外订单 → 404 ORDER_NOT_FOUND，零作业，且与「订单不存在」同形。

    管家端用户故事 5/6/7：查不到别的运营商的订单、拒绝要明确、且拒绝不能
    用来试探订单是否存在。走的是前端真实路径（`shortcut_code`），不是裸
    `order_no` 字段。
    """
    client, runtime = _app(
        tmp_path,
        monkeypatch,
        _operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)}),
        _Connection(),
    )

    refused = _click_order_diagnosis(client, ORDER_OUTSIDE)
    missing = _click_order_diagnosis(client, ORDER_MISSING)

    assert refused.status_code == missing.status_code == 404, (refused.text, missing.text)
    assert refused.json()["error"]["code"] == missing.json()["error"]["code"] == "ORDER_NOT_FOUND"
    # 拒绝不是存在性预言：响应体里既没有订单、也不说「无权」。
    assert "order_no" not in refused.json()
    assert "forbidden" not in refused.text and "unauthorized" not in refused.text.lower()
    # 不改写成普通问答，也不启动诊断。
    assert refused.json().get("type") is None
    assert runtime.diagnoses == []
    assert runtime.questions == []


def test_a_site_outside_the_scope_is_refused_even_when_the_order_is_the_callers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只挂在本人名下、站点在集合外的订单同样被拒——替换 self，不是与 self 取并集。"""
    client, runtime = _app(
        tmp_path,
        monkeypatch,
        _operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)}),
        _Connection(),
    )

    assert _click_order_diagnosis(client, ORDER_OUTSIDE).status_code == 404
    assert runtime.diagnoses == []


def test_an_operator_account_without_shop_binding_is_refused_at_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """未绑定店铺 → 空集合 → 拒绝，不回落为租户级放行，且不发起订单查询。"""
    connection = _Connection()
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        caller = _operator_caller(monkeypatch, sites={}, shop_ids=())
    client, runtime = _app(tmp_path, monkeypatch, caller, connection)

    refused = _click_order_diagnosis(client, ORDER_INSIDE)

    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert runtime.diagnoses == []
    assert [item for item in connection.executed if "ch_order_info" in item[0]] == []
    # 空集合的成因（账号漏登记）与「站点不在集合内」不是一回事，日志必须可区分。
    assert "reason=no_shop_binding" in caplog.text


# --- 2. 拒绝语言：订单选择器的提示跟随请求语言 -------------------------------


def test_the_order_picker_prompt_is_localized_on_the_operator_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """管端用户点击「订单检测」没带订单 → 本语言的订单选择器提示。

    入口侧订单守卫的职责（用户故事 3 + 23）。少一种语言不会报错，只会让那个
    语言的用户拿到中文提示，因此按语言断言文案互不相同（静默回落会让两种
    语言拿到同一串），而不是只断言非空。
    """
    messages = {}
    client, runtime = _app(
        tmp_path, monkeypatch, _operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)}), _Connection()
    )
    for tag in SUPPORTED_LANGUAGES:
        resp = client.post(
            "/v1/assistant/questions",
            json={"question": DIAGNOSIS_QUESTION, "shortcut_code": "smart_diagnosis"},
            headers={**_OPERATOR_HEADERS, "Accept-Language": tag},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["type"] == "clarification"
        assert body["missing_fields"] == ["order_no"]
        assert body["language"] == tag
        assert "qa_id" not in body and "diagnosis_id" not in body
        assert runtime.diagnoses == [] and runtime.questions == []
        messages[tag] = body["message"]

    assert len(set(messages.values())) == len(SUPPORTED_LANGUAGES)


# --- 3. 活跃订单：每一轮都重新校验权限 --------------------------------------


def test_active_order_follow_up_drops_an_order_that_left_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """追问已确认的活跃订单不带订单号，但每一轮都重新校验（用户故事 12）。

    订单仍在范围内 → 202 diagnosis；订单被改挂到集合外站点（数据侧变动，
    范围指纹不变）→ 同一个追问静默回落为普通问答并清掉失效绑定，而不是
    拿着上一轮的确认继续诊断一个已经看不到的订单。
    """
    connection = _Connection()
    client, runtime = _app(
        tmp_path, monkeypatch, _operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)}), connection
    )

    conversation = client.post(
        "/v1/conversations", json={"agent_version_key": AGENT_VERSION_KEY}, headers=_OPERATOR_HEADERS
    )
    assert conversation.status_code == 201, conversation.text
    cid = conversation.json()["conversation_id"]

    bound = client.post(
        f"/v1/conversations/{cid}/active-order",
        json={"order_no": ORDER_INSIDE},
        headers=_OPERATOR_HEADERS,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["active_order_no"] == ORDER_INSIDE

    first = client.post(
        "/v1/assistant/questions",
        json={"question": FOLLOWUP_QUESTION, "conversation_id": cid},
        headers=_OPERATOR_HEADERS,
    )
    assert first.status_code == 202, first.text
    assert first.json()["type"] == "diagnosis"
    assert first.json()["order_no_from_context"] == ORDER_INSIDE

    # 订单改挂到集合外站点：调用者站点集合不变（指纹不变），但订单已不可见。
    connection.orders = [
        {**row, "site_id": SITE_OUT} if row["order_no"] == ORDER_INSIDE else row for row in connection.orders
    ]
    second = client.post(
        "/v1/assistant/questions",
        json={"question": FOLLOWUP_AFTER_LOSS, "conversation_id": cid},
        headers=_OPERATOR_HEADERS,
    )

    assert second.status_code == 202, second.text
    assert second.json()["type"] == "qa"
    assert runtime.diagnoses == [(ORDER_INSIDE, FOLLOWUP_QUESTION)]
    detail = client.get(f"/v1/conversations/{cid}", headers=_OPERATOR_HEADERS)
    assert detail.json()["active_order_no"] is None


def test_a_changed_operator_scope_makes_the_conversation_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """范围指纹覆盖运营商维度：站点集合变化即会话不可见——统一 404，无存在性泄漏。

    用户故事 19 + 12：权限变化后旧会话不能再被用来追问；同一个 conversation_id
    在任何一个站点集合下都得到 404，不告诉调用者它存在过。
    """
    caller = _operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)})
    client, _ = _app(tmp_path, monkeypatch, caller, _Connection())

    conversation = client.post(
        "/v1/conversations", json={"agent_version_key": AGENT_VERSION_KEY}, headers=_OPERATOR_HEADERS
    )
    cid = conversation.json()["conversation_id"]

    # 下一次会话解析给出更大的站点集合（指纹随之改变）。
    caller.context = _operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN, SITE_OUT)})[0]

    for headers in (_OPERATOR_HEADERS, _CONSUMER_HEADERS):
        resp = client.get(f"/v1/conversations/{cid}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


# --- 4. 数据完整性四类情形：站点上的 partner_b_id 不能放宽可见性 --------------


class _PartnerAwareConnection(_Connection):
    """站点替身：按参数取值匹配 ``shop_id`` **或** ``partner_b_id``。

    按参数驱动而不是按列驱动，是为了让「改成按 partner_b_id 取站点」这种
    实现退化自己暴露：那样一来这行会被返回，下面的断言随即转红。
    """

    def fetchall(self) -> list[dict[str, Any]]:
        sql, params = self.executed[-1]
        if "ch_site" not in sql:
            return super().fetchall()
        tenant_id = str(params[0])
        wanted = {str(value) for value in params[1:]}
        return [
            row
            for row in self.sites
            if str(row.get("tenant_id")) == tenant_id
            and (str(row.get("shop_id")) in wanted or str(row.get("partner_b_id") or "") in wanted)
        ]


class _FakeShops:
    def __init__(self, shop_ids: tuple[str, ...]) -> None:
        self.shop_ids = shop_ids
        self.calls: list[str] = []

    def shop_ids_by_b_user_id(self, b_user_id: str) -> tuple[str, ...]:
        self.calls.append(b_user_id)
        return self.shop_ids


class _ChargingBookOperatorScope:
    """会话身份的运营商站点范围：店铺集合走内存替身，站点映射走真实 ``MySQLSource``。

    保留四跳链里唯一有判断的那一跳（店铺 → 站点归属），因此这条用例同时是
    #425 解析函数在真实映射来源上的回归。
    """

    def __init__(self, settings: Settings, connection: _Connection, shop_ids: tuple[str, ...]) -> None:
        self.source = MySQLSource(settings, scope=None)
        self.shops = _FakeShops(shop_ids)

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str):
        return resolve_operator_site_scope(b_user_id, tenant_id, shops=self.shops, mapper=self.source)


def _mysql_settings() -> Settings:
    settings = Settings.from_env()
    settings.mysql.user = "readonly"
    settings.mysql.password = "secret"
    settings.ssh.enabled = False
    return settings


# PRD 数据完整性表的前三类，各给一个站点：租户自有（无 partner_b_id）、
# 悬空引用（partner_b_id 指向不存在的账号）、账号类型不符（指向一个不是
# 代理商的账号）。后两类的 partner_b_id 都**命名了调用者自己**，用来检验
# 「站点上的这一列不能当授权依据」。
SITES = (
    {"id": "SITE-TENANT-OWNED", "tenant_id": TENANT, "shop_id": "SHOP-1", "partner_b_id": ""},
    {"id": "SITE-DANGLING", "tenant_id": TENANT, "shop_id": "SHOP-OTHER", "partner_b_id": B_USER_ID},
    {"id": "SITE-MISTYPED", "tenant_id": TENANT, "shop_id": "SHOP-3", "partner_b_id": "B-CONSUMER-1"},
)
ORDERS_AT_THOSE_SITES = (
    {
        "order_no": ORDER_INSIDE,
        "tenant_id": TENANT,
        "site_id": "SITE-TENANT-OWNED",
        "user_id": "C-OTHER",
    },
    {
        "order_no": ORDER_OUTSIDE,
        "tenant_id": TENANT,
        "site_id": "SITE-DANGLING",
        "user_id": C_USER_ID,
    },
    {
        "order_no": ORDER_MISSING,
        "tenant_id": TENANT,
        "site_id": "SITE-MISTYPED",
        "user_id": C_USER_ID,
    },
)


def test_a_sites_partner_b_id_never_widens_the_operators_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前三类数据完整性情形的可观察行为：可见性只由「调用者持有该店铺」决定。

    - 租户自有站点（无 ``partner_b_id``）在持有店铺的运营商范围内可见；
    - 悬空引用 / 账号类型不符的站点**不**因为站点那一列指向谁而进入范围，
      ``partner_b_id`` 命名调用者自己也不行。

    PRD 表格里后两类的「记录供数据修复」（订单 + 站点 + 原因清单）属数据侧
    跨库只读查询，不在授权判定链路里，本片如实记为未交付；判定侧能证明的是
    **悬空引用不会让任何人多看到一个站点**。
    """
    settings = _mysql_settings()
    connection = _PartnerAwareConnection(orders=ORDERS_AT_THOSE_SITES, sites=SITES)
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)
    scope = _ChargingBookOperatorScope(settings, connection, ("SHOP-1",))

    context = _session(monkeypatch, operator_scope=scope)

    assert context.data_scope.site_ids == ("SITE-TENANT-OWNED",)
    authorizer = ScopedOrderAuthorizer(settings)
    assert authorizer.can_access(context, ORDER_INSIDE) is True
    # 自己的订单也不行：判据是站点归属，不是订单归属，也不是站点那一列。
    assert authorizer.can_access(context, ORDER_OUTSIDE) is False
    assert authorizer.can_access(context, ORDER_MISSING) is False


# --- 5. 消费者端零回归 ------------------------------------------------------


def test_the_consumer_entry_keeps_its_own_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """消费者入口仍只有它自己的三个动作，管家端动作不出现在那里。"""
    client, _ = _app(
        tmp_path,
        monkeypatch,
        _operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)}),
        _Connection(),
        published_entries=("consumer", "operator"),
    )

    listed = client.get("/v1/shortcuts", headers=_CONSUMER_HEADERS)

    assert listed.status_code == 200, listed.text
    assert sorted(item["code"] for item in listed.json()["shortcuts"]) == [
        "case_exploration",
        "report_fault",
        "smart_diagnosis",
    ]


def test_the_consumer_entry_keeps_its_own_order_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通消费者会话（无 B 端账号）的可见性仍只按 C 端本人过滤。

    #426 记录的边界：入口维度不进身份，所以运营商维度对「消费者入口中恰好也有
    B 端账号」的会话同样生效。这一条是它的反向守护——运营商维度**没有**把消费者
    入口放宽到「同租户任意订单」，本人的订单照样能查、别人的照样被拒。
    """
    connection = _Connection()
    consumer = _Caller(_session(monkeypatch, records=()))  # 无 B 端账号：消费者端正常状态
    client, _ = _app(
        tmp_path, monkeypatch, consumer, connection, published_entries=("consumer", "operator")
    )
    own_orders = [row for row in connection.orders if row["user_id"] == C_USER_ID]

    for row in own_orders:
        own = client.post(
            "/v1/assistant/questions",
            json={"question": DIAGNOSIS_QUESTION, "order_no": row["order_no"]},
            headers=_CONSUMER_HEADERS,
        )
        assert own.status_code == 202, own.text
        assert own.json()["type"] == "diagnosis"

    # 同一租户内挂在别人名下、且站点在某个运营商的站点集合里的订单：消费者看不到。
    foreign = client.post(
        "/v1/assistant/questions",
        json={"question": DIAGNOSIS_QUESTION, "order_no": ORDER_INSIDE},
        headers=_CONSUMER_HEADERS,
    )

    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "ORDER_NOT_FOUND"
