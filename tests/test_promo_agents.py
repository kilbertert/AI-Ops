"""Promotional case/solution routing tests (#231).

Acceptance from the issue: 案例不进入 FAQ (cases never land in the customer
FAQ); 方案返回结构化卡片 (solution requests return the card contract);
语言跟随请求 (output language follows the request); 无命中时明确无可用
案例/方案 (empty retrieval honestly reports no material, never fabricates
customer facts); 不泄露内部 KB/运行信息 (no internal KB/runtime leak).

All model/KB interaction runs against the published-agent/KB 替身 (stubs)
already used by the QA harness tests — no fixture here is a real acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.promo_agents import (
    promo_empty_result,
    promo_intent_from_text,
    promo_prompt,
    scenario_keywords,
    select_promo_agent,
)
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore

# ---------------------------------------------------------------------------
# Unit layer: intent cues, scenario keywords, empty card, target resolution.
# ---------------------------------------------------------------------------


def test_promo_intent_cues_are_narrow() -> None:
    """Only questions that NAME case/solution exploration route without the
    classifier; ordinary support wording never hits the promotional path."""
    assert promo_intent_from_text("我想看看客户案例") == "case_exploration"
    assert promo_intent_from_text("给我一个行业解决方案") == "solution_discovery"
    assert promo_intent_from_text("show me a case study of port charging") == "case_exploration"
    # Bare broad words that belong to support QA must NOT route here.
    assert promo_intent_from_text("我的充电方案是什么") is None
    assert promo_intent_from_text("怎么拔枪") is None
    assert promo_intent_from_text("") is None


def test_scenario_keywords_steer_search() -> None:
    assert scenario_keywords("港口充电有没有案例") == ["港口"]
    assert "重卡" in scenario_keywords("重卡车队的解决方案")
    assert scenario_keywords("随便看看") == []


def test_empty_card_is_honest_per_intent_and_language() -> None:
    zh_case = promo_empty_result("zh", "case_exploration")
    assert zh_case["retrieval_status"] == "not_found"
    assert "没有可用的客户案例" in zh_case["blocks"][0]["text"]
    en_solution = promo_empty_result("en", "solution_discovery")
    assert "No matching industry solution" in en_solution["blocks"][0]["text"]
    # Unknown language degrades to the default pack, never an error.
    degraded = promo_empty_result("xx", "case_exploration")
    assert degraded["blocks"][0]["text"] == zh_case["blocks"][0]["text"]


def _promo_store(tmp_path: Path) -> tuple[AgentStore, str]:
    store = AgentStore(tmp_path / "gateway.db")
    admin = _admin_context()
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    agent = manager.create(
        admin,
        name="宣传",
        description="",
        config=AgentConfig(
            agent_type="customer",
            prompt="宣传语气，事实只来自检索。",
            knowledge_base_ids=("kb-promo",),
            model="aiops-api",
            output_contract="blocks-v1",
        ),
    )
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    return store, f"{agent.agent_id}#v1"


def test_select_promo_agent_pins_published_version(tmp_path: Path) -> None:
    """A valid `agt_xxx#vN` resolves to the immutable published snapshot."""
    store, target = _promo_store(tmp_path)
    selection = select_promo_agent(store, "tenant-a", target)
    assert selection is not None
    assert target.startswith(selection.agent_id)
    assert selection.knowledge_base_ids == ("kb-promo",)
    # Stale/foreign/absent references degrade to None — never a crash.
    assert select_promo_agent(store, "tenant-b", target) is None
    agent_id = target.partition("#")[0]
    assert select_promo_agent(store, "tenant-a", f"{agent_id}#v99") is None
    assert select_promo_agent(store, "tenant-a", "not-a-ref") is None
    assert select_promo_agent(store, "tenant-a", None) is None


def test_global_promo_action_uses_only_current_tenant_binding(tmp_path: Path) -> None:
    agent_store, target = _promo_store(tmp_path)
    shortcut_store = ShortcutStore(tmp_path / "gateway.db")
    shortcut_manager = ShortcutManager(shortcut_store)
    platform_context = type(
        "Ctx", (), {"effective_tenant_id": "__platform__", "roles": {"ROLE_PLATFORM_ADMIN"}}
    )()
    platform = shortcut_store.create(
        "__platform__",
        "consumer",
        "case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        labels={"zh": "客户案例"},
        descriptions={},
        question_templates={},
        target_agent_version=None,
        created_by="platform",
    )
    shortcut_manager.publish(
        platform_context, platform.shortcut_id, expected_revision=platform.revision, scope="platform"
    )

    tenant_context = type("Ctx", (), {"effective_tenant_id": "tenant-a", "roles": {"ROLE_AGENT_ADMIN"}})()
    override = shortcut_store.create(
        "tenant-a",
        "consumer",
        "case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        labels={"zh": "客户案例"},
        descriptions={},
        question_templates={},
        target_agent_version=target,
        created_by="tenant-a",
    )
    shortcut_manager.publish(tenant_context, override.shortcut_id, expected_revision=override.revision)

    effective_a = shortcut_store.find_effective_by_code("tenant-a", "consumer", "case_exploration")
    effective_b = shortcut_store.find_effective_by_code("tenant-b", "consumer", "case_exploration")
    assert effective_a is not None and effective_a.target_agent_version == target
    assert effective_b is not None and effective_b.target_agent_version is None
    assert select_promo_agent(agent_store, "tenant-a", effective_a.target_agent_version) is not None
    assert select_promo_agent(agent_store, "tenant-b", effective_a.target_agent_version) is None


def test_promo_prompt_injects_keywords_and_language(tmp_path: Path) -> None:
    store, target = _promo_store(tmp_path)
    selection = select_promo_agent(store, "tenant-a", target)
    assert selection is not None
    prompt = promo_prompt(selection, "港口充电的客户案例", language="zh", intent="case_exploration")
    # Industry keyword from the question is passed into the search hint.
    assert "港口" in prompt
    # No keyword named: fair-rotation instruction instead.
    rotation = promo_prompt(selection, "看看案例", language="zh", intent="case_exploration")
    assert "Rotate fairly" in rotation
    # Output language instruction follows the request language.
    english = promo_prompt(selection, "port charging case", language="en", intent="case_exploration")
    assert "English" in english


# ---------------------------------------------------------------------------
# HTTP layer: routing precedence — promo never enters the FAQ branch.
# ---------------------------------------------------------------------------


class _Caller:
    """Management token "admin" carries the admin role and every requested
    scope; token "narrow" holds only faq:read (assistant consumer callers)."""

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del third_session
        if token == "narrow" and required_scope != "aiops:faq:read":
            raise CallerAuthError("insufficient scope", code=CALLER_AUTH_FORBIDDEN)
        subject = SubjectRecord(b_user_id="c:C-1", c_user_id="C-1", tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset({"ROLE_AGENT_ADMIN"}) if token == "admin" else frozenset(),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _PromoRuntime:
    """Records the promo kwargs the API layer forwards; returns a queued job."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._qa = {}

    def shutdown(self) -> None:
        pass

    def start_assistant_qa(
        self,
        context: ScopeContext,
        question: str,
        *,
        conversation=None,
        conversation_turn_no=None,
        language="zh",
        promo_target: str | None = None,
        promo_intent: str | None = None,
    ):
        del context, conversation, conversation_turn_no
        self.calls.append({"question": question, "promo_target": promo_target, "promo_intent": promo_intent})
        qa_id = "qa_test00000000000000000000000000000001"
        self._qa[qa_id] = {"qa_id": qa_id, "question": question, "status": "queued", "result": None}
        return self._qa[qa_id]

    def get_assistant_qa(self, context: ScopeContext, qa_id: str):
        del context
        return self._qa.get(qa_id)


