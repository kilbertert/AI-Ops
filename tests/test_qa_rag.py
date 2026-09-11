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

from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.codex_runtime import AgentRuntimeError, CodexTurnOutput
from aiops_diagnostics.config import AgentSettings
from aiops_diagnostics.knowledge_retrieval import (
    KnowledgeSearchUnavailable,
    MediaResourceSigner,
)
from aiops_diagnostics.qa_rag import (
    CustomerAgentSelection,
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
