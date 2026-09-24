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

import os
import time as time_module
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# 与 #426 共用同一套替身（假 MySQL 行级镜像、真实授权判定、运营商会话身份）：
# 两份拷贝会各自漂移，因此这里不重写，直接复用。
from test_operator_order_authorization import (
    B_USER_ID,
    ORDER_INSIDE,
    SITE_IN,
    TENANT,
    _authorizer,
    _Connection,
    _operator_session,
)

from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import SubjectRecord
from aiops_diagnostics.shortcut_lifecycle import (
    _BUNDLED_SHORTCUTS,
    ShortcutManager,
    ShortcutStore,
)

#: 「订单检测」的成功路径问句：不带订单号，由动作的 requires_order 决定去向。
DIAGNOSIS_QUESTION = "帮我检测这个订单的充电异常"
#: 「客户案例」的点击问句：故意不含任何宣传意图线索（「案例库」「标杆」等都没出现），
#: 因此只有入口侧的动作行能把这次点击判成案例探索——若意图改由文案推导，本条会红。
CASE_QUESTION = "推荐一些内容给我看看"

_OPERATOR_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "operator"}
_CONSUMER_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


class _Directory:
    """平台身份目录：该会话有 admin client_type，operator 入口可用。"""

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord(B_USER_ID, c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _StubRuntime:
    """记录「有没有启动作业 / 启动的是哪一单」的运行时替身。"""

    def __init__(self) -> None:
        self.diagnoses: list[tuple[str, str]] = []
        self.questions: list[dict[str, Any]] = []

    def shutdown(self) -> None:
        return None

    def start_standard_diagnosis(
        self, context: Any, order_no: str, question: str, indicator_code, language="zh"
    ):
        del context, indicator_code
        self.diagnoses.append((order_no, question))
        return {
            "diagnosis_id": "dx_operator0000000000000000000001",
            "order_no": order_no,
            "question": question,
            "indicator_code": None,
            "status": "queued",
            "result": None,
            "error_code": None,
            "error_message": None,
            "created_at": "2026-09-24T00:00:00+00:00",
            "updated_at": "2026-09-24T00:00:00+00:00",
            "completed_at": None,
        }

    def get_standard_diagnosis(self, context: Any, diagnosis_id: str):
        del context, diagnosis_id
        return None

    classified = None

    def classify_lightweight(self, question: str, *, language: str = "zh", tenant_id: str | None = None):
        del question, language, tenant_id
        return self.classified

    def start_assistant_qa(self, context: Any, question: str, **kwargs: Any):
        del context, kwargs
        record = {
            "qa_id": "qa_operator0000000000000000000001",
            "question": question,
            "status": "queued",
            "result": None,
        }
        self.questions.append(record)
        return record

    def get_assistant_qa(self, context: Any, qa_id: str):
        del context
        return next((item for item in self.questions if item["qa_id"] == qa_id), None)

    def list_assistant_qa(self, context: Any, *, limit: int = 50):
        del context, limit
        return []


class _PublisherContext:
    """运营发布者的身份：租户管理员在该租户的 operator 入口发布既有动作。"""

    effective_tenant_id = TENANT
    roles = frozenset({"ROLE_AGENT_ADMIN"})
    caller = SubjectRecord(b_user_id="ops-admin", tenant_id=TENANT)


def _settings(tmp_path: Path) -> GatewayServerSettings:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    # 配置文件是私有文件：运行时在读它之前会校验权限。
    os.chmod(settings.server_config_file, 0o600)
    return settings


def _publish(manager: ShortcutManager, entries: tuple[str, ...]) -> None:
    """按 租户 + 入口 发布种子行（PRD 说的那个数据操作）。"""
    context = _PublisherContext()
    for seeded in manager.store.seed_bundled(context, manager):
        if seeded.business_entry in entries:
            manager.publish(context, seeded.shortcut_id, expected_revision=seeded.revision)


def _app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: Any = None,
    connection: _Connection | None = None,
    published_entries: tuple[str, ...] = ("operator",),
) -> tuple[TestClient, Any]:
    settings = _settings(tmp_path)
    store = GatewayStore(settings.database_file)
    shortcuts = ShortcutManager(ShortcutStore(settings.database_file))
    _publish(shortcuts, published_entries)
    selected_runtime = runtime if runtime is not None else _StubRuntime()
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=selected_runtime,
        caller_resolver=_OperatorCaller(monkeypatch),
        order_authorizer=_authorizer(monkeypatch, connection if connection is not None else _Connection()),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
        shortcut_manager=shortcuts,
    )
    return TestClient(app), selected_runtime


