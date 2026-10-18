"""A zero-order answer reaches the client as blocks-v1, like every other one.

PRD #708: the assistant-question poll contract is one shape —
``{"blocks": [...], "retrieval_status": ...}`` — and
``docs/agents/frontend-api-brief.md`` §10.8 scenario B tells a client to
render ``blocks[]``. The RAG and promotional paths already wrote it, but the
plain zero-order turn stored its model payload verbatim as
``{"text", "reminder"}``.

The cost was measured, not hypothetical: on 41, 9 of the 33 completed rows
carried the text-only shape, and the shipped UlinkEV 1.2.14 build reads
``blocks[]`` only — it renders its own "未找到" fallback for every one of
them, so a user's question was answered and then displayed as unanswered.
"""

from __future__ import annotations

import os
from pathlib import Path

SCOPE = "tenant-a"
QUESTION = "什么是分时电价"
#: What `run_zero_order_answer` returns — its documented T3/#153 shape.
MODEL_ANSWER = {"text": "分时电价按峰、谷、平段分别计价。", "reminder": True}


def _runtime(tmp_path: Path, *, with_search: bool = False):
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge_retrieval import KbServiceClient

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

    class _StubSearch(KbServiceClient):
        """A real subclass: the runtime asserts isinstance() on it. The model
        raises before retrieval is consulted, so it never dials out."""

        def __init__(self):
            super().__init__("http://127.0.0.1:1", tenant_id=SCOPE)

        def for_tenant(self, tenant_id: str):
            return self

        def search(self, *args, **kwargs):
            return []

    return GatewayRuntime(
        GatewayStore(gateway_settings.database_file),
        gateway_settings,
        settings,
        kb_search_client=_StubSearch() if with_search else None,
        media_signer=object() if with_search else None,
    )


def _conversation_turn(runtime) -> tuple[str, tuple[str, str, int]]:
    conversation = runtime.conversation_store.create(
        scope_fingerprint=SCOPE,
        business_entry="consumer",
        agent_version_key="agt_12345678#v1",
    )
    turn_no = runtime.conversation_store.begin_turn(
        conversation["conversation_id"], SCOPE, kind="qa", question=QUESTION
    )
    return conversation["conversation_id"], (conversation["conversation_id"], SCOPE, turn_no)


def _run_worker(runtime, qa_id: str, conversation_turn, monkeypatch) -> None:
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_runtime.run_zero_order_answer",
        lambda *_a, **_k: dict(MODEL_ANSWER),
    )
    runtime._execute_assistant_qa(
        qa_id, None, QUESTION, None, None, SCOPE, conversation_turn, "zh", None, None
    )


def test_zero_order_answer_is_stored_as_blocks(tmp_path: Path, monkeypatch) -> None:
    """The regression: the model's prose must survive as a renderable block."""
    runtime = _runtime(tmp_path)
    _, turn = _conversation_turn(runtime)
    qa = runtime.store.create_assistant_question(SCOPE, QUESTION)

    _run_worker(runtime, qa["qa_id"], turn, monkeypatch)

    stored = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
    assert stored is not None
    assert stored["status"] == "completed"
    assert stored["result"] == {
        "blocks": [{"kind": "text", "text": MODEL_ANSWER["text"]}],
        "retrieval_status": "not_found",
    }
    # The old shape must not survive alongside it: one shape, not two.
    assert "text" not in stored["result"]


def test_poll_response_carries_the_blocks_the_client_renders() -> None:
    """The public body is what the APK reads — assert the field it walks."""
    from aiops_diagnostics.gateway_api import _assistant_question_response
    from aiops_diagnostics.gateway_runtime import as_blocks_result

    body = _assistant_question_response(
        {
            "qa_id": "qa_x",
            "question": QUESTION,
            "status": "completed",
            "result": as_blocks_result(dict(MODEL_ANSWER)),
            "error_code": None,
        },
        "zh",
    )

    assert body["result"]["retrieval_status"] == "not_found"
    kinds = [block["kind"] for block in body["result"]["blocks"]]
    assert kinds == ["text"]
    # Non-empty, so a renderer that joins text blocks shows the answer rather
    # than falling back to its own "not found" copy.
    assert body["result"]["blocks"][0]["text"] == MODEL_ANSWER["text"]


def test_normalizer_is_not_a_second_contract() -> None:
    """Shapes that already satisfy the contract pass through untouched."""
    from aiops_diagnostics.gateway_runtime import as_blocks_result

    blocks = {"blocks": [{"kind": "text", "text": "已在合同内"}], "retrieval_status": "found"}
    assert as_blocks_result(blocks) is blocks
    # Empty/absent prose has nothing to move; it must not become an empty
    # blocks array, which the QaAnswer contract forbids (min_length=1).
    assert as_blocks_result({"text": "   ", "reminder": False}) == {"text": "   ", "reminder": False}
    assert as_blocks_result({}) == {}


