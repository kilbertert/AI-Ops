"""Customer QA RAG runtime tests (T3/#170) — protocol-level, mock-first.

Covers the acceptance contract of issue #170: blocks-v1 output with
text/image/video/reference blocks, retrieval_status semantics (found /
not_found / unavailable / limited), forced supplementary search when the model
skips retrieval, the two-search cap, media blocks restricted to this turn's
authorized resources, and published-agent selection rules.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics import answer_language
from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.codex_runtime import AgentRuntimeError, CodexTurnOutput
from aiops_diagnostics.config import AgentSettings
from aiops_diagnostics.i18n import QA_FALLBACK_MESSAGES
from aiops_diagnostics.knowledge_retrieval import (
    KnowledgeSearchUnavailable,
    MediaResource,
    MediaResourceSigner,
    RetrievalStatus,
)
from aiops_diagnostics.qa_rag import (
    CustomerAgentSelection,
    _finalize,
    _initial_prompt,
    _TurnRetrieval,
    run_customer_qa_answer,
    select_customer_agent,
)

_PROJECT_ROOT = Path(__file__).parents[1]

_IMAGE_CHUNK = {
    "knowledge_base_id": "kb-a",
    "chunk_id": "chunk-img",
    "doc_id": "doc-1",
    "docnm_kwd": "拔枪示意.png",
    "content_with_weight": "请先停止充电，再按下枪柄卡扣。",
    "image_id": "ragflow-image-1",
    "mime_type": "image/png",
    "score": 0.9,
}

_VIDEO_CHUNK = {
    "knowledge_base_id": "kb-a",
    "chunk_id": "chunk-vid",
    "doc_id": "doc-2",
    "docnm_kwd": "重卡充电.mp4",
    "content_with_weight": "重卡充电操作演示。",
    "doc_type_kwd": "video",
    "mime_type": "video/mp4",
    "score": 0.85,
}

# 41's live promotional library is Chinese-named media: the only material the
# campaign advertises is a video called 新加坡无人电动巴士.mp4. Its name is the
# resource's identifier, so the card keeps it — see ADR-0007.
_VIDEO_CHUNK_CN_NAME = {
    "knowledge_base_id": "kb-a",
    "chunk_id": "chunk-sg-bus",
    "doc_id": "doc-sg-bus",
    "docnm_kwd": "新加坡无人电动巴士.mp4",
    "content_with_weight": "新加坡无人电动巴士在园区试运营。",
    "mime_type": "video/mp4",
    "score": 0.95,
}

# The same resource named with a sentence instead of a label: the harness stores
# document names verbatim, so this shape is reachable.
_VIDEO_CHUNK_CN_PROSE_NAME = {
    "knowledge_base_id": "kb-a",
    "chunk_id": "chunk-sg-ops",
    "doc_id": "doc-sg-ops",
    "docnm_kwd": "运维记录：新加坡园区试运营.mp4",
    "content_with_weight": "记录试运营期间的充电与调度。",
    "mime_type": "video/mp4",
    "score": 0.95,
}

# The promotional library's other material: the case document itself. The
# knowledge base stores it under its Chinese name, and the reference block
# carries that name so a reader can match the card back to the library.
_DOCX_CHUNK = {
    "knowledge_base_id": "kb-a",
    "chunk_id": "chunk-promo-docx",
    "doc_id": "doc-promo-docx",
    "docnm_kwd": "宣传.docx",
    "content_with_weight": "趋势智能与华为、比亚迪合作交付新加坡国家级无人电动巴士项目。",
    "score": 0.9,
}

_CARD_TEXT = "Here is the autonomous bus introduction video."


class _SearchClient:
    """Fake kb-service client; returns canned chunk payloads per call."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], str, int]] = []

    def search(self, knowledge_base_ids: tuple[str, ...], question: str, top_k: int):
        self.calls.append((knowledge_base_ids, question, top_k))
        if not self.responses:
            return []
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeSession:
    """Scripted Codex session.

    `responses` are consumed in order. When a response is a `dict` with
    `cite_media`, the pending search-results prompt (the last one received)
    is scanned for the harness-issued media resource ids, and the literal
    "PLACEHOLDER_MEDIA" in the response's blocks is replaced by the first
    issued id — the scripted model cannot know the signed id in advance.
    """

    def __init__(self, responses, thread_id: str = "thread-qa") -> None:
        self.responses = list(responses)
        self._thread_id = thread_id
        self.prompts: list[str] = []
        self.closed = False

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def run(self, prompt: str, *, output_schema=None) -> CodexTurnOutput:  # noqa: ARG002
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict) and response.get("cite_media"):
            issued = re.findall(r'"resource_id": "(media_[A-Za-z0-9_-]+)"', self.prompts[-1])
            response = json.loads(
                json.dumps(response["payload"], ensure_ascii=False).replace("PLACEHOLDER_MEDIA", issued[0])
            )
        return CodexTurnOutput(turn_id=f"turn-{len(self.prompts)}", final_response=response, usage={})

    def close(self) -> None:
        self.closed = True


def _selection() -> CustomerAgentSelection:
    return CustomerAgentSelection(
        agent_id="agt_test",
        version_no=1,
        prompt="用简体中文回答充电业务问题。",
        knowledge_base_ids=("kb-a",),
    )


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(codex_bin="/bin/true", run_root=str(tmp_path / "runs"))


def _signer() -> MediaResourceSigner:
    return MediaResourceSigner("test-secret", ttl_seconds=60)


def _tool_request(query: str = "拔枪步骤") -> str:
    return json.dumps(
        {
            "kind": "tool_requests",
            "tool_requests": [{"tool": "knowledge_search", "reason": "需要业务资料", "query": query}],
            "answer": None,
        },
        ensure_ascii=False,
    )


def _empty_tool_request() -> str:
    return json.dumps(
        {"kind": "tool_requests", "tool_requests": [], "answer": None},
        ensure_ascii=False,
    )


def _text_block(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text, "resource_id": "", "reference_id": "", "title": ""}


def _answer(blocks: list[dict[str, Any]], status: str, *, cite_media: bool = False) -> Any:
    payload = json.dumps(
        {
            "kind": "answer",
            "tool_requests": [],
            "answer": {"blocks": blocks, "retrieval_status": status},
        },
        ensure_ascii=False,
    )
    return {"cite_media": True, "payload": payload} if cite_media else payload


