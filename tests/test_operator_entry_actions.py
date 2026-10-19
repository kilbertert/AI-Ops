"""管家端入口的两个快捷动作与「订单检测」成功路径（#427，PRD #423）。

断言的只有对外可观察行为：`X-Business-Entry: operator` 下列出哪两个动作、点击
「订单检测」得到的是订单选择器澄清还是诊断响应、点击「客户案例」在知识库未就绪时
拿到的是明确的不可用而不是空的「未检索到」，以及消费者侧的既有行为零回归。

交付形状沿用 PRD 的决定：两个动作复用既有 code、不新建执行路径；「按租户 + 入口
发布」是运营动作（数据操作），因此用例先用发布动作的同一条生命周期把种子行发布掉，
再断入口行为——发布这一步就是运营的那个动作，不是新代码。

「订单在可见范围内」不是桩：授权判定走真实的 `ScopedOrderAuthorizer`，可见范围来自
会话身份里的运营商站点集合（#426）。成功路径用的订单挂在**别人**名下、站点在集合内，
所以只要实现悄悄回落到「仅本人」范围，这条用例就会红。
"""

from __future__ import annotations

import time as time_module
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from operator_support import (
    AGENT_VERSION_KEY,
    DIAGNOSIS_QUESTION,
    FOLLOWUP_QUESTION,
    ORDER_INSIDE,
    SITE_IN,
    Caller,
    Connection,
    Directory,
    PlatformIdentityResolver,
    Runtime,
    assistant_app,
    build_authorizer,
    create_gateway_app,
    gateway_settings,
    operator_session,
    publish,
)

# 与 #426 共用同一套替身（行级镜像的假 MySQL、真实授权判定、运营商会话身份、发布
# 后的入口应用）：替身各写一份会各自漂移，因此这里不重写。
from operator_support import (
    CONSUMER_HEADERS as _CONSUMER_HEADERS,
)
from operator_support import (
    OPERATOR_HEADERS as _OPERATOR_HEADERS,
)

from aiops_diagnostics.faq import FAQCatalog
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.shortcut_lifecycle import (
    _BUNDLED_SHORTCUTS,
    ShortcutManager,
    ShortcutStore,
)

#: 「客户案例」的点击问句：故意不含任何宣传意图线索（「案例库」「标杆」等都没出现），
#: 因此只有入口侧的动作行能把这次点击判成案例探索——若意图改由文案推导，本条会红。
CASE_QUESTION = "推荐一些内容给我看看"


def _app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    published_entries: tuple[str, ...] = ("operator",),
    connection: Connection | None = None,
) -> tuple[TestClient, Runtime]:
    """发布后的入口应用：会话身份取 #426 的运营商会话（真实范围 + 真实授权判定）。"""
    caller = Caller(operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})[0])
    return assistant_app(
        tmp_path,
        monkeypatch,
        caller,
        connection if connection is not None else Connection(),
        published_entries=published_entries,
    )


def _wait_terminal(client: TestClient, qa_id: str, *, deadline_s: float = 20.0) -> dict[str, Any]:
    deadline = time_module.time() + deadline_s
    body: dict[str, Any] = {}
    while time_module.time() < deadline:
        response = client.get(f"/v1/assistant/questions/{qa_id}", headers=_OPERATOR_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"completed", "failed"}:
            return body
        time_module.sleep(0.05)
    return body


# --- 1. 管家端入口的动作集 ------------------------------------------------


