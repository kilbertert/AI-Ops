from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from aiops_diagnostics.agent_debug import (
    KbBindingResolver,
    KbDocumentState,
    run_agent_debug_answer,
)
from aiops_diagnostics.agent_lifecycle import (
    AgentManager,
    AgentPublishError,
    AgentStore,
)
from aiops_diagnostics.codex_runtime import CodexTurnOutput
from aiops_diagnostics.config import AgentSettings
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.knowledge_retrieval import MediaResourceSigner
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeKbClient:
    """Scripted kb-service GET client: per-kb detail/documents or raises."""

    def __init__(
        self,
        *,
        detail: dict[str, Any] | Exception | None = None,
        docs: dict[str, Any] | Exception | None = None,
    ) -> None:
        self._detail = detail if detail is not None else {}
        self._docs = docs if docs is not None else {"docs": []}
        self.calls: list[str] = []
        self.tenants: list[str] = []

    def for_tenant(self, tenant_id: str) -> _FakeKbClient:
        self.tenants.append(tenant_id)
        return self

    def knowledge_base(self, kb_id: str) -> dict[str, Any]:
        self.calls.append(f"detail:{kb_id}")
        if isinstance(self._detail, Exception):
            raise self._detail
        return self._detail

    def documents(self, kb_id: str, *, page_size: int = 100) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(f"docs:{kb_id}")
        if isinstance(self._docs, Exception):
            raise self._docs
        return self._docs


class _FakeSearchClient:
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
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.closed = False

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


_IMAGE_CHUNK: dict[str, Any] = {
    "knowledge_base_id": "kb-a",
    "chunk_id": "chk-1",
    "content_with_weight": "把充电枪拔出前先停止充电。",
    "doc_id": "doc-1",
    "docnm_kwd": "拔枪示意.png",
    "image_id": "img-1",
    "mime_type": "image/png",
    "doc_type_kwd": "image",
    "similarity": 0.9,
}


def _text_block(text: str) -> dict[str, Any]:
    return {"kind": "text", "text": text, "resource_id": "", "reference_id": "", "title": ""}


def _answer(blocks: list[dict[str, Any]], status: str, *, cite_media: bool = False) -> Any:
    payload = json.dumps(
        {"kind": "answer", "tool_requests": [], "answer": {"blocks": blocks, "retrieval_status": status}},
        ensure_ascii=False,
    )
    return {"cite_media": True, "payload": payload} if cite_media else payload


def _tool_request(query: str = "拔枪步骤") -> str:
    return json.dumps(
        {
            "kind": "tool_requests",
            "tool_requests": [{"tool": "knowledge_search", "reason": "需要业务资料", "query": query}],
            "answer": None,
        },
        ensure_ascii=False,
    )


# ── KbBindingResolver ──────────────────────────────────────────────────


def test_resolver_accepts_done_documents() -> None:
    client = _FakeKbClient(docs={"docs": [{"run": "DONE"}, {"run": "DONE"}]})
    KbBindingResolver(client).validate("tenant-a", ("kb-a",))
    assert client.calls == ["detail:kb-a", "docs:kb-a"]
    assert client.tenants == ["tenant-a"]  # rebinding to the publishing tenant


def test_resolver_names_parsing_and_failed_documents() -> None:
    parsing = _FakeKbClient(docs={"docs": [{"run": "RUNNING"}]})
    with_error = KbBindingResolver(parsing)
    try:
        with_error.validate("tenant-a", ("kb-a",))
    except AgentPublishError as exc:
        assert "解析中" in str(exc)
    else:
        raise AssertionError("parsing documents must block publish")

    failed = _FakeKbClient(docs={"docs": [{"run": "DONE"}, {"run": "FAIL"}]})
    try:
        KbBindingResolver(failed).validate("tenant-a", ("kb-a",))
    except AgentPublishError as exc:
        assert "解析失败" in str(exc)
    else:
        raise AssertionError("failed documents must block publish")


