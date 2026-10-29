from __future__ import annotations

from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_debug import (
    KbBindingResolver,
    KbDocumentState,
)
from aiops_diagnostics.agent_lifecycle import (
    AgentPublishError,
)

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
