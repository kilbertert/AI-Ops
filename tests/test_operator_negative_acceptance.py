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

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# 与 #426 共用同一套替身（行级镜像的假 MySQL、真实授权判定、运营商会话身份、发布
# 后的入口应用）、与 #427 共用点击问句：替身各写一份会各自漂移，因此都不重写。
from operator_support import (
    AGENT_VERSION_KEY,
    B_USER_ID,
    C_USER_ID,
    DIAGNOSIS_QUESTION,
    ORDER_INSIDE,
    ORDER_MISSING,
    ORDER_OUTSIDE,
    ORDER_OWN_IN,
    SITE_IN,
    SITE_OUT,
    TENANT,
    Caller,
    Connection,
    FakeShops,
    OperatorScope,
    assistant_app,
    b_subject,
    mysql_settings,
    operator_caller,
    operator_session,
    resolve_session,
)
from operator_support import (
    CONSUMER_HEADERS as _CONSUMER_HEADERS,
)
from operator_support import (
    OPERATOR_HEADERS as _OPERATOR_HEADERS,
)

from aiops_diagnostics.config import Settings
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.identity.caller_auth import ScopedOrderAuthorizer
from aiops_diagnostics.identity.query_scope import resolve_operator_site_scope
from aiops_diagnostics.sources import MySQLSource


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
    client, runtime = assistant_app(
        tmp_path, monkeypatch, operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)}), Connection()
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


