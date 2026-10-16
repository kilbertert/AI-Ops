"""管家端入口的快捷动作与「订单检测」成功路径（#427，PRD #423）。

断言的只有对外可观察行为：`X-Business-Entry: operator` 下列出哪些动作、点击
「订单检测」得到的是订单选择器澄清还是诊断响应、宣传面在知识库未就绪时给出的是
明确的不可用而不是空的「未检索到」，以及消费者侧的既有行为零回归。

**2026-10-16 变更**：`case_exploration`（「客户案例」）从 `operator` 种子里移除
（用户决定：管家端用户不要看到客户案例卡片）。理由不是「素材没配」而是**该入口
结构上产不出这张卡**：平台行不允许携带 `target_agent_version`，而 `operator` 侧
只有平台行 ⇒ `_promo_route` 恒解析不出宣传智能体 ⇒ 每次点击都是空卡（10-15 生产
实测 8 次全部 `unavailable` + 9–13ms）。本入口的宣传面因此只剩**自由文本**一条路，
那条路按用户选择保持不动。详见 `test_operator_entry_holds_the_two_actions` 与
`test_operator_entry_reaches_the_promo_surface_only_by_free_text`。

交付形状沿用 PRD 的决定：动作复用既有 code、不新建执行路径；「按租户 + 入口
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

#: 同一意图的**自由文本**问句：自带「客户案例」这条确定性线索，所以它会经
#: `promo_intent_from_text` 进宣传面，不依赖任何已发布动作行、也不依赖分类器。
#: 管家端摘掉的是按钮，不是这条自由文本的路（2026-10-16 用户选择）。
CASE_QUESTION_WITH_CUE = "有哪些客户案例"


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

    判据只在**两侧都有**的 code 上成立：`case_exploration` 已于 2026-10-16 从
    `operator` 入口移除（见 `test_operator_entry_holds_the_two_actions`），
    所以这里只剩 `smart_diagnosis` 一条共享 code。`consumer` 侧仍在用自己的那一份。
    """
    consumer = dict(_BUNDLED_SHORTCUTS)["consumer"]
    operator = dict(_BUNDLED_SHORTCUTS)["operator"]
    shared = ("intent", "requires_order", "sort_order", "question_templates")

    # 两侧都有的 code 才谈得上「共享一份定义」；这一句让新增/移除 code 时
    # 本用例自己会提示该往哪边看。
    assert set(consumer) & set(operator) == {"smart_diagnosis"}

    code = "smart_diagnosis"
    for field in shared:
        assert consumer[code][field] == operator[code][field], f"{code}.{field}"
    # 展示字段各自定义（不共享）：两个入口的按钮叫法不同。
    assert (
        set(consumer[code]["labels"]) == set(operator[code]["labels"]) == set(consumer[code]["descriptions"])
    )
    # 「订单检测」与「智能检测」是同一个 code 在不同入口的两种叫法。
    assert consumer["smart_diagnosis"]["labels"]["zh"] == "智能检测"
    assert operator["smart_diagnosis"]["labels"]["zh"] == "订单检测"


def test_operator_entry_holds_the_two_actions() -> None:
    """operator 内容域只此两个动作：订单检测（#427）+ 运维巡检（#721）。

    「订单检测」是 `smart_diagnosis`：`requires_order` 为真（靠订单守卫弹澄清），
    **不能**绑智能体版本——生命周期校验只允许宣传类意图绑定，绑了连草稿都存不进去。
    跳转动作不进助手入口。

    「运维巡检」是 #721 新加的那一个：`requires_order` 为假（它问的是站点集合 + 时间窗，
    不是某一张单），`intent` 用 `knowledge` 而**不是**新意图——意图是分类器也能从自由文本
    产出的东西，给它一个自己的意图等于让一句话也能路由进来。

    **`case_exploration` 于 2026-10-16 移除**（用户决定：管家端用户不要看到客户案例卡片）。
    它不是被停用，是**不该在这个入口存在**：平台行不允许携带 `target_agent_version`，
    而 `operator` 侧只有平台行 ⇒ 该入口**结构上不可能**解析出宣传智能体 ⇒ 每次点击都是
    「客户案例服务暂时不可用」。10-15 生产实测 8 次全部 `unavailable` + 9–13ms。
    这条断言就是那个决定的回归——把种子加回去，本用例转红。
    """
    specs = dict(_BUNDLED_SHORTCUTS)["operator"]

    assert set(specs) == {"smart_diagnosis", "operations_review"}
    assert "case_exploration" not in specs

    order = specs["smart_diagnosis"]
    assert order["intent"] == "order_issue"
    assert order["requires_order"] is True
    assert order.get("target_agent_version") is None
    assert order.get("jump_path") is None


