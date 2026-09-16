"""Runtime-level coverage for the promotional card when nothing was searched.

41 live regression (2026-09-16): tapping 行业方案 returned
"当前没有可用的行业方案，未检索到匹配的宣传资料。" — a claim that the library
holds no match. That shortcut has no promotional target bound, so no query was
ever issued: the system asserted something about content it never looked at.

These drive the real GatewayRuntime, because the branch that builds the card
lives inside it and the lower-level helper cannot reach it.
"""

from __future__ import annotations

import os
import time as time_module
from pathlib import Path
from typing import Any

from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


def _runtime(tmp_path: Path, *, with_search: bool = False, agent_store=None):
    """A GatewayRuntime on a private temp database, mirroring the harness in
    test_agent_metrics.py.

    ``with_search`` wires a search client and media signer. Without them the
    promo branch short-circuits at the "no capability" card and never reaches
    the model, which is a different code path from the one under test."""
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    os.chmod(tmp_path, 0o750)
    config = tmp_path / "production.env"
    config.write_text("# test\n", encoding="utf-8")
    os.chmod(config, 0o600)
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
    )
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(gateway_settings.database_file)
    return GatewayRuntime(
        store,
        gateway_settings,
        settings,
        kb_search_client=_stub_search() if with_search else None,
        media_signer=object() if with_search else None,
        agent_store=agent_store,
    )


def _context():
    subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-a")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:qa:write"}),
    )


def _wait_terminal(runtime: Any, context: Any, qa_id: str, *, deadline_s: float = 20.0) -> dict:
    deadline = time_module.time() + deadline_s
    job: dict[str, Any] = {}
    while time_module.time() < deadline:
        job = runtime.get_assistant_qa(context, qa_id) or {}
        if job.get("status") in {"completed", "failed"}:
            return job
        time_module.sleep(0.05)
    return job


def test_promo_without_resolvable_target_does_not_claim_an_empty_library(tmp_path: Path) -> None:
    """No pin resolves -> the card must say the search is unavailable, not that
    the library has nothing."""
    runtime = _runtime(tmp_path)
    context = _context()
    try:
        qa = runtime.start_assistant_qa(context, "给我看看行业解决方案", promo_intent="solution_discovery")
        job = _wait_terminal(runtime, context, qa["qa_id"])
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    result = job["result"]
    assert result["retrieval_status"] == "unavailable"
    text = result["blocks"][0]["text"]
    assert "未检索到匹配的宣传资料" not in text
    assert "不可用" in text


def test_promo_without_resolvable_target_is_english_when_asked(tmp_path: Path) -> None:
    """The outage copy is localized too; an unknown language degrades to zh."""
    runtime = _runtime(tmp_path)
    context = _context()
    try:
        qa = runtime.start_assistant_qa(
            context,
            "show me industry solutions",
            promo_intent="solution_discovery",
            language="en",
        )
        job = _wait_terminal(runtime, context, qa["qa_id"])
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    assert "temporarily unavailable" in job["result"]["blocks"][0]["text"]


def _published_promo_agent(store_path: Path) -> str:
    """A real published promotional agent, so the pin resolves and the runtime
    actually reaches the model instead of short-circuiting on "no target"."""
    from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore

    store = AgentStore(store_path)
    subject = SubjectRecord(b_user_id="B-1", tenant_id="tenant-a")
    admin = ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )

    class _PublishOk:
        def validate(self, *args, **kwargs):
            return None

        def __getattr__(self, _name):
            return lambda *a, **k: None

    manager = AgentManager(store, knowledge_resolver=_PublishOk())
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
    return f"{agent.agent_id}#v1"


def test_promo_click_degrades_to_a_card_when_the_model_is_unreachable(tmp_path: Path, monkeypatch) -> None:
    """41 live (2026-09-16): with the provider account in arrears, every
    case_exploration click returned a hard QA_FAILED — an error the user cannot
    act on, on a product surface whose contract promises an honest card.

    Asserts the value the runtime hands back for the run, which is the
    observable the store and the poll endpoint both carry."""
    from aiops_diagnostics.codex_runtime import AgentRuntimeError

    runtime = _runtime(tmp_path, with_search=True, agent_store=_StoreWithPublishedAgent())
    created = runtime.store.create_assistant_question("tenant-a", "我想看看客户案例")

    def boom(*args, **kwargs):
        raise AgentRuntimeError("model provider unavailable (Arrearage)")

    monkeypatch.setattr("aiops_diagnostics.qa_rag.run_customer_qa_answer", boom)

    try:
        outcome = runtime._try_customer_rag(
            created["qa_id"],
            "我想看看客户案例",
            "tenant-a",
            runtime.diagnostic_settings,
            None,
            None,
            "zh",
            promo_target="agt_12345678#v1",
            promo_intent="case_exploration",
        )
    finally:
        runtime.shutdown()

    # With the model unreachable the run must report completion, not failure:
    # {"status": "failed"} is the hard error this regression is about.
    assert outcome == {"status": "completed"}, outcome


def test_promo_model_outage_card_is_the_outage_copy_not_an_empty_library(tmp_path: Path, monkeypatch) -> None:
    """The card written for a model outage must say the service is unavailable,
    never that the library holds nothing matching."""
    from aiops_diagnostics.codex_runtime import AgentRuntimeError

    runtime = _runtime(tmp_path, with_search=True, agent_store=_StoreWithPublishedAgent())
    monkeypatch.setattr(
        "aiops_diagnostics.qa_rag.run_customer_qa_answer",
        lambda *a, **k: (_ for _ in ()).throw(AgentRuntimeError("model down")),
    )
    captured: dict = {}
    real_update = runtime.store.update_assistant_question

    def spy(qa_id, **kwargs):
        captured.update(kwargs)
        return real_update(qa_id, **kwargs)

    monkeypatch.setattr(runtime.store, "update_assistant_question", spy)
    created = runtime.store.create_assistant_question("tenant-a", "我想看看客户案例")
    try:
        runtime._try_customer_rag(
            created["qa_id"],
            "我想看看客户案例",
            "tenant-a",
            runtime.diagnostic_settings,
            None,
            None,
            "zh",
            promo_target="agt_12345678#v1",
            promo_intent="case_exploration",
        )
    finally:
        runtime.shutdown()

    card = captured.get("result")
    assert card is not None, captured
    assert card["retrieval_status"] == "unavailable"
    assert "暂时不可用" in card["blocks"][0]["text"]
    assert "未检索到匹配的宣传资料" not in card["blocks"][0]["text"]


class _StoreWithPublishedAgent:
    """``select_promo_agent`` calls ``store.version(...)``; answer with a
    snapshot that passes its checks so the runtime proceeds to the model
    instead of short-circuiting on "no resolvable target"."""

    def version(self, agent_id: str, tenant_id: str, version_no: int):
        return type(
            "V",
            (),
            {
                "version_no": version_no,
                "snapshot": {
                    "status": "published",
                    "agent_type": "customer",
                    "output_contract": "blocks-v1",
                    "prompt": "宣传语气。",
                    "knowledge_base_ids": ["kb-promo"],
                },
            },
        )()


def _stub_search():
    """A real KbServiceClient subclass: the runtime asserts isinstance() on it.
    The model raises before retrieval is consulted, so it never dials out."""
    from aiops_diagnostics.knowledge_retrieval import KbServiceClient

    class _Stub(KbServiceClient):
        def __init__(self):
            super().__init__("http://127.0.0.1:1", tenant_id="tenant-a")

        def for_tenant(self, tenant_id: str):
            return self

        def search(self, *args, **kwargs):
            return []

    return _Stub()