def test_the_two_entries_share_one_definition_of_the_execution_fields() -> None:
    """两个入口的动作行在执行字段上逐字节相同（#427 复用同一 code 与执行路径）。

    这些字段来自 `shortcut_lifecycle._SHARED_ACTION_FIELDS` 这一个常量，两侧各写
    一份会各自漂移，而漂移是 invisible 的——只有按钮文案不同。这一条就是那个
    共享常量的回归：一旦某侧开始自带一份，这里转红。
    """
    consumer = dict(_BUNDLED_SHORTCUTS)["consumer"]
    operator = dict(_BUNDLED_SHORTCUTS)["operator"]
    shared = ("intent", "requires_order", "sort_order", "question_templates")

    for code in ("case_exploration", "smart_diagnosis"):
        for field in shared:
            assert consumer[code][field] == operator[code][field], f"{code}.{field}"
        # 展示字段各自定义（不共享）：两个入口的按钮叫法不同。
        assert (
            set(consumer[code]["labels"])
            == set(operator[code]["labels"])
            == set(consumer[code]["descriptions"])
        )
    # 「订单检测」与「智能检测」是同一个 code 在不同入口的两种叫法。
    assert consumer["smart_diagnosis"]["labels"]["zh"] == "智能检测"
    assert operator["smart_diagnosis"]["labels"]["zh"] == "订单检测"


def test_operator_entry_holds_the_three_actions_and_nothing_else() -> None:
    """operator 内容域的动作复用既有 code，且只此三个（#427 的两个 + #721 的运维巡检）。

    「订单检测」是 `smart_diagnosis`：`requires_order` 为真（靠订单守卫弹澄清），
    **不能**绑智能体版本——生命周期校验只允许宣传类意图绑定，绑了连草稿都存不进去。
    「客户案例」是 `case_exploration`。跳转动作不进助手入口。

    「运维巡检」是 #721 新加的那一个：`requires_order` 为假（它问的是站点集合 + 时间窗，
    不是某一张单），`intent` 用 `knowledge` 而**不是**新意图——意图是分类器也能从自由文本
    产出的东西，给它一个自己的意图等于让一句话也能路由进来。
    """
    specs = dict(_BUNDLED_SHORTCUTS)["operator"]

    assert set(specs) == {"smart_diagnosis", "case_exploration", "operations_review"}
    order = specs["smart_diagnosis"]
    assert order["intent"] == "order_issue"
    assert order["requires_order"] is True
    assert order.get("target_agent_version") is None
    assert order.get("jump_path") is None

    case = specs["case_exploration"]
    assert case["intent"] == "case_exploration"
    assert case["requires_order"] is False
    assert case.get("target_agent_version") is None
    assert case.get("jump_path") is None