def _run(
    tmp_path: Path,
    session: _FakeSession,
    client: _SearchClient,
    question: str = "怎么拔枪",
    language: str | None = None,
) -> dict[str, Any]:
    return run_customer_qa_answer(
        question,
        _selection(),
        _settings(tmp_path),
        search_client=client,
        media_signer=_signer(),
        tenant_id="tenant-a",
        project_root=_PROJECT_ROOT,
        session_factory=lambda *_args, **_kw: session,
        **({"language": language} if language is not None else {}),
    )


def test_image_hit_returns_authorized_media_block(tmp_path: Path) -> None:
    """A hit with an image chunk yields text+image+reference blocks, signed."""
    client = _SearchClient([[dict(_IMAGE_CHUNK)]])
    session = _FakeSession(
        [
            _tool_request(),
            _answer(
                [
                    _text_block("先按卡扣再拔枪。"),
                    {
                        "kind": "image",
                        "text": "",
                        "resource_id": "PLACEHOLDER_MEDIA",
                        "reference_id": "",
                        "title": "拔枪示意.png",
                    },
                    {
                        "kind": "reference",
                        "text": "",
                        "resource_id": "",
                        "reference_id": "chunk-img",
                        "title": "拔枪示意.png",
                    },
                ],
                "found",
                cite_media=True,
            ),
        ]
    )
    result = _run(tmp_path, session, client)
    blocks = result["blocks"]
    assert result["retrieval_status"] == "found"
    assert blocks[0]["kind"] == "text"
    image = blocks[1]
    assert image["kind"] == "image"
    assert image["resource_id"].startswith("media_")
    assert image["media"]["mime_type"] == "image/png"
    assert image["media"]["url"].startswith("/v1/media/")
    assert "ragflow" not in image["media"]["url"]
    assert blocks[2]["kind"] == "reference"
    # The search went only to the agent's bound knowledge base.
    assert client.calls[0][0] == ("kb-a",)


def test_unauthorized_media_reference_is_dropped(tmp_path: Path) -> None:
    """The model inventing a media id loses the block; text survives."""
    client = _SearchClient([[dict(_IMAGE_CHUNK)]])
    session = _FakeSession(
        [
            _tool_request(),
            _answer(
                [
                    _text_block("回答正文。"),
                    {
                        "kind": "image",
                        "text": "",
                        "resource_id": "media_forged_not_issued_this_turn",
                        "reference_id": "",
                        "title": "",
                    },
                ],
                "found",
            ),
        ]
    )
    result = _run(tmp_path, session, client)
    assert [block["kind"] for block in result["blocks"]] == ["text"]
    assert result["retrieval_status"] == "found"


def test_no_hit_reports_not_found(tmp_path: Path) -> None:
    """Empty retrieval chunks surface as retrieval_status=not_found."""
    client = _SearchClient([[]])
    session = _FakeSession(
        [
            _tool_request(),
            _answer([_text_block("资料里没有该内容。")], "not_found"),
        ]
    )
    result = _run(tmp_path, session, client)
    assert result["retrieval_status"] == "not_found"
    assert result["blocks"][0]["kind"] == "text"


def test_model_skipping_search_gets_one_forced_retry(tmp_path: Path) -> None:
    """Business question answered without retrieval triggers one forced search."""
    client = _SearchClient([[dict(_IMAGE_CHUNK)]])
    session = _FakeSession(
        [
            # First response: answers directly, no search — must be rejected.
            _answer([_text_block("直接回答。")], "not_found"),
            # After the forced-search prompt: requests the tool, then answers.
            _tool_request(),
            _answer([_text_block("引用资料后回答。")], "found"),
        ]
    )
    result = _run(tmp_path, session, client, question="怎么拔出充电枪")
    assert result["retrieval_status"] == "found"
    assert "knowledge_search" in session.prompts[1]
    assert len(client.calls) == 1
    assert len(session.prompts) == 3


def test_greeting_needs_no_search(tmp_path: Path) -> None:
    """Chit-chat is answered directly; no forced search is issued."""
    client = _SearchClient([])
    session = _FakeSession([_answer([_text_block("你好，请问有什么可以帮你？")], "not_found")])
    result = _run(tmp_path, session, client, question="你好")
    assert result["blocks"][0]["text"].startswith("你好")
    assert client.calls == []


def test_greeting_model_says_not_needed_is_normalized(tmp_path: Path) -> None:
    """Real-model regression (41 live, 2026-09-15): a greeting answered without
    retrieval came back with retrieval_status="not_needed", which the public
    QaAnswer contract rejects — the whole run failed QA_FAILED. The harness now
    normalizes any non-contract status to not_found; text survives."""
    client = _SearchClient([])
    session = _FakeSession([_answer([_text_block("你好！请问有什么可以帮你？")], "not_needed")])
    result = _run(tmp_path, session, client, question="你好")
    assert result["retrieval_status"] == "not_found"
    assert result["blocks"][0]["text"].startswith("你好")
    assert client.calls == []


def test_search_cap_is_two_calls(tmp_path: Path) -> None:
    """The guard refuses a third search; the run reports limited, text remains."""
    client = _SearchClient([[dict(_IMAGE_CHUNK)], []])
    session = _FakeSession(
        [
            _tool_request("query-1"),
            _tool_request("query-2"),
            # Third request: the guard returns LIMITED with no chunks and no
            # client call; the model must then answer from what it has.
            _tool_request("query-3"),
            _answer([_text_block("最终回答。")], "limited"),
        ]
    )
    result = _run(tmp_path, session, client)
    assert result["retrieval_status"] == "limited"
    assert len(client.calls) == 2


def test_kb_unavailable_still_delivers_text(tmp_path: Path) -> None:
    """kb-service outage maps to retrieval_status=unavailable, text remains."""
    client = _SearchClient([[KnowledgeSearchUnavailable("down")], [KnowledgeSearchUnavailable("down")]])
    session = _FakeSession(
        [
            _tool_request(),
            _answer([_text_block("知识库暂不可用，以下是通用建议。")], "unavailable"),
        ]
    )
    result = _run(tmp_path, session, client)
    assert result["retrieval_status"] == "unavailable"
    assert result["blocks"][0]["kind"] == "text"