def test_resolver_reports_missing_or_cross_tenant_base() -> None:
    from aiops_diagnostics.knowledge_retrieval import KnowledgeSearchUnavailable

    missing = _FakeKbClient(detail=KnowledgeSearchUnavailable("kb-service returned HTTP 502"))
    try:
        KbBindingResolver(missing).validate("tenant-a", ("kb-other",))
    except AgentPublishError as exc:
        assert "不存在" in str(exc)
    else:
        raise AssertionError("a base kb-service cannot find must block publish")


def test_resolver_fails_closed_when_kb_service_unreachable() -> None:
    from aiops_diagnostics.knowledge_retrieval import KnowledgeSearchUnavailable

    down = _FakeKbClient(detail=KnowledgeSearchUnavailable("kb-service is unavailable"))
    try:
        KbBindingResolver(down).validate("tenant-a", ("kb-a",))
    except AgentPublishError as exc:
        assert "不可用" in str(exc)
    else:
        raise AssertionError("an unreachable kb-service must block publish")


def test_resolver_empty_binding_needs_no_calls() -> None:
    client = _FakeKbClient()
    KbBindingResolver(client).validate("tenant-a", ())
    assert client.calls == []


def test_kb_document_state_reasons() -> None:
    assert KbDocumentState(kb_id="kb-a").reason() is None
    assert KbDocumentState(kb_id="kb-a", exists=False).reason() is not None
    assert KbDocumentState(kb_id="kb-a", parsing=True).reason() is not None
    assert KbDocumentState(kb_id="kb-a", failed=True).reason() is not None


# ── run_agent_debug_answer ──────────────────────────────────────────────


def _debug_run(tmp_path: Path, session: _FakeSession, client: _FakeSearchClient) -> dict[str, Any]:
    return run_agent_debug_answer(
        "怎么拔枪",
        agent_id="agt_test",
        revision=3,
        prompt="用简体中文回答充电业务问题。",
        knowledge_base_ids=("kb-a",),
        agent_settings=AgentSettings(codex_bin="/bin/true", run_root=str(tmp_path / "runs")),
        search_client=client,
        media_signer=MediaResourceSigner("test-secret", ttl_seconds=60),
        tenant_id="tenant-a",
        project_root=_PROJECT_ROOT,
        session_factory=lambda *_args, **_kw: session,
    )


def test_debug_run_returns_blocks_with_draft_marker(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _tool_request(),
            _answer(
                [_text_block("先停止充电，再拔枪。")],
                "found",
            ),
        ]
    )
    result = _debug_run(tmp_path, session, _FakeSearchClient([[dict(_IMAGE_CHUNK)]]))
    assert result["debug"] is True
    assert result["agent_version"] == "agt_test#draft-r3"
    assert result["retrieval_status"] == "found"
    assert any(block["kind"] == "text" for block in result["blocks"])


def test_debug_run_carries_media_blocks(tmp_path: Path) -> None:
    session = _FakeSession(
        [
            _tool_request(),
            _answer(
                [
                    _text_block("看图操作。"),
                    {
                        "kind": "image",
                        "text": "",
                        "resource_id": "PLACEHOLDER_MEDIA",
                        "reference_id": "chk-1",
                        "title": "操作手册.pdf",
                    },
                ],
                "found",
                cite_media=True,
            ),
        ]
    )
    result = _debug_run(tmp_path, session, _FakeSearchClient([[dict(_IMAGE_CHUNK)]]))
    image_blocks = [block for block in result["blocks"] if block["kind"] == "image"]
    assert image_blocks, "an authorized image chunk must yield an image block"
    assert image_blocks[0]["media"]["url"].startswith("/v1/media/media_")


# ── /v1/agents/{id}/debug-run API ───────────────────────────────────────


class _Knowledge:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids
        if self.blocked:
            raise AgentPublishError("knowledge base is still parsing")


class _Resolver:
    def __init__(self, context: ScopeContext) -> None:
        self.context = context

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del token, required_scope, third_session
        return self.context