def test_operator_entry_lists_all_three_actions_after_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布后，`operator` 入口列出「客户案例」「订单检测」「运维巡检」。

    排序按 `sort_order`：案例 10 → 订单检测 20 → 运维巡检 30。第三条是 #721 加的，
    它排最后是刻意的 —— 巡检是**周期性**动作，不像前两条是围绕当前这一单/这一次对话。
    """
    client, _ = _app(tmp_path, monkeypatch)

    listed = client.get("/v1/shortcuts", headers=_OPERATOR_HEADERS)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["type"] == "shortcut_list"
    assert [item["code"] for item in body["shortcuts"]] == [
        "case_exploration",
        "smart_diagnosis",
        "operations_review",
    ]
    assert all(item["target_agent_version"] is None for item in body["shortcuts"])
    smart = next(item for item in body["shortcuts"] if item["code"] == "smart_diagnosis")
    assert smart["requires_order"] is True

    # 按钮文案是产品内容：管家端叫「订单检测」，与消费者侧「智能检测」不同名。
    assert {item["code"]: item["label"] for item in body["shortcuts"]} == {
        "case_exploration": "客户案例",
        "smart_diagnosis": "订单检测",
        "operations_review": "运维巡检",
    }
    # 每个受支持语言都拿到自己的文案，而不是悄悄回落到 zh（41 live, 2026-09-18
    # 的语言缺口就是这样 invisible 的：请求照旧 200，只是按钮变成了中文）。
    english = client.get("/v1/shortcuts", headers={**_OPERATOR_HEADERS, "Accept-Language": "en"}).json()
    assert {item["code"]: item["label"] for item in english["shortcuts"]} == {
        "case_exploration": "Customer Cases",
        "smart_diagnosis": "Order Diagnosis",
        "operations_review": "Operations Review",
    }


def test_consumer_entry_is_untouched_by_the_operator_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """消费者侧零回归：管家端动作只存在于 operator 内容域，列表与文案都不变。"""
    client, _ = _app(tmp_path, monkeypatch)

    listed = client.get("/v1/shortcuts", headers=_CONSUMER_HEADERS)

    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 0


# --- 2. 「订单检测」的成功路径 ----------------------------------------------


def test_clicked_order_diagnosis_without_an_order_asks_for_the_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点击「订单检测」且没带订单号 → 订单选择器澄清，不启动作业。

    这是入口侧订单守卫的职责：管家端用户不该被推进一次没有订单的诊断，也不该
    被改写成普通问答。
    """
    client, runtime = _app(tmp_path, monkeypatch)

    response = client.post(
        "/v1/assistant/questions",
        json={"question": DIAGNOSIS_QUESTION, "shortcut_code": "smart_diagnosis"},
        headers=_OPERATOR_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "clarification"
    assert body["missing_fields"] == ["order_no"]
    assert runtime.diagnoses == []
    assert runtime.questions == []


def test_clicked_order_diagnosis_with_a_visible_order_starts_the_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带订单号、且该订单在可见范围内 → 启动诊断并返回既有诊断响应形态。

    可见范围由会话身份里的运营商站点集合决定（#426），用的订单挂在**别人**名下、
    站点在集合内：这正是管家端替同事和站点处理问题要看的那一单，也是「仅本人」范围
    下看不到的那一单。
    """
    connection = Connection()
    client, runtime = _app(tmp_path, monkeypatch, connection=connection)

    response = client.post(
        "/v1/assistant/questions",
        json={
            "question": DIAGNOSIS_QUESTION,
            "shortcut_code": "smart_diagnosis",
            "order_no": ORDER_INSIDE,
        },
        headers=_OPERATOR_HEADERS,
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["type"] == "diagnosis"
    assert body["order_no"] == ORDER_INSIDE
    assert body["status"] == "queued"
    assert body["retry_after_ms"] == 1000
    assert body["language"] == "zh"
    assert body["error"] is None
    assert runtime.diagnoses == [(ORDER_INSIDE, DIAGNOSIS_QUESTION)]
    assert runtime.questions == []


def test_the_clicked_order_diagnosis_shares_the_clarification_shape_across_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「订单检测」未绑智能体版本，同 code 在两个入口的**请求形态**一致。

    入口只决定内容域与「哪一份运维配方服务它」（#695），不另开一条执行路径：同样的
    点击在两侧得到同一个澄清响应。

    ⚠️ 这条断言**不足以**证明入口分流没坏——澄清发生在订单守卫处，选版那一段根本
    没跑到。真正证明分流的是下面那两条：一条断言入口确实传到了运行时，一条断言两侧
    拿到的选版结果不同。三条一起才是「一条执行路径、两个入口」。
    """
    client, runtime = _app(tmp_path, monkeypatch, published_entries=("consumer", "operator"))

    operator_response = client.post(
        "/v1/assistant/questions",
        json={"question": DIAGNOSIS_QUESTION, "shortcut_code": "smart_diagnosis"},
        headers=_OPERATOR_HEADERS,
    )
    consumer_response = client.post(
        "/v1/assistant/questions",
        json={"question": DIAGNOSIS_QUESTION, "shortcut_code": "smart_diagnosis"},
        headers=_CONSUMER_HEADERS,
    )

    assert operator_response.status_code == consumer_response.status_code == 200
    operator_body = operator_response.json()
    consumer_body = consumer_response.json()
    assert operator_body["type"] == consumer_body["type"] == "clarification"
    assert operator_body["missing_fields"] == consumer_body["missing_fields"] == ["order_no"]
    assert operator_body["message"] == consumer_body["message"]
    assert runtime.diagnoses == []


def test_the_entry_header_reaches_the_diagnosis_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入口头必须真的传进运行时——这是 #695 的接线那半边。

    注入式替身对「header 有没有被传到门」是**盲的**：它照旧返回一个固定上下文，
    于是把 forward 那行删掉，上面那条形态断言照样全绿。所以这条断言的是**收到的
    参数**，而不是结果。
    """
    connection = Connection()
    client, runtime = _app(tmp_path, monkeypatch, connection=connection)

    client.post(
        "/v1/assistant/questions",
        json={
            "question": DIAGNOSIS_QUESTION,
            "shortcut_code": "smart_diagnosis",
            "order_no": ORDER_INSIDE,
        },
        headers=_OPERATOR_HEADERS,
    )

    assert runtime.entries == ["operator"], runtime.entries


def test_the_entry_header_reaches_the_runtime_on_the_active_order_path_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**第三个**诊断调用点也要传入口：会话活跃订单的随访（route 1c）。

    #695 把入口接到了诊断面上，而诊断面有**四个**调用点（标准诊断路由 + 助手的三条
    订单分支）。上面那条用例只覆盖显式带订单号的那一条；对抗性突变（把 route 1c 的
    `business_entry=` 删掉）证明**没有任何用例会因此转红**——四个调用点里三个有回归、
    第四个没有。

    这条走的是真实链路里最容易漏的那一条：追问只带会话 id、订单号来自会话的
    `active_order_no`。它必须与其它三条一样，把入口传进运行时。
    """
    connection = Connection()
    client, runtime = _app(tmp_path, monkeypatch, connection=connection)

    created = client.post(
        "/v1/conversations", json={"agent_version_key": AGENT_VERSION_KEY}, headers=_OPERATOR_HEADERS
    )
    assert created.status_code == 201, created.text
    cid = created.json()["conversation_id"]
    bound = client.post(
        f"/v1/conversations/{cid}/active-order",
        json={"order_no": ORDER_INSIDE},
        headers=_OPERATOR_HEADERS,
    )
    assert bound.status_code == 200, bound.text

    response = client.post(
        "/v1/assistant/questions",
        json={"question": FOLLOWUP_QUESTION, "conversation_id": cid},
        headers=_OPERATOR_HEADERS,
    )

    assert response.status_code == 202, response.text
    assert response.json()["type"] == "diagnosis"
    assert response.json()["order_no_from_context"] == ORDER_INSIDE
    assert runtime.entries == ["operator"], runtime.entries


# --- 3. 「客户案例」在知识库未就绪时返回明确的不可用 --------------------------


def test_clicked_customer_cases_report_the_missing_library_not_an_empty_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """案例库未就绪 → 明确的不可用卡片，而不是空的「未检索到」。

    「未检索到」是对一个从没读过的库下的结论。这条用例跑真实运行时（那张卡片是
    在它里面生成的），问题文案也不含宣传线索，因此意图只能来自入口侧的动作行。
    """
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    settings = gateway_settings(tmp_path)
    diagnostic_settings = Settings()
    diagnostic_settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(settings.database_file)
    shortcuts = ShortcutManager(ShortcutStore(settings.database_file))
    publish(shortcuts, ("operator",))
    runtime = GatewayRuntime(store, settings, diagnostic_settings)
    try:
        app = create_gateway_app(
            settings=settings,
            store=store,
            runtime=runtime,
            caller_resolver=Caller(operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})[0]),
            order_authorizer=build_authorizer(monkeypatch, Connection()),
            platform_resolver=PlatformIdentityResolver(Directory()),
            faq_catalog=FAQCatalog.bundled(),
            shortcut_manager=shortcuts,
        )
        with TestClient(app) as client:
            accepted = client.post(
                "/v1/assistant/questions",
                json={"question": CASE_QUESTION, "shortcut_code": "case_exploration"},
                headers=_OPERATOR_HEADERS,
            )
            assert accepted.status_code == 202, accepted.text
            body = accepted.json()
            assert body["type"] == "qa"
            job = _wait_terminal(client, body["qa_id"])
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    result = job["result"]
    assert result["retrieval_status"] == "unavailable"
    text = result["blocks"][0]["text"]
    assert "未检索到匹配的宣传资料" not in text
    assert "不可用" in text