def test_empty_tool_request_uses_question_for_bounded_search(tmp_path: Path) -> None:
    client = _SearchClient([[dict(_IMAGE_CHUNK)]])
    session = _FakeSession(
        [
            _empty_tool_request(),
            _answer([_text_block("依据知识库的回答。")], "found"),
        ]
    )
    result = _run(tmp_path, session, client, question="怎么拔枪")
    assert result["retrieval_status"] == "found"
    assert client.calls == [(("kb-a",), "怎么拔枪", 5)]


def test_empty_tool_request_after_unavailable_returns_unavailable_text(tmp_path: Path) -> None:
    client = _SearchClient([KnowledgeSearchUnavailable("down")])
    session = _FakeSession([_empty_tool_request()])
    result = _run(tmp_path, session, client, question="知识库故障时怎么办")
    assert result["retrieval_status"] == "unavailable"
    assert result["searches"] == 1
    assert result["blocks"][0]["kind"] == "text"


def test_empty_tool_request_after_not_found_returns_not_found_text(tmp_path: Path) -> None:
    client = _SearchClient([[]])
    session = _FakeSession([_empty_tool_request()])
    result = _run(tmp_path, session, client, question="不存在的业务问题")
    assert result["retrieval_status"] == "not_found"
    assert result["searches"] == 1
    assert result["blocks"][0]["kind"] == "text"


def test_video_chunk_gets_playable_video_block(tmp_path: Path) -> None:
    """A video chunk yields a video block with an mp4 media descriptor."""
    client = _SearchClient([[dict(_VIDEO_CHUNK)]])
    session = _FakeSession(
        [
            _tool_request("重卡充电"),
            _answer(
                [
                    _text_block("请看演示视频。"),
                    {
                        "kind": "video",
                        "text": "",
                        "resource_id": "PLACEHOLDER_MEDIA",
                        "reference_id": "",
                        "title": "重卡充电.mp4",
                    },
                ],
                "found",
                cite_media=True,
            ),
        ]
    )
    result = _run(tmp_path, session, client)
    video = result["blocks"][1]
    assert video["kind"] == "video"
    assert video["media"]["mime_type"] == "video/mp4"


def test_invented_reference_id_is_dropped(tmp_path: Path) -> None:
    """Reference blocks citing ids not returned this turn are removed."""
    client = _SearchClient([[dict(_IMAGE_CHUNK)]])
    session = _FakeSession(
        [
            _tool_request(),
            _answer(
                [
                    _text_block("回答。"),
                    {
                        "kind": "reference",
                        "text": "",
                        "resource_id": "",
                        "reference_id": "chunk-never-returned",
                        "title": "",
                    },
                ],
                "found",
            ),
        ]
    )
    result = _run(tmp_path, session, client)
    assert [block["kind"] for block in result["blocks"]] == ["text"]


def test_answer_without_text_block_fails_the_run(tmp_path: Path) -> None:
    """An answer turn with zero text blocks raises (job becomes failed)."""
    client = _SearchClient([])
    session = _FakeSession(
        [
            _answer(
                [
                    {
                        "kind": "reference",
                        "text": "",
                        "resource_id": "",
                        "reference_id": "x",
                        "title": "",
                    }
                ],
                "found",
            ),
        ]
    )
    with pytest.raises(AgentRuntimeError):
        _run(tmp_path, session, client, question="你好")


def test_found_claim_without_retrieval_is_downgraded(tmp_path: Path) -> None:
    """The model claiming `found` with nothing retrieved is corrected."""
    client = _SearchClient([])
    session = _FakeSession([_answer([_text_block("你好")], "found")])
    result = _run(tmp_path, session, client, question="你好")
    assert result["retrieval_status"] == "not_found"


class _PublishOkResolver:
    """Knowledge binding resolver that accepts publishes (test fixture)."""

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def _publish_ok() -> _PublishOkResolver:
    return _PublishOkResolver()


def _admin_context():
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

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


def test_select_customer_agent_requires_published_customer(tmp_path: Path) -> None:
    """Only a published customer agent with KBs serves the RAG path."""
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    agent = manager.create(admin, name="客服", description="", config=_customer_config())
    assert select_customer_agent(store, "tenant-a") is None  # draft — not served
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    selection = select_customer_agent(store, "tenant-a")
    assert selection is not None
    assert selection.agent_id == agent.agent_id
    assert selection.knowledge_base_ids == ("kb-a",)
    assert selection.version_no == 1
    # Another tenant sees nothing (tenant scope, no existence leak).
    assert select_customer_agent(store, "tenant-b") is None


def test_select_customer_agent_skips_promo_pinned_agent(tmp_path: Path) -> None:
    """A promotional agent pinned by a published shortcut never serves the
    plain customer QA path (41 live regression, 2026-09-15): creating a newer
    promotional agent made `select_customer_agent` hand the customer-service
    questions to the promo agent because list() orders by created_at DESC."""
    from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore

    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    service = manager.create(admin, name="客服", description="", config=_customer_config())
    manager.publish(admin, service.agent_id, expected_revision=service.revision)
    promo = manager.create(admin, name="宣传", description="", config=_customer_config())
    manager.publish(admin, promo.agent_id, expected_revision=promo.revision)

    # Without a shortcut pin the newest (promo) agent still serves — old behavior.
    selection = select_customer_agent(store, "tenant-a")
    assert selection is not None and selection.agent_id == promo.agent_id

    # Pin the promo agent via a published shortcut: customer QA returns to the
    # customer-service agent, and the promo agent serves only its promo route.
    shortcut_manager = ShortcutManager(ShortcutStore(store.path))
    shortcut = shortcut_manager.create(
        admin,
        {
            "business_entry": "consumer",
            "code": "case_exploration",
            "intent": "case_exploration",
            "requires_order": False,
            "sort_order": 10,
            "labels": {"zh": "客户案例"},
            "descriptions": {"zh": "说明"},
            "question_templates": {"zh": "我想看看客户案例"},
            "target_agent_version": f"{promo.agent_id}#v1",
        },
    )
    shortcut_manager.publish(admin, shortcut.shortcut_id, expected_revision=shortcut.revision)
    selection = select_customer_agent(store, "tenant-a")
    assert selection is not None and selection.agent_id == service.agent_id