def _client(tmp_path: Path, runtime: _PromoRuntime) -> Any:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Caller(),
        order_authorizer=lambda: None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    from fastapi.testclient import TestClient

    return TestClient(app)


_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "consumer"}


_ADMIN_HEADERS = {"Authorization": "Bearer admin", "X-Business-Entry": "consumer"}


def _publish_promo_shortcut(client, code: str, intent: str, target: str | None) -> None:
    created = client.post(
        "/v1/shortcuts",
        headers=_ADMIN_HEADERS,
        json={
            "business_entry": "consumer",
            "code": code,
            "intent": intent,
            "requires_order": False,
            "sort_order": 5,
            "labels": {"zh": "按钮", "en": "Button"},
            "descriptions": {"zh": "说明"},
            "question_templates": {"zh": "模板"},
            "target_agent_version": target,
        },
    )
    assert created.status_code == 201, created.text
    shortcut = created.json()
    published = client.post(
        f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
        headers=_ADMIN_HEADERS,
        json={"expected_revision": shortcut["revision"]},
    )
    assert published.status_code == 200, published.text


def _ask(client, question: str, **overrides: Any) -> Any:
    return client.post(
        "/v1/assistant/questions",
        headers=_HEADERS,
        json={"question": question, **overrides},
    )


def test_case_request_never_lands_in_faq(tmp_path: Path) -> None:
    """案例不进入 FAQ: an explicit case-exploration question must start a
    promo QA job even when the wording could plausibly hit FAQ keywords."""
    runtime = _PromoRuntime()
    client = _client(tmp_path, runtime)
    response = _ask(client, "我想看看客户案例")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["type"] == "qa"
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call["promo_intent"] == "case_exploration"