def test_legacy_rows_are_read_back_verbatim(tmp_path: Path) -> None:
    """Stored history is not rewritten: a row written in the old shape is
    returned exactly as it was persisted."""
    runtime = _runtime(tmp_path)
    qa = runtime.store.create_assistant_question(SCOPE, QUESTION)
    legacy = dict(MODEL_ANSWER)
    assert runtime.store.update_assistant_question(qa["qa_id"], status="completed", result=legacy)

    stored = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
    assert stored is not None
    assert stored["result"] == legacy


def _publish_customer_agent(path: Path, *, tenant_id: str = SCOPE) -> None:
    """Publish one real customer agent, so the RAG path is actually reached.

    Built through `AgentManager` rather than a fake store: `select_customer_agent`
    reads `.list()`, `.version()` and a published snapshot's `agent_type` /
    `knowledge_base_ids`, and a hand-rolled stub that satisfies all of those
    would be testing the stub. This mirrors `tests/test_dify_registry_route_proof.py`.
    """
    from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context

    class _PublishOk:
        def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
            del tenant_id, knowledge_base_ids

    manager = AgentManager(AgentStore(path), knowledge_resolver=_PublishOk(), allowed_models=("aiops-api",))
    context = admin_context(tenant_id)
    created = manager.create(
        context,
        name="cs-agent",
        description="",
        config=AgentConfig(
            agent_type="customer",
            prompt="客服语气。",
            knowledge_base_ids=("kb-1",),
            model="aiops-api",
            output_contract="blocks-v1",
        ),
    )
    manager.publish(context, created.agent_id, expected_revision=created.revision)


def test_kb_outage_is_not_reported_as_no_match(tmp_path: Path, monkeypatch) -> None:
    """When the reason for the zero-order fall-through is a FAILED search, the
    completed row says `unavailable`, not `not_found`.

    The two causes are different claims about a library: "no customer agent was
    selected" means nothing was searched; a `KnowledgeSearchUnavailable` means
    the library was never read. Stamping both `not_found` tells the client (and
    the next debugger) that the library holds no match — the claim
    `promo_empty_result` already refuses to make for the other card sites, and
    which `_try_customer_rag`'s own comment states must not be made here.
    """
    from aiops_diagnostics.agent_lifecycle import AgentStore
    from aiops_diagnostics.knowledge_retrieval import KnowledgeSearchUnavailable

    runtime = _runtime(tmp_path, with_search=True)
    _publish_customer_agent(tmp_path / "gateway.db")
    runtime.agent_store = AgentStore(tmp_path / "gateway.db")
    _, turn = _conversation_turn(runtime)
    qa = runtime.store.create_assistant_question(SCOPE, QUESTION)

    monkeypatch.setattr(
        "aiops_diagnostics.gateway_runtime.run_zero_order_answer",
        lambda *_a, **_k: dict(MODEL_ANSWER),
    )
    # The search dependency is down: kb-service unreachable mid-run.
    monkeypatch.setattr(
        "aiops_diagnostics.qa_rag.run_customer_qa_answer",
        lambda *_a, **_k: (_ for _ in ()).throw(KnowledgeSearchUnavailable("kb-service is down")),
    )

    runtime._execute_assistant_qa(qa["qa_id"], None, QUESTION, None, None, SCOPE, turn, "zh", None, None)

    stored = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
    assert stored is not None
    assert stored["status"] == "completed", (stored["status"], stored.get("error_code"))
    assert stored["result"]["retrieval_status"] == "unavailable"
    assert stored["result"]["blocks"] == [{"kind": "text", "text": MODEL_ANSWER["text"]}]


def test_no_agent_selected_stays_not_found(tmp_path: Path, monkeypatch) -> None:
    """The other fall-through cause keeps `not_found`: nothing was searched.

    No agent store is wired, so `_try_customer_rag` returns `None` before any
    search — the honest status is "no match", because no library was read.
    """
    runtime = _runtime(tmp_path, with_search=True)
    _, turn = _conversation_turn(runtime)
    qa = runtime.store.create_assistant_question(SCOPE, QUESTION)

    monkeypatch.setattr(
        "aiops_diagnostics.gateway_runtime.run_zero_order_answer",
        lambda *_a, **_k: dict(MODEL_ANSWER),
    )
    runtime._execute_assistant_qa(qa["qa_id"], None, QUESTION, None, None, SCOPE, turn, "zh", None, None)

    stored = runtime.store.get_assistant_question(qa["qa_id"], SCOPE)
    assert stored is not None
    assert stored["result"]["retrieval_status"] == "not_found"