def test_select_customer_agent_skips_operations_type(tmp_path: Path) -> None:
    """A published operations agent never serves the customer QA path."""
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    ops = manager.create(
        admin,
        name="运维",
        description="",
        config=AgentConfig(
            agent_type="operations",
            prompt="只读诊断。",
            knowledge_base_ids=("kb-a",),
            model="aiops-api",
            output_contract="diagnosis-v1",
        ),
    )
    manager.publish(admin, ops.agent_id, expected_revision=ops.revision)
    assert select_customer_agent(store, "tenant-a") is None


def test_disabled_agent_is_not_selected(tmp_path: Path) -> None:
    """A disabled published agent stops serving new questions."""
    store = AgentStore(tmp_path / "gateway.db")
    manager = AgentManager(store, knowledge_resolver=_publish_ok())
    admin = _admin_context()
    agent = manager.create(admin, name="客服", description="", config=_customer_config())
    manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
    published = manager.get(admin, agent.agent_id)
    manager.disable(admin, agent.agent_id, expected_revision=published.revision)
    assert select_customer_agent(store, "tenant-a") is None


def _customer_config() -> AgentConfig:
    return AgentConfig(
        agent_type="customer",
        prompt="回答必须引用已授权的业务资料。",
        knowledge_base_ids=("kb-a",),
        model="aiops-api",
        output_contract="blocks-v1",
    )


def test_answer_turn_top_level_shape_is_normalized() -> None:
    """Real-model variant (P0 canary, 2026-09-11): the answer fields arrive at
    turn top level without the `answer` wrapper, and blocks carry `type`
    instead of `kind`. The harness must accept and normalize the shape."""
    from aiops_diagnostics.qa_rag import _normalize_answer_turn

    turn = {
        "kind": "answer",
        "reminder": True,
        "retrieval_status": "found",
        "blocks": [
            {"type": "text", "text": "新加坡无人电动巴士是……"},
            {
                "type": "video",
                "resource_id": "media_x",
                "title": "bus.mp4",
                "mime_type": "video/mp4",
                "caption": "新加坡无人电动巴士项目视频",
            },
        ],
    }
    answer = _normalize_answer_turn(turn)
    assert answer is not None
    assert answer["retrieval_status"] == "found"
    kinds = [block.get("kind") for block in answer["blocks"]]
    assert kinds == ["text", "video"]
    # Display metadata outside the contract is stripped before the strict
    # pydantic validation (extra=forbid) — mime_type/caption never pass through.
    assert set(answer["blocks"][1]) == {"kind", "resource_id", "title"}


def test_answer_turn_contract_shape_unchanged() -> None:
    from aiops_diagnostics.qa_rag import _normalize_answer_turn

    turn = {
        "kind": "answer",
        "tool_requests": [],
        "answer": {"blocks": [{"kind": "text", "text": "好"}], "retrieval_status": "found"},
    }
    answer = _normalize_answer_turn(turn)
    assert answer == {"blocks": [{"kind": "text", "text": "好"}], "retrieval_status": "found"}


def test_answer_turn_shapeless_is_rejected() -> None:
    from aiops_diagnostics.qa_rag import _normalize_answer_turn

    assert _normalize_answer_turn({"kind": "answer"}) is None
    assert _normalize_answer_turn({"kind": "answer", "answer": "not-a-dict"}) is None
    assert _normalize_answer_turn({"kind": "answer", "blocks": []}) is None