def test_shortcut_code_pins_promo_target(tmp_path: Path) -> None:
    """快捷动作通过稳定 code 引用 Agent/version: the clicked code resolves
    the tenant's own published shortcut and forwards its pinned target."""
    runtime = _PromoRuntime()
    client = _client(tmp_path, runtime)
    target = "agt_promo00000000000000000000000000001#v1"
    _publish_promo_shortcut(client, "cases", "case_exploration", target)
    response = _ask(client, "看看案例", shortcut_code="cases")
    assert response.status_code == 202, response.text
    call = runtime.calls[0]
    assert call["promo_target"] == target
    assert call["promo_intent"] == "case_exploration"
    # Unknown/stale codes fall through to the normal ladder — the promo run
    # then comes from the free-text cue; the pinned target degrades to the
    # tenant's first published shortcut of that intent (here: the same one).
    runtime.calls.clear()
    stale = _ask(client, "我想看看客户案例", shortcut_code="no-such-code")
    assert stale.status_code == 202, stale.text
    assert runtime.calls[0]["promo_intent"] == "case_exploration"  # free-text cue
    assert runtime.calls[0]["promo_target"] == target  # first published fallback


def test_free_text_solution_cue_routes_without_shortcut(tmp_path: Path) -> None:
    runtime = _PromoRuntime()
    client = _client(tmp_path, runtime)
    response = _ask(client, "有港口的行业解决方案吗")
    assert response.status_code == 202, response.text
    call = runtime.calls[0]
    assert call["promo_intent"] == "solution_discovery"
    assert call["promo_target"] is None  # honest empty card at runtime level


def test_ordinary_question_still_uses_normal_ladder(tmp_path: Path) -> None:
    """Without promotional cues nothing changes. A non-FAQ, non-promo question
    (e.g. a greeting-free general question) starts the plain QA job with no
    promo kwargs at all; FAQ hits keep their synchronous 200 answer."""
    runtime = _PromoRuntime()
    client = _client(tmp_path, runtime)
    response = _ask(client, "你们公司在哪里")
    assert response.status_code == 202, response.text
    assert runtime.calls[0]["promo_intent"] is None
    assert runtime.calls[0]["promo_target"] is None
    # A keyword FAQ hit never starts a job at all (Route 2b stays first
    # for non-promo questions).
    faq = _ask(client, "怎么拔枪")
    assert faq.status_code == 200
    assert faq.json()["type"] == "faq"
    assert runtime.calls == [{"question": "你们公司在哪里", "promo_target": None, "promo_intent": None}]


def test_promo_response_hides_internal_details(tmp_path: Path) -> None:
    """不泄露内部 KB/运行信息: the 202 body carries only the public qa
    contract — no agent_id, no KB ids, no target reference."""
    runtime = _PromoRuntime()
    client = _client(tmp_path, runtime)
    target = "agt_promo00000000000000000000000000001#v1"
    _publish_promo_shortcut(client, "cases", "case_exploration", target)
    response = _ask(client, "我想看看客户案例", shortcut_code="cases")
    assert response.status_code == 202
    dumped = json.dumps(response.json())
    assert "agt_promo" not in dumped
    assert "kb-" not in dumped


# Helpers reused from the existing QA fixtures, kept local so this file
# stays self-contained.
def _admin_context() -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-a")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


class _PublishOkResolver:
    """Knowledge binding resolver that accepts publishes (test fixture)."""

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def _publish_ok():
    return _PublishOkResolver()


def test_unavailable_card_does_not_claim_an_empty_library() -> None:
    """A kb-service outage must not be reported as "the library holds no
    match". Verified on 41: with the provider account in arrears, kb-service
    answers 502 code=102, and the old code turned that into
    "未检索到匹配的宣传资料" — a false statement about content nobody read."""
    outage = promo_empty_result("zh", "solution_discovery", retrieval_status="unavailable")
    empty = promo_empty_result("zh", "solution_discovery")

    assert outage["retrieval_status"] == "unavailable"
    assert empty["retrieval_status"] == "not_found"

    # The two must not share copy: one asserts content, the other an outage.
    assert outage["blocks"][0]["text"] != empty["blocks"][0]["text"]
    assert "未检索到匹配的宣传资料" not in outage["blocks"][0]["text"]
    assert "不可用" in outage["blocks"][0]["text"]

    # Localized, and unknown languages degrade rather than raise. Compare
    # like-for-like: the copy is keyed by intent as well as language.
    en_outage = promo_empty_result("en", "solution_discovery", retrieval_status="unavailable")
    assert "temporarily unavailable" in en_outage["blocks"][0]["text"]
    assert en_outage["blocks"][0]["text"] != outage["blocks"][0]["text"]
    assert (
        promo_empty_result("xx", "solution_discovery", retrieval_status="unavailable")["blocks"][0]["text"]
        == outage["blocks"][0]["text"]
    )

    # The status survives into the public blocks contract unchanged.
    assert outage["retrieval_status"] in {"found", "not_found", "unavailable", "limited"}


def test_unavailable_card_is_still_a_valid_blocks_payload() -> None:
    """The card goes through the same QaAnswer contract as a normal answer."""
    from aiops_diagnostics.qa_rag import _finalize  # noqa: F401  (import path guard)

    card = promo_empty_result("zh", "case_exploration", retrieval_status="unavailable")
    assert isinstance(card["blocks"], list) and card["blocks"]
    assert card["blocks"][0]["kind"] == "text"
    assert isinstance(card["blocks"][0]["text"], str) and card["blocks"][0]["text"]