class _OperatorCaller:
    """把每个请求都解析成 #426 的运营商会话身份（organ + 该运营商的站点集合）。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.context, self.scope = _operator_session(monkeypatch, sites={"SHOP-1": (SITE_IN,)})

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> Any:
        del token, third_session, required_scope
        return self.context


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


def test_operator_entry_holds_the_two_actions_and_nothing_else() -> None:
    """PRD #427：operator 内容域的两个动作复用既有 code，且只此两个。

    「订单检测」是 `smart_diagnosis`：`requires_order` 为真（靠订单守卫弹澄清），
    **不能**绑智能体版本——生命周期校验只允许宣传类意图绑定，绑了连草稿都存不进去。
    「客户案例」是 `case_exploration`。跳转动作不进助手入口，因此管家端没有第三个动作。
    """
    specs = dict(_BUNDLED_SHORTCUTS)["operator"]

    assert set(specs) == {"smart_diagnosis", "case_exploration"}
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


def test_operator_entry_lists_both_actions_after_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布后，`operator` 入口列出「订单检测」与「客户案例」。"""
    client, _ = _app(tmp_path, monkeypatch)

    listed = client.get("/v1/shortcuts", headers=_OPERATOR_HEADERS)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["type"] == "shortcut_list"
    assert [item["code"] for item in body["shortcuts"]] == ["case_exploration", "smart_diagnosis"]
    assert all(item["target_agent_version"] is None for item in body["shortcuts"])
    smart = next(item for item in body["shortcuts"] if item["code"] == "smart_diagnosis")
    assert smart["requires_order"] is True

    # 按钮文案是产品内容：管家端叫「订单检测」，与消费者侧「智能检测」不同名。
    assert {item["code"]: item["label"] for item in body["shortcuts"]} == {
        "case_exploration": "客户案例",
        "smart_diagnosis": "订单检测",
    }
    # 每个受支持语言都拿到自己的文案，而不是悄悄回落到 zh（41 live, 2026-09-18
    # 的语言缺口就是这样 invisible 的：请求照旧 200，只是按钮变成了中文）。
    english = client.get("/v1/shortcuts", headers={**_OPERATOR_HEADERS, "Accept-Language": "en"}).json()
    assert {item["code"]: item["label"] for item in english["shortcuts"]} == {
        "case_exploration": "Customer Cases",
        "smart_diagnosis": "Order Diagnosis",
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
    connection = _Connection()
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


def test_the_clicked_order_diagnosis_behaves_like_the_same_code_on_the_consumer_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「订单检测」未绑智能体版本，同 code 在两个入口的行为一致。

    入口只决定内容域，不另开一条执行路径：同样的点击在两侧得到同一个澄清响应。
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

    settings = _settings(tmp_path)
    diagnostic_settings = Settings()
    diagnostic_settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(settings.database_file)
    shortcuts = ShortcutManager(ShortcutStore(settings.database_file))
    _publish(shortcuts, ("operator",))
    runtime = GatewayRuntime(store, settings, diagnostic_settings)
    try:
        app = create_gateway_app(
            settings=settings,
            store=store,
            runtime=runtime,
            caller_resolver=_OperatorCaller(monkeypatch),
            order_authorizer=_authorizer(monkeypatch, _Connection()),
            platform_resolver=PlatformIdentityResolver(_Directory()),
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