def test_full_run_accepts_top_level_answer_turn(tmp_path: Path) -> None:
    """End-to-end through run_customer_qa_answer with a real-model-shaped turn."""
    client = _SearchClient([[dict(_IMAGE_CHUNK)]])
    session = _FakeSession(
        [
            _tool_request(),
            json.dumps(
                {
                    "kind": "answer",
                    "retrieval_status": "found",
                    "blocks": [
                        {"type": "text", "text": "先停止充电再拔枪。"},
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )
    result = run_customer_qa_answer(
        "怎么拔枪",
        _selection(),
        _settings(tmp_path),
        search_client=client,
        media_signer=_signer(),
        tenant_id="tenant-a",
        project_root=_PROJECT_ROOT,
        session_factory=lambda *_args, **_kw: session,
    )
    assert result["retrieval_status"] == "found"
    assert any(block["kind"] == "text" for block in result["blocks"])


def test_output_language_rides_prompts_and_fallback_copy(tmp_path: Path) -> None:
    """An en request carries the output-language directive through every turn
    and localizes the harness fallback copy (#204)."""
    client = _SearchClient([[]])
    session = _FakeSession([_tool_request("gun"), _empty_tool_request()])
    result = _run(
        tmp_path,
        session,
        client,
        question="Why can't I pull out the connector?",
        language="en",
    )
    assert "English" in session.prompts[0]
    assert "English" in session.prompts[1]
    assert result["retrieval_status"] == "not_found"
    assert result["blocks"][0]["text"] == QA_FALLBACK_MESSAGES["en"]["not_found"]


def test_unknown_language_falls_back_to_zh_fallback_copy(tmp_path: Path) -> None:
    client = _SearchClient([[]])
    session = _FakeSession([_empty_tool_request()])
    result = _run(tmp_path, session, client, question="怎么拔枪", language="ja")
    assert result["blocks"][0]["text"] == QA_FALLBACK_MESSAGES["zh"]["not_found"]


def test_initial_prompt_names_output_language() -> None:
    """The initial prompt names the output language; zh stays authoritative."""
    selection = CustomerAgentSelection(
        agent_id="agt", version_no=1, prompt="话术", knowledge_base_ids=("kb",)
    )
    assert "English" in _initial_prompt(selection, "question", "en")
    assert "German" in _initial_prompt(selection, "Frage", "de")
    assert "Simplified Chinese" in _initial_prompt(selection, "问题", "zh")


def test_overfilled_blocks_are_trimmed_to_their_kind() -> None:
    """41 live (2026-09-17): the model answered with a text block that also
    carried resource/reference ids, and the contract rejected the whole answer
    ("text block must not carry resource/reference ids"). The fields are the
    model overfilling a block, not a second intent — trim to the kind."""
    from aiops_diagnostics.qa_rag import _normalize_answer_turn

    turn = {
        "kind": "answer",
        "retrieval_status": "found",
        "blocks": [
            # text carrying reference ids (the live failure shape)
            {
                "kind": "text",
                "text": "案例正文",
                "resource_id": "b2e2d38a064a0ea7",
                "reference_id": "b2e2d38a064a0ea7",
            },
            # reference echoing text and a resource
            {
                "kind": "reference",
                "text": "echo",
                "resource_id": "x",
                "reference_id": "ref-1",
                "title": "宣传.docx",
            },
            # image with a stray text field
            {"kind": "image", "text": "caption", "resource_id": "media-1", "title": "图.png"},
        ],
    }
    result = _normalize_answer_turn(turn)
    assert result is not None
    blocks = result["blocks"]

    assert blocks[0] == {"kind": "text", "text": "案例正文"}
    assert blocks[1] == {"kind": "reference", "reference_id": "ref-1", "title": "宣传.docx"}
    assert blocks[2] == {"kind": "image", "resource_id": "media-1", "title": "图.png"}

    # The text and reference blocks are exactly what the strict contract
    # accepts. An image block additionally needs a harness-signed id, which
    # this unit test has no signer for (covered by the media tests).
    from aiops_diagnostics.agent_contracts import QaBlock

    QaBlock.model_validate(blocks[0])
    QaBlock.model_validate(blocks[1])


def test_unknown_block_kind_is_left_for_the_contract_to_reject() -> None:
    """Trimming must not invent a shape for a kind we do not define."""
    from aiops_diagnostics.qa_rag import _normalize_answer_turn

    result = _normalize_answer_turn(
        {
            "kind": "answer",
            "retrieval_status": "found",
            "blocks": [{"kind": "hologram", "text": "?", "reference_id": "r"}],
        }
    )
    assert result is not None
    assert result["blocks"][0]["kind"] == "hologram"


def test_overfilled_blocks_are_trimmed_in_the_wrapped_answer_shape() -> None:
    """The shape that actually failed on 41: blocks arrive nested under
    `answer`, which used to return EARLY and skip cleaning entirely — so the
    first trim fix never ran and the same contract error survived."""
    from aiops_diagnostics.qa_rag import _normalize_answer_turn

    # Mimics the live payload: text blocks carrying `title`, and a reference
    # block echoing `text` (both forbidden combinations).
    turn = {
        "kind": "answer",
        "answer": {
            "retrieval_status": "found",
            "blocks": [
                {"kind": "text", "title": "标题", "text": "案例正文", "resource_id": "", "reference_id": ""},
                {
                    "kind": "reference",
                    "title": "案例背景与痛点",
                    "text": "案例来源：宣传.docx",
                    "reference_id": "ref-9",
                },
            ],
        },
    }
    result = _normalize_answer_turn(turn)
    assert result is not None
    blocks = result["blocks"]

    assert blocks[0] == {"kind": "text", "text": "案例正文"}
    assert blocks[1] == {"kind": "reference", "reference_id": "ref-9", "title": "案例背景与痛点"}
    # retrieval_status must survive from the wrapper.
    assert result["retrieval_status"] == "found"

    from aiops_diagnostics.agent_contracts import QaAnswer

    QaAnswer.model_validate({"blocks": blocks, "retrieval_status": "found"})


# --------------------------------------------------------------------------
# Output-language guard on the answer surfaces (#293)
#
# The customer-QA and promotional paths settle on their blocks in one place,
# so a Chinese leak is caught there for both. These tests go through the public
# harness result, not the guard's internals: what matters is what the user gets.
# --------------------------------------------------------------------------


def _client_with_one_chunk() -> _SearchClient:
    """One chunk that survives normalization, so retrieval really did succeed.

    `doc_id` is required for that: `normalize_search_response` derives the
    reference id from `chunk_id or document_id` and drops a chunk carrying
    neither, and a chunk that never reached `reference_ids` leaves the
    `found`-without-evidence downgrade to correct the model's claim.
    """
    return _SearchClient(
        [
            [
                {
                    "content": "扫码开始充电。",
                    "title": "充电桩操作",
                    "score": 0.9,
                    "chunk_id": "chunk-1",
                    "doc_id": "doc-1",
                    "reference_id": "ref-1",
                    "media": [],
                }
            ]
        ]
    )


def _video_answer(text: str, title: str) -> Any:
    return _answer(
        [
            _text_block(text),
            {
                "kind": "video",
                "text": "",
                "resource_id": "PLACEHOLDER_MEDIA",
                "reference_id": "",
                "title": title,
            },
        ],
        "found",
        cite_media=True,
    )


def test_chinese_resource_name_keeps_the_video_card(tmp_path: Path) -> None:
    """The exemption must reach the payload the surface actually delivers.

    Judged on the blocks as the frontend receives them, a Chinese resource name
    is an identifier rather than a leak: the card survives with its video block,
    and the status keeps reporting that retrieval succeeded.
    """
    session = _FakeSession(
        [
            _tool_request("无人巴士"),
            _video_answer("Here is the autonomous bus introduction video.", "新加坡无人电动巴士.mp4"),
        ]
    )

    result = _run(
        tmp_path,
        session,
        _SearchClient([[dict(_VIDEO_CHUNK_CN_NAME)]]),
        question="Show me the autonomous bus video",
        language="en",
    )

    blocks = result["blocks"]
    assert [block["kind"] for block in blocks] == ["text", "video"]
    assert blocks[0]["text"] == "Here is the autonomous bus introduction video."
    # The name the knowledge base gave the resource, on the block and on the
    # descriptor mounted beside it — unchanged, in both places.
    assert blocks[1]["title"] == "新加坡无人电动巴士.mp4"
    assert blocks[1]["media"]["title"] == "新加坡无人电动巴士.mp4"
    assert result["retrieval_status"] == "found"
    assert "language_fallback" not in result


def test_a_prose_resource_name_in_the_mounted_descriptor_is_still_judged(tmp_path: Path) -> None:
    """The judgement covers what is delivered, so it also covers the descriptor
    the harness mounts after the model's blocks are settled.

    A resource named with a sentence instead of a label is prose, so it is
    judged rather than exempted (ADR-0007 judges the value's shape, not the key
    it arrived under). The card is withheld, and the status still tells the
    truth about retrieval — the warning carries the reason instead.
    """
    session = _FakeSession(
        [
            _tool_request("无人巴士"),
            # No Chinese from the model at all: only the resource's own name.
            _video_answer("Here is the operations video.", ""),
        ]
    )

    result = _run(
        tmp_path,
        session,
        _SearchClient([[dict(_VIDEO_CHUNK_CN_PROSE_NAME)]]),
        question="Show me the operations video",
        language="en",
    )

    texts = [block["text"] for block in result["blocks"] if block["kind"] == "text"]
    assert texts == [QA_FALLBACK_MESSAGES["en"]["unavailable"]]
    assert result["retrieval_status"] == "found"
    assert "language_fallback" not in result


def test_a_card_named_only_by_its_resource_descriptor_survives(tmp_path: Path) -> None:
    """The 41 shape: the model emits no title at all, so the only Chinese in the
    payload is the resource name the harness mounts onto the block.

    The descriptor layer used to sit past the judgement point entirely — never
    judged, so the name survived by accident rather than by exemption. Judged
    where it is delivered, it is now exempt for the right reason, and the card
    keeps the video and the descriptor naming it.
    """
    session = _FakeSession(
        [
            _tool_request("无人巴士"),
            _video_answer("Here is the autonomous bus introduction video.", ""),
        ]
    )

    result = _run(
        tmp_path,
        session,
        _SearchClient([[dict(_VIDEO_CHUNK_CN_NAME)]]),
        question="Show me the autonomous bus video",
        language="en",
    )

    blocks = result["blocks"]
    assert [block["kind"] for block in blocks] == ["text", "video"]
    # The model supplied no title of its own; the name is the resource's.
    assert blocks[1].get("title", "") == ""
    assert blocks[1]["media"]["title"] == "新加坡无人电动巴士.mp4"
    assert result["retrieval_status"] == "found"
    assert "language_fallback" not in result


def test_a_real_leak_beside_an_exempt_resource_name_is_still_withheld(tmp_path: Path) -> None:
    """The exemption is per value, not per card: a Chinese text body beside a
    Chinese resource name is still a leak, and the card is withheld."""
    session = _FakeSession(
        [
            _tool_request("无人巴士"),
            _video_answer("标题: 新加坡项目；行业痛点: 土地资源有限", "新加坡无人电动巴士.mp4"),
        ]
    )

    result = _run(
        tmp_path,
        session,
        _SearchClient([[dict(_VIDEO_CHUNK_CN_NAME)]]),
        question="Show me the customer case",
        language="en",
    )

    texts = [block["text"] for block in result["blocks"] if block["kind"] == "text"]
    assert texts == [QA_FALLBACK_MESSAGES["en"]["unavailable"]]
    assert result["retrieval_status"] == "found"
    assert "language_fallback" not in result


def test_english_answer_that_leaked_chinese_is_replaced_with_localized_fallback(
    tmp_path: Path,
) -> None:
    """The prompt asked for English and the model answered in Chinese anyway."""
    session = _FakeSession(
        [
            _tool_request("充电"),
            _answer([_text_block("标题: 新加坡项目; 行业痛点: 土地资源有限")], "found"),
        ]
    )

    result = _run(
        tmp_path,
        session,
        _client_with_one_chunk(),
        question="Show me the customer case",
        language="en",
    )

    texts = [block["text"] for block in result["blocks"] if block["kind"] == "text"]
    assert texts == [QA_FALLBACK_MESSAGES["en"]["unavailable"]]
    # Retrieval SUCCEEDED here — that is why the model had Chinese to paste.
    # Reporting `unavailable` would tell the operator the knowledge base was
    # down, which is a false claim about the data source and would hide the
    # real cause. The status keeps saying what retrieval did.
    assert result["retrieval_status"] == "found"
    # No undeclared key rides along in the public payload; the warning carries
    # the reason instead.
    assert "language_fallback" not in result


def test_a_clean_english_answer_is_delivered_untouched(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _tool_request("charging"),
            _answer([_text_block("Scan the QR code to start charging.")], "found"),
        ]
    )

    result = _run(
        tmp_path,
        session,
        _client_with_one_chunk(),
        question="How do I start charging?",
        language="en",
    )

    texts = [block["text"] for block in result["blocks"] if block["kind"] == "text"]
    assert texts == ["Scan the QR code to start charging."]


def test_chinese_answer_is_never_treated_as_a_leak(tmp_path: Path) -> None:
    """zh is the default: a Chinese answer is correct, not a defect."""
    session = _FakeSession(
        [
            _tool_request("充电"),
            _answer([_text_block("标题: 新加坡项目")], "found"),
        ]
    )

    result = _run(
        tmp_path,
        session,
        _client_with_one_chunk(),
        question="给我看看客户案例",
        language="zh",
    )

    texts = [block["text"] for block in result["blocks"] if block["kind"] == "text"]
    assert texts == ["标题: 新加坡项目"]


def test_every_supported_non_chinese_language_is_guarded(tmp_path: Path) -> None:
    """A new supported language must be covered without touching this guard."""
    for language in ("en", "de", "fr", "es", "pt"):
        session = _FakeSession(
            [
                _tool_request("charging"),
                _answer([_text_block("标题: 新加坡项目")], "found"),
            ]
        )
        result = _run(
            tmp_path,
            session,
            _client_with_one_chunk(),
            question="Show me the customer case",
            language=language,
        )
        texts = [block["text"] for block in result["blocks"] if block["kind"] == "text"]
        assert texts == [QA_FALLBACK_MESSAGES[language]["unavailable"]], language
        # The payload shape stays the contract on every language.
        assert "language_fallback" not in result, language


# --------------------------------------------------------------------------
# Closing the coverage #367 asks for (#361 T5)
#
# The guard is fed the payload the surface is about to deliver, and the
# resource-name exemption is reachable by construction rather than by the caller
# picking the right Python shape. What that leaves is the reason those two facts
# can rot on their own: nothing said the two places a resource name sits are
# judged by ONE predicate, and nothing said the 41 acceptance conclusion
# (`docs/validation.md` AL-COV-10: the residual Chinese was the media block's
# `title`, a resource filename, exempt by design) still holds against the current
# code. Both are executable below, and each carries a counter-proof beside it that
# fails when the thing it pins is removed -- a guard nobody has watched turn red
# is not evidence.
# --------------------------------------------------------------------------


def _video_block(*, title: str = "") -> dict[str, Any]:
    """A video block citing this turn's signed resource, as the model emits it."""
    return {
        "kind": "video",
        "text": "",
        "resource_id": "media_sg",
        "reference_id": "",
        "title": title,
    }


def _resource(title: str) -> MediaResource:
    """The descriptor the harness mounts for the resource it signed this turn."""
    return MediaResource(
        resource_id="media_sg",
        url="/v1/media/media_sg",
        kind="video",
        mime_type="video/mp4",
        title=title,
        reference_id="",
    )


def _finalized(
    blocks: list[dict[str, Any]],
    *,
    resources: dict[str, MediaResource] | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Run `_finalize` on blocks plus this turn's authorized descriptors.

    `_finalize` is where the customer-QA and promotional paths settle, so driving
    it directly is how a test states exactly which layer carries which Chinese:
    the block's own `title` is what the model wrote, and `media.title` is what the
    harness mounted from the knowledge base. The signed resource has to be in
    `media_by_id` for the block to survive at all -- a media block citing a
    resource this turn never issued is dropped, so an absent descriptor would hide
    a verdict behind a dropped block.
    """
    retrieval = _TurnRetrieval(
        reference_ids={block["reference_id"] for block in blocks if block.get("reference_id")},
        media_by_id=resources or {},
        searches_used=1,
    )
    return _finalize({"blocks": blocks, "retrieval_status": "found"}, retrieval, language)


def _delivered(result: dict[str, Any]) -> list[str]:
    """The block kinds the user receives -- the whole card, or the withheld one."""
    return [block["kind"] for block in result["blocks"]]


@pytest.mark.parametrize(
    ("value", "exempt"),
    [
        ("新加坡无人电动巴士.mp4", True),
        ("宣传.docx", True),
        ("案例背景与痛点", True),
        ("运维记录：新加坡园区试运营.mp4", False),
    ],
    ids=["a video filename", "a document filename", "an extensionless name", "a sentence"],
)
def test_the_mounted_descriptor_is_judged_by_the_blocks_own_predicate(value: str, exempt: bool) -> None:
    """`media.title` and the block's own `title` are one rule, not two layers.

    User story 3 of the PRD: the same resource name must not be exempted as a
    block title while never being judged at all as a descriptor. The value is put
    in each place in turn and both verdicts are compared AND pinned, so a
    descriptor with an exemption of its own -- or one that skips it -- disagrees
    here rather than in production.

    The sentence case is the half that makes this a shared-PREDICATE assertion: a
    descriptor ruled on by where it sits rather than by its value would deliver
    the names and be caught by the sentence.
    """
    # A mounted descriptor that names nothing, so the block survives and the only
    # name in play is the block's own.
    nameless = {"media_sg": _resource("")}
    expected = ["text", "video"] if exempt else ["text"]

    told_on_the_block = _finalized([_text_block(_CARD_TEXT), _video_block(title=value)], resources=nameless)
    told_on_the_descriptor = _finalized(
        [_text_block(_CARD_TEXT), _video_block()], resources={"media_sg": _resource(value)}
    )

    assert _delivered(told_on_the_block) == expected
    assert _delivered(told_on_the_descriptor) == expected


def test_a_chinese_text_beside_the_descriptor_is_still_a_leak() -> None:
    """The predicate is shared, not widened: a body that leaks is still judged.

    Without this, the assertion above would also pass if the exemption had quietly
    grown from "a resource name" to "the whole card that carries one".
    """
    result = _finalized(
        [_text_block("标题: 新加坡项目"), _video_block()],
        resources={"media_sg": _resource("新加坡无人电动巴士.mp4")},
    )

    assert _delivered(result) == ["text"]
    assert result["blocks"][0]["text"] == QA_FALLBACK_MESSAGES["en"]["unavailable"]
    # A leak is a language-contract miss, not a retrieval outcome: the status
    # keeps reporting what retrieval actually did (ADR-0007). Nothing was
    # retrieved here — no chunk came back — so the truthful status is
    # `not_found`, exactly as it would be had the same card been delivered.
    assert result["retrieval_status"] == "not_found"


def test_a_withheld_card_reports_the_status_the_delivered_one_would() -> None:
    """A leak decides whether the text goes out, never what retrieval did.

    The `found`-without-evidence downgrade corrects a claim about evidence, and a
    false claim about evidence does not become true because the card was
    withheld — if anything it misleads most there, since the fallback copy beside
    it says the content is unavailable while the status says the knowledge backing
    it was found. Deciding the status twice, once per branch, is how the two came
    to disagree: the same answer reported `found` when it leaked and `not_found`
    when it did not.
    """
    delivered = _finalized([_text_block(_CARD_TEXT)])
    withheld = _finalized([_text_block("标题: 新加坡项目")])

    assert _delivered(withheld) == ["text"]
    assert withheld["blocks"][0]["text"] == QA_FALLBACK_MESSAGES["en"]["unavailable"]
    assert withheld["retrieval_status"] == delivered["retrieval_status"] == "not_found"


def test_an_outage_is_still_reported_as_one_on_the_withheld_card() -> None:
    """Withholding does not swallow the outage correction.

    A knowledge-base outage is something the dependency did, not something the
    model did, so the withheld card reports it — and reports it as the contract's
    value rather than as the enum object, which is what the delivered card sends.
    """
    retrieval = _TurnRetrieval(
        reference_ids=set(),
        media_by_id={},
        searches_used=1,
        last_status=RetrievalStatus.UNAVAILABLE,
    )
    withheld = _finalize(
        {"blocks": [_text_block("标题: 新加坡项目")], "retrieval_status": "found"}, retrieval, "en"
    )

    assert withheld["blocks"][0]["text"] == QA_FALLBACK_MESSAGES["en"]["unavailable"]
    assert withheld["retrieval_status"] == RetrievalStatus.UNAVAILABLE.value
    assert type(withheld["retrieval_status"]) is str


def test_the_sentence_in_the_descriptor_is_withheld_because_the_descriptor_is_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counter-proof for the sentence case: drop the descriptor out of the
    contract payload and there is no Chinese left to judge, so the card goes out.

    That is exactly what a judgement running before the mount saw -- the
    descriptor layer sat past the guard's reach entirely, so a sentence named as
    a resource went out unjudged while the same sentence in a block title was
    judged. If the sentence assertion above ever passes for the wrong reason,
    this one goes the other way.
    """
    monkeypatch.setattr(answer_language, "_media_title_of", lambda block: "")

    result = _finalized(
        [_text_block(_CARD_TEXT), _video_block()],
        resources={"media_sg": _resource("运维记录：新加坡园区试运营.mp4")},
    )

    assert _delivered(result) == ["text", "video"]


def _bus_card(title: str) -> list[dict[str, Any]]:
    """The 41 card: the prose, the video it shows, and the chunk it came from."""
    return [
        _text_block(_CARD_TEXT),
        {
            "kind": "video",
            "text": "",
            "resource_id": "PLACEHOLDER_MEDIA",
            "reference_id": "",
            "title": title,
        },
        {
            "kind": "reference",
            "text": "",
            "resource_id": "",
            "reference_id": "chunk-sg-bus",
            "title": title,
        },
    ]


def _document_card(title: str) -> list[dict[str, Any]]:
    """The 41 case card: the prose and the document it was assembled from."""
    return [
        _text_block(
            "TrendPower, Huawei and BYD delivered Singapore's first national-level autonomous bus project."
        ),
        {
            "kind": "reference",
            "text": "",
            "resource_id": "",
            "reference_id": "chunk-promo-docx",
            "title": title,
        },
    ]


def _run_card(
    tmp_path: Path,
    chunk: dict[str, Any],
    blocks: list[dict[str, Any]],
    *,
    cite_media: bool,
    question: str,
) -> dict[str, Any]:
    """Drive the real harness over one scripted card and one search result."""
    session = _FakeSession([_tool_request("无人巴士"), _answer(blocks, "found", cite_media=cite_media)])
    return _run(tmp_path, session, _SearchClient([[dict(chunk)]]), question=question, language="en")


def test_the_41_english_card_keeps_its_chinese_resource_names(tmp_path: Path) -> None:
    """`docs/validation.md` AL-COV-10 recorded the residual Chinese in 41's
    English card as the media block's `title` -- `新加坡无人电动巴士.mp4` -- a
    resource filename, exempt by design. The current code must agree with that
    record, so the same card asked for in English is delivered whole rather than
    withheld as a leak.

    The name rides in three places -- the block's own title, the descriptor
    mounted beside it, and the citation naming the chunk it came from -- and all
    three survive.
    """
    result = _run_card(
        tmp_path,
        _VIDEO_CHUNK_CN_NAME,
        _bus_card("新加坡无人电动巴士.mp4"),
        cite_media=True,
        question="Show me the autonomous bus video",
    )

    blocks = result["blocks"]
    assert [block["kind"] for block in blocks] == ["text", "video", "reference"]
    # The names the knowledge base issued, unchanged everywhere they are carried.
    assert blocks[1]["title"] == "新加坡无人电动巴士.mp4"
    assert blocks[1]["media"]["title"] == "新加坡无人电动巴士.mp4"
    assert blocks[2]["title"] == "新加坡无人电动巴士.mp4"
    assert result["retrieval_status"] == "found"
    assert "language_fallback" not in result


def test_the_41_english_card_keeps_its_chinese_reference_document(tmp_path: Path) -> None:
    """The other recorded resource name: the source document, which the library
    stores as `宣传.docx`. A reader has to be able to match the card's citation
    back to the library, so the reference block keeps it rather than being
    translated into something that matches nothing.
    """
    result = _run_card(
        tmp_path,
        _DOCX_CHUNK,
        _document_card("宣传.docx"),
        cite_media=False,
        question="Show me the customer case",
    )

    blocks = result["blocks"]
    assert [block["kind"] for block in blocks] == ["text", "reference"]
    assert blocks[1]["title"] == "宣传.docx"
    assert result["retrieval_status"] == "found"
    assert "language_fallback" not in result


_real_from_public_blocks = answer_language.AnswerSurface.from_public_blocks


class _NoRetrievedTitles:
    """Stand-in for ``from_public_blocks`` with the run's provenance withheld.

    A classmethod, matching the method it replaces, so the binding is the same.
    """

    @classmethod
    def from_public_blocks(cls, blocks, **kwargs):
        kwargs.pop("retrieved_titles", None)
        return _real_from_public_blocks(blocks, **kwargs)


@pytest.mark.parametrize(
    ("chunk", "blocks", "cite_media", "question"),
    [
        (
            _VIDEO_CHUNK_CN_NAME,
            _bus_card("新加坡无人电动巴士.mp4"),
            True,
            "Show me the autonomous bus video",
        ),
        (_DOCX_CHUNK, _document_card("宣传.docx"), False, "Show me the customer case"),
    ],
    ids=["the video resource", "the cited document"],
)
def test_the_recorded_cards_are_carried_by_the_exemption_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chunk: dict[str, Any],
    blocks: list[dict[str, Any]],
    cite_media: bool,
    question: str,
) -> None:
    """Counter-proof: turn the resource-name exemption off and both recorded
    cards are withheld.

    Without this, the two assertions above would also pass if the exemption had
    quietly stopped being what carries them -- the same failure mode as the guards
    this repository has already had to replace. Judging a resource name as prose
    is exactly what a shape-blind, leaf-by-leaf payload did to the video card.
    """
    # #376: the exemption is a CONJUNCTION -- a title is a resource name only
    # when it both looks like one and was actually retrieved this run. Disabling
    # either half alone no longer withdraws it (an absent provenance falls back
    # to the shape rule), so the counter-proof disables both: that is what "turn
    # the exemption off" now means. Judging a resource name as prose is still
    # exactly what a shape-blind payload did to the video card.
    monkeypatch.setattr(answer_language, "looks_like_asset_name", lambda value: False)
    monkeypatch.setattr(
        answer_language.AnswerSurface,
        "from_public_blocks",
        _NoRetrievedTitles.from_public_blocks,
    )

    result = _run_card(tmp_path, chunk, blocks, cite_media=cite_media, question=question)

    texts = [block["text"] for block in result["blocks"] if block["kind"] == "text"]
    assert texts == [QA_FALLBACK_MESSAGES["en"]["unavailable"]]
    # The status still tells the truth about retrieval; only the alert says why.
    assert result["retrieval_status"] == "found"
    assert "language_fallback" not in result