def test_the_operator_button_resolves_a_target_from_the_tenant_row(tmp_path: Path) -> None:
    """管家端的按钮能出卡：**平台行给可见性 + 租户行给绑定**（2026-10-19 恢复）。

    这条是 #803 与本次恢复之间那个真正的判据。#803 的机制观察是对的 ——
    **平台行**不允许携带 `target_agent_version`（`_validated_fields` 对
    `PLATFORM_SCOPE` 抛 `platform shortcuts cannot bind a tenant agent version`），
    所以只挂平台行时 `_promo_route` 恒得 `(None, "case_exploration")`。
    但它把「平台行绑不了」读成了「这个入口不该有这张卡」；正确形状一直是两层：
    `list_effective` 里**已发布的租户行覆盖同 code 的平台行**。

    断的是路由判定本身而不是 HTTP 响应：`_promo_route` 是「这一次请求要不要走宣传面、
    钉到哪个版本」的唯一一处。走完整 HTTP 还要过分类器与模型，而本仓测试环境没有模型，
    那时失败的原因与这条判据无关。

    **反向**（这条用例的价值所在）：删掉下面的租户覆盖行，断言转红 —— 这正是
    「只恢复种子、不建租户行」的实际后果，也是 41 上必须补那一步数据的原因。
    """
    from types import SimpleNamespace

    from aiops_diagnostics.gateway_api import _promo_route
    from aiops_diagnostics.shortcut_lifecycle import PLATFORM_SCOPE, PLATFORM_TENANT_ID

    settings = gateway_settings(tmp_path)
    store = ShortcutStore(settings.database_file)
    shortcuts = ShortcutManager(store)

    # 平台行：按钮可见，但**绑不了** target（`create` 对 PLATFORM_SCOPE 会抛）。
    platform_context = type(
        "Ctx",
        (),
        {"effective_tenant_id": PLATFORM_TENANT_ID, "roles": {"ROLE_PLATFORM_ADMIN"}, "caller": None},
    )()
    platform = store.create(
        PLATFORM_TENANT_ID,
        "operator",
        "case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        labels={"zh": "客户案例"},
        descriptions={},
        question_templates={},
        target_agent_version=None,
        created_by="platform-admin",
    )
    shortcuts.publish(
        platform_context, platform.shortcut_id, expected_revision=platform.revision, scope=PLATFORM_SCOPE
    )

    context = SimpleNamespace(shortcut_manager=shortcuts)
    caller = SimpleNamespace(effective_tenant_id="T-1")
    decision = SimpleNamespace(platform="operator")
    payload = SimpleNamespace(question=CASE_QUESTION, shortcut_code="case_exploration")

    # 只有平台行时：意图解析得出（按钮在），但**没有**可钉的版本 ⇒ 空卡。
    assert _promo_route(context, caller, decision, payload) == (None, "case_exploration")

    # 租户覆盖行：把目标钉在该租户自己的已发布宣传智能体上（运营动作）。
    tenant_context = type(
        "Ctx", (), {"effective_tenant_id": "T-1", "roles": {"ROLE_AGENT_ADMIN"}, "caller": None}
    )()
    override = store.create(
        "T-1",
        "operator",
        "case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        labels={"zh": "客户案例"},
        descriptions={},
        question_templates={},
        target_agent_version=AGENT_VERSION_KEY,
        created_by="operator-admin",
    )
    shortcuts.publish(tenant_context, override.shortcut_id, expected_revision=override.revision)

    target, intent = _promo_route(context, caller, decision, payload)

    assert intent == "case_exploration"
    assert target == AGENT_VERSION_KEY, "租户行给绑定——只恢复种子不建这行，管家端仍是空卡"