def test_a_changed_operator_scope_makes_the_conversation_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """范围指纹覆盖运营商维度：站点集合变化即会话不可见——统一 404，无存在性泄漏。

    用户故事 19 + 12：权限变化后旧会话不能再被用来追问；同一个 conversation_id
    在任何一个站点集合下都得到 404，不告诉调用者它存在过。
    """
    caller = operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)})
    client, _ = assistant_app(tmp_path, monkeypatch, caller, Connection())

    conversation = client.post(
        "/v1/conversations", json={"agent_version_key": AGENT_VERSION_KEY}, headers=_OPERATOR_HEADERS
    )
    cid = conversation.json()["conversation_id"]

    # 下一次会话解析给出更大的站点集合（指纹随之改变）。
    caller.context = operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN, SITE_OUT)})[0]

    for headers in (_OPERATOR_HEADERS, _CONSUMER_HEADERS):
        resp = client.get(f"/v1/conversations/{cid}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_a_changed_subject_makes_the_conversation_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换成**同一个 C 端用户绑定的另一个 B 端主体**，同一个 conversation_id 也是 404。

    上面那条只换站点集合，**没有换主体** —— 而 #497 的风险声明里有一句
    「不是跨主体泄漏」，那句当时只有「指纹的 payload 含主体 id」这个构造上的依据，
    没有测试证据。评审指出后补上这条。

    单独立一条而不是塞进上面那条：两条的失效**成因不同**（一条是范围收窄，
    一条是主体不同），合成一条时前者先触发，后者就永远验不到了。

    ⚠️ **第一版写错了，是本条用例自己抓出来的**。第一版换的是
    ``b_subject(b_user_id="B-OTHER", c_user_id="C-OTHER")``，而替身的会话用户恒为
    ``C-1`` —— 于是 C→B 映射在 ``record.c_user_id != user_id`` 那一步被判
    ``c_user_mismatch``，**退回 C 端主体、范围退成 ``self``、站点集合清空**。
    那样构造出来的 404 来自**范围退化**，不是「另一个有效主体看不到」，等于用
    前面那条的成因冒充了这一条。正确的隔离方式是：**C 端用户不变（仍 ``C-1``）、
    只把 C→B 映射指向另一个 B 端主体**（评审给的方向）。
    """
    caller = operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)})
    client, _ = assistant_app(tmp_path, monkeypatch, caller, Connection())

    conversation = client.post(
        "/v1/conversations", json={"agent_version_key": AGENT_VERSION_KEY}, headers=_OPERATOR_HEADERS
    )
    assert conversation.status_code == 201, conversation.text
    cid = conversation.json()["conversation_id"]
    owner_scope = caller.context.scope_fingerprint
    assert caller.context.subject.b_subject_reason == ""
    assert caller.context.subject.b_user_id == "B-9"

    # 会话用户仍是 C-1（替身恒定的那个），只换 C→B 映射指向的 B 端主体。
    other = resolve_session(
        monkeypatch,
        records=(b_subject(b_user_id="B-OTHER"),),
        operator_scope=OperatorScope(sites={"SHOP-1": (SITE_IN,)}),
    )
    # **前提断言**：这次换的是主体，且范围**没有**跟着退化 —— 否则下面的 404 是
    # 「范围变空」造成的，与上一条用例同因，本用例就又白写了。
    assert other.subject.b_subject_reason == "", (
        "前提不成立：C→B 映射没解析成有效主体（退回了 C 端），本用例要的是两个有效主体"
    )
    assert other.subject.b_user_id == "B-OTHER"
    assert other.data_scope.type == "organ", "前提不成立：范围类型退化了，404 的成因就不是主体"
    assert sorted(other.data_scope.site_ids) == sorted(caller.context.data_scope.site_ids), (
        "前提不成立：站点集合变了，那又变成范围收窄那条路径了"
    )
    assert other.scope_fingerprint != owner_scope, (
        "前提不成立：换了主体却没换指纹 —— 那说明指纹不含主体，跨主体那条边界不成立"
    )

    caller.context = other
    resp = client.get(f"/v1/conversations/{cid}", headers=_OPERATOR_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


# --- 4. 数据完整性四类情形：站点上的 partner_b_id 不能放宽可见性 --------------


class _PartnerAwareConnection(Connection):
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


class _ChargingBookOperatorScope:
    """会话身份的运营商站点范围：店铺集合走内存替身，站点映射走真实 ``MySQLSource``。

    保留四跳链里唯一有判断的那一跳（店铺 → 站点归属），因此这条用例同时是
    #425 解析函数在真实映射来源上的回归。
    """

    def __init__(self, settings: Settings, shop_ids: tuple[str, ...]) -> None:
        self.source = MySQLSource(settings, scope=None)
        self.shops = FakeShops(shop_ids)

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str):
        return resolve_operator_site_scope(b_user_id, tenant_id, shops=self.shops, mapper=self.source)


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
    settings = mysql_settings()
    connection = _PartnerAwareConnection(orders=ORDERS_AT_THOSE_SITES, sites=SITES)
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)
    scope = _ChargingBookOperatorScope(settings, ("SHOP-1",))

    context = resolve_session(monkeypatch, operator_scope=scope)

    assert context.data_scope.site_ids == ("SITE-TENANT-OWNED",)
    authorizer = ScopedOrderAuthorizer(settings)
    assert authorizer.can_access(context, ORDER_INSIDE) is True
    # 自己的订单也不行：判据是站点归属，不是订单归属，也不是站点那一列。
    assert authorizer.can_access(context, ORDER_OUTSIDE) is False
    assert authorizer.can_access(context, ORDER_MISSING) is False


# --- 5. 消费者端零回归 ------------------------------------------------------


def test_the_consumer_entry_keeps_its_own_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """消费者入口仍只有它自己的三个动作，管家端动作不出现在那里。"""
    client, _ = assistant_app(
        tmp_path,
        monkeypatch,
        operator_caller(monkeypatch, sites={"SHOP-1": (SITE_IN,)}),
        Connection(),
        published_entries=("consumer", "operator"),
    )

    listed = client.get("/v1/shortcuts", headers=_CONSUMER_HEADERS)

    assert listed.status_code == 200, listed.text
    assert sorted(item["code"] for item in listed.json()["shortcuts"]) == [
        "case_exploration",
        "report_fault",
        "smart_diagnosis",
    ]


def test_the_entry_header_reaches_the_session_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入口头必须真的传到解析器 —— 分流逻辑在解析器里，接线断了它就不生效。

    这一条覆盖**接线**（header → resolver 参数）；分流本身（按入口选范围）由
    ``test_operator_order_authorization`` 里那组用例覆盖。两者缺一，
    "消费者侧不再拿到运营商范围"就可能是运气而非实现。
    """
    session = resolve_session(
        monkeypatch,
        records=(b_subject(),),
        operator_scope=OperatorScope(sites={"SHOP-1": (SITE_IN,)}),
        platform_entry="consumer",
    )
    caller = Caller(session)
    client, _ = assistant_app(
        tmp_path, monkeypatch, caller, Connection(), published_entries=("consumer", "operator")
    )

    # 第三个用例**保留 Authorization**：完全无头的请求在鉴权处就被拒，
    # 解析器根本不会被调用，测不出"入口缺失时传了什么"。
    for header, expected in (
        (_CONSUMER_HEADERS, "consumer"),
        (_OPERATOR_HEADERS, "operator"),
        ({"Authorization": "Bearer service"}, None),
    ):
        resp = client.post(
            "/v1/assistant/questions",
            json={"question": DIAGNOSIS_QUESTION, "order_no": ORDER_OWN_IN},
            headers=header,
        )
        assert caller.last_platform_entry == expected, (header, caller.last_platform_entry)
        if expected is None:
            # 省略入口头**不会**被上游自动选成 operator —— 会话路径的 subject 恒带
            # C 端 id，于是 consumer 与 operator 都可用，平台决策判歧义并拒绝。
            # 所以"无头 + 自愿 self 范围"这个组合**不可达**（不是靠本层兜住的）。
            assert resp.status_code == 409, resp.text
            assert resp.json()["error"]["code"] == "PLATFORM_AMBIGUOUS", resp.text