class _Runtime:
    def shutdown(self) -> None:
        pass

    # debug-run dependency surface
    def run_agent_debug(self, context, agent_id: str, question: str) -> dict[str, Any]:
        del context
        self.calls.append((agent_id, question))
        return {
            "blocks": [_text_block("预览回答")],
            "retrieval_status": "found",
            "agent_version": f"{agent_id}#draft-r1",
            "debug": True,
        }

    calls: list[tuple[str, str]] = []


def _context(
    tenant: str = "tenant-a", roles: frozenset[str] = frozenset({"ROLE_AGENT_ADMIN"})
) -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", tenant_id=tenant)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="self"),
        roles=roles,
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _app(tmp_path: Path, *, roles: frozenset[str] | None = None, runtime: Any = None) -> TestClient:
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = AgentStore(settings.database_file)
    manager = AgentManager(store, knowledge_resolver=_Knowledge())
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime or _Runtime(),  # type: ignore[arg-type]
        caller_resolver=_Resolver(_context(roles=roles) if roles else _context()),
        agent_manager=manager,
    )
    return TestClient(app)


def _create_agent(client: TestClient) -> dict[str, Any]:
    payload = {
        "name": "客服",
        "description": "",
        "agent_type": "customer",
        "prompt": "回答业务问题",
        "knowledge_base_ids": ["kb-a"],
        "model": "aiops-api",
        "output_contract": "blocks-v1",
        "opening_questions": [],
        "quick_commands": [],
    }
    created = client.post("/v1/agents", headers={"Authorization": "Bearer token"}, json=payload)
    assert created.status_code == 201, created.text
    return created.json()


def test_debug_run_endpoint_runs_draft_and_returns_preview(tmp_path: Path) -> None:
    runtime = _Runtime()
    with _app(tmp_path, runtime=runtime) as client:
        agent = _create_agent(client)
        response = client.post(
            f"/v1/agents/{agent['agent_id']}/debug-run",
            headers={"Authorization": "Bearer token"},
            json={"question": "怎么拔枪"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["debug"] is True
        assert body["agent_version"] == f"{agent['agent_id']}#draft-r1"
        assert any(block["kind"] == "text" for block in body["blocks"])
        assert runtime.calls == [(agent["agent_id"], "怎么拔枪")]


def test_debug_run_endpoint_requires_edit_role(tmp_path: Path) -> None:
    runtime = _Runtime()
    with _app(tmp_path, roles=frozenset({"ROLE_AGENT_VIEWER"}), runtime=runtime) as client:
        agent = _create_agent(client) if False else None
        # viewer cannot even create; use the same DB from an admin client
    admin_client_app = _app(tmp_path)
    with admin_client_app as client:
        agent = _create_agent(client)
    viewer = _app(tmp_path, roles=frozenset({"ROLE_AGENT_VIEWER"}), runtime=runtime)
    with viewer as client:
        response = client.post(
            f"/v1/agents/{agent['agent_id']}/debug-run",
            headers={"Authorization": "Bearer token"},
            json={"question": "怎么拔枪"},
        )
        assert response.status_code == 403


def test_debug_run_endpoint_cross_tenant_is_404(tmp_path: Path) -> None:
    runtime = _Runtime()
    with _app(tmp_path / "a", runtime=runtime) as client:
        agent = _create_agent(client)

    # Same database, but the caller resolves to another tenant: the manager
    # scope makes the agent indistinguishable from missing (404, no leak).
    settings = GatewayServerSettings(
        data_home=tmp_path / "a",
        database_file=tmp_path / "a" / "gateway.db",
        server_config_file=tmp_path / "a" / "production.env",
    )
    manager = AgentManager(AgentStore(settings.database_file), knowledge_resolver=_Knowledge())
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Resolver(_context(tenant="tenant-b")),
        agent_manager=manager,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/v1/agents/{agent['agent_id']}/debug-run",
            headers={"Authorization": "Bearer token"},
            json={"question": "怎么拔枪"},
        )
        assert response.status_code == 404