def test_operator_entry_lists_both_actions_after_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布后，`operator` 入口列出「订单检测」「运维巡检」——**没有**「客户案例」。

    排序按 `sort_order`：订单检测 20 → 运维巡检 30（案例那条 10 已随种子移除）。
    巡检排最后是刻意的 —— 它是**周期性**动作，不像订单检测是围绕当前这一单。
    """
    client, _ = _app(tmp_path, monkeypatch)

    listed = client.get("/v1/shortcuts", headers=_OPERATOR_HEADERS)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["type"] == "shortcut_list"
    assert [item["code"] for item in body["shortcuts"]] == [
        "smart_diagnosis",
        "operations_review",
    ]
    assert all(item["target_agent_version"] is None for item in body["shortcuts"])
    smart = next(item for item in body["shortcuts"] if item["code"] == "smart_diagnosis")
    assert smart["requires_order"] is True

    # 按钮文案是产品内容：管家端叫「订单检测」，与消费者侧「智能检测」不同名。
    assert {item["code"]: item["label"] for item in body["shortcuts"]} == {
        "smart_diagnosis": "订单检测",
        "operations_review": "运维巡检",
    }
    # 每个受支持语言都拿到自己的文案，而不是悄悄回落到 zh（41 live, 2026-09-18
    # 的语言缺口就是这样 invisible 的：请求照旧 200，只是按钮变成了中文）。
    english = client.get("/v1/shortcuts", headers={**_OPERATOR_HEADERS, "Accept-Language": "en"}).json()
    assert {item["code"]: item["label"] for item in english["shortcuts"]} == {
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


# --- 3. 管家端的宣传面只剩自由文本一条路，且它仍然诚实 ------------------------


def test_the_operator_button_cannot_reach_the_promo_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`case_exploration` 这个 **code** 在 `operator` 入口解析不出宣传意图。

    断的是路由判定本身，不是 HTTP 响应：`_promo_route` 是本入口决定「这一次请求
    要不要走宣传面」的唯一一处，所以它就是这条路的判据。走完整 HTTP 会先绕到
    分类器（本仓测试环境没有模型），那时失败的原因与本次改动无关，反而把这条
    断言的真实含义冲淡。

    `forced_intent` 传 None 是刻意的 —— 它模拟「客户端自己发来这个 code」这条
    最坏路径：意图只能来自入口侧的动作行，而那一行已经不存在了。
    """
    from types import SimpleNamespace

    from aiops_diagnostics.gateway_api import _promo_route

    settings = gateway_settings(tmp_path)
    shortcuts = ShortcutManager(ShortcutStore(settings.database_file))
    publish(shortcuts, ("operator",))
    context = SimpleNamespace(shortcut_manager=shortcuts)
    caller = SimpleNamespace(effective_tenant_id="T-1")
    decision = SimpleNamespace(platform="operator")

    target, intent = _promo_route(
        context, caller, decision, SimpleNamespace(question=CASE_QUESTION, shortcut_code="case_exploration")
    )

    assert (target, intent) == (None, None), "管家端不得再经按钮进入宣传面"


def test_operator_entry_free_text_still_reaches_the_promo_surface_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自由文本问句仍走宣传面，知识库未就绪时给的仍是**明确的不可用**。

    这条用例在 2026-10-16 之前用「点击『客户案例』」驱动；那个按钮已从本入口移除
    （`test_operator_entry_holds_the_two_actions` 是那个决定的回归）。按用户选择，
    **只摘按钮、不动自由文本**——本用例就是那条自由文本路的回归。

    跑真实运行时（那张卡片是在它里面生成的）。问句用**确定性线索**
    （`promo_intent_from_text`，不经分类器），因此没有「模型恰好这么答」的抖动。
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
                json={"question": CASE_QUESTION_WITH_CUE},
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