# --- 3. 「运维巡检」：点击 → 一张跨订单的运维卡（#721） -------------------------


def test_the_operator_entry_lists_the_operations_review_action(tmp_path: Path, monkeypatch) -> None:
    """管家的可用动作里有「运维巡检」，且它**不需要订单**。"""
    client, _ = _app(tmp_path, monkeypatch)
    body = client.get("/v1/shortcuts", headers=_OPERATOR_HEADERS).json()

    codes = [item["code"] for item in body["shortcuts"]]
    assert "operations_review" in codes, codes
    review = next(item for item in body["shortcuts"] if item["code"] == "operations_review")
    # 「不需要订单」是这张卡与「订单检测」的根本区别：它问的是
    # 「这一组站点这一段区间整体如何」，不是某一张单。
    assert review["requires_order"] is False
    assert review["label"] == "运维巡检"


def test_the_consumer_entry_does_not_see_the_operations_review_action(tmp_path: Path, monkeypatch) -> None:
    """客户端入口看不到它 —— 判据是**列出有效动作**，不是「代码里有没有那一行」。"""
    client, _ = _app(tmp_path, monkeypatch, published_entries=("consumer",))
    body = client.get("/v1/shortcuts", headers=_CONSUMER_HEADERS).json()

    codes = [item["code"] for item in body["shortcuts"]]
    assert "operations_review" not in codes, codes


def test_clicking_operations_review_starts_an_analysis_job_not_a_diagnosis(
    tmp_path: Path, monkeypatch
) -> None:
    """点它起的是**分析**作业，且**窗口由服务端决定**。

    断言三件事，缺一条都可能是错的实现：
    ① 起的是分析作业（不是诊断）；
    ② `site_ids=None` —— 「调用者可见的全部站点」，由 worker 与实时 scope 求交；
    ③ 窗口是服务端给的 `OPERATIONS_REVIEW_WINDOW_HOURS`，且**长度正确**、
        `window_end` 贴近请求时刻。判据用**长度**而不是字面值，因为字面值会是
        「测试写死了今天的日期」那种明天就烂的断言。
    """
    from datetime import datetime

    from aiops_diagnostics.gateway_api import OPERATIONS_REVIEW_WINDOW_HOURS

    client, runtime = _app(tmp_path, monkeypatch)
    response = client.post(
        "/v1/assistant/questions",
        headers=_OPERATOR_HEADERS,
        json={"question": "帮我看一下最近的运维巡检", "shortcut_code": "operations_review"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["type"] == "analysis", body
    assert body["analysis_id"].startswith("an_"), body
    assert runtime.diagnoses == [], "点击运维巡检不得起一张订单诊断"

    job = runtime.analyses[-1]
    assert job["site_ids"] is None, job
    start = datetime.fromisoformat(job["window_start"])
    end = datetime.fromisoformat(job["window_end"])
    assert (end - start).total_seconds() == OPERATIONS_REVIEW_WINDOW_HOURS * 3600, job
    assert abs((datetime.now(start.tzinfo) - end).total_seconds()) < 120, job


def test_a_clicked_operations_review_is_not_hijacked_by_an_order_in_the_sentence(
    tmp_path: Path, monkeypatch
) -> None:
    """问句里出现订单号也不得把这张卡改成一次订单诊断。

    点击是最明确的意图信号；让 Route 1 先认领它，等于回答了**按钮没问的那个问题**。
    """
    client, runtime = _app(tmp_path, monkeypatch)
    response = client.post(
        "/v1/assistant/questions",
        headers=_OPERATOR_HEADERS,
        json={
            "question": f"帮我看一下最近的运维巡检（{ORDER_INSIDE}）",
            "shortcut_code": "operations_review",
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["type"] == "analysis", response.json()
    assert runtime.diagnoses == [], "订单号不得把运维巡检变成订单诊断"


def test_operations_review_requires_a_published_action(tmp_path: Path, monkeypatch) -> None:
    """草稿行的 `shortcut_code` 不生效 —— 种子是草稿，发布才是运营动作。

    同一条规矩适用于入口其余动作：未发布时点击**不能**起作业。
    """
    client, runtime = _app(tmp_path, monkeypatch, published_entries=())
    response = client.post(
        "/v1/assistant/questions",
        headers=_OPERATOR_HEADERS,
        json={"question": "帮我看一下最近的运维巡检", "shortcut_code": "operations_review"},
    )

    assert response.status_code == 202
    assert response.json()["type"] != "analysis", response.json()
    assert getattr(runtime, "analyses", []) == [], "未发布的动作不得起分析作业"
