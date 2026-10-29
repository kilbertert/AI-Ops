"""发布前核对知识库归属、文档解析状态与可用性。"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.agent_lifecycle import (
    AgentPublishError,
    KnowledgeBindingResolver,
)
from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    parse_data_key_envelope,
    request_json,
    tenant_id_header,
)
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KnowledgeSearchUnavailable,
)

# RAGFlow TaskStatus names surfaced by kb-service document lists
# (common/constants.py: UNSTART/RUNNING/CANCEL/DONE/FAIL/SCHEDULE).
_PARSING_RUNS = frozenset({"UNSTART", "RUNNING", "SCHEDULE", "CANCEL"})
_FAILED_RUNS = frozenset({"FAIL"})


@dataclass(frozen=True, slots=True)
class KbDocumentState:
    """Aggregated kb-service/RAGFlow view of one knowledge base."""

    kb_id: str
    exists: bool = True
    parsing: bool = False
    failed: bool = False
    documents: int = 0

    def reason(self) -> str | None:
        """The publish blocker for this base, or None when publishable."""
        if not self.exists:
            return f"知识库 {self.kb_id} 不存在或不在当前租户下"
        if self.parsing:
            return f"知识库 {self.kb_id} 仍有文档解析中，请稍后发布"
        if self.failed:
            return f"知识库 {self.kb_id} 存在解析失败的文档，请处理后发布"
        return None


class KbServiceKnowledgeClient:
    """Minimal GET surface of kb-service needed for binding validation.

    Mirrors ``KbServiceClient``'s transport discipline (stdlib, tenant header,
    bounded timeout) but for the GET endpoints kb-service already exposes:
    ``GET /kb/knowledge-bases/{kb}`` and ``GET /kb/knowledge-bases/{kb}/documents``.
    """

    def __init__(self, base_url: str, *, tenant_id: str, timeout: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.timeout = timeout

    def for_tenant(self, tenant_id: str) -> KbServiceKnowledgeClient:
        """A copy bound to another tenant header (same base/timeout).

        The gateway builds one client per process; publish validation rebinds
        to the publishing tenant so RAGFlow's per-tenant dataset access check
        is meaningful.
        """
        return KbServiceKnowledgeClient(self.base_url, tenant_id=tenant_id, timeout=self.timeout)

    def knowledge_base(self, kb_id: str) -> dict[str, Any]:
        return self._get(f"/kb/knowledge-bases/{urllib.parse.quote(kb_id, safe='')}")

    def documents(self, kb_id: str, *, page_size: int = 100) -> dict[str, Any]:
        path = f"/kb/knowledge-bases/{urllib.parse.quote(kb_id, safe='')}/documents"
        return self._get(path, query=f"page=1&page_size={page_size}")

    def _get(self, path: str, *, query: str = "") -> dict[str, Any]:
        # kb-service turns RAGFlow failures (missing base, cross-tenant access,
        # invalid id) into 502 upstream_error responses; every failure collapses
        # onto one message here, so the mapping does the same.
        def _unavailable(_failure: HttpFailure) -> Exception:
            return KnowledgeSearchUnavailable("kb-service is unavailable")

        def _http_error(failure: HttpFailure) -> Exception:
            return KnowledgeSearchUnavailable(f"kb-service returned HTTP {failure.status}")

        # A non-200 that still decoded, and a parse failure, keep their own
        # wording: the skeleton separates those from transport faults, which is
        # exactly the distinction this client already drew by hand.
        url = join_url(self.base_url, path)
        if query:
            url = f"{url}?{query}"
        payload = request_json(
            RequestSpec(
                url=url,
                method="GET",
                headers=tenant_id_header(self.tenant_id),
                timeout=self.timeout,
            ),
            mapping=ErrorMapping(
                auth_rejected=_http_error,
                http_error=_http_error,
                unavailable=_unavailable,
                invalid_body=lambda _f: KnowledgeSearchUnavailable("kb-service returned an invalid response"),
                invalid_envelope=lambda _f: KnowledgeSearchUnavailable(
                    "kb-service returned an invalid response"
                ),
            ),
            envelope=parse_data_key_envelope,
        )
        if not isinstance(payload, dict):
            raise KnowledgeSearchUnavailable("kb-service returned an invalid response")
        return payload


class KbBindingResolver(KnowledgeBindingResolver):
    """Validate draft knowledge bindings against live kb-service state."""

    def __init__(self, client: KbServiceKnowledgeClient) -> None:
        self.client = client

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        if not knowledge_base_ids:
            return
        # Rebind to the publishing tenant so RAGFlow's per-tenant dataset
        # access check (kb-service tenant header → tenancy.db → ragflow token)
        # decides existence; cross-tenant and missing bases are equally
        # "不存在" to the caller.
        client = self.client.for_tenant(tenant_id)
        for kb_id in knowledge_base_ids:
            state = self.inspect(client, kb_id)
            reason = state.reason()
            if reason is not None:
                raise AgentPublishError(reason)

    def inspect(self, client: KbServiceKnowledgeClient, kb_id: str) -> KbDocumentState:
        try:
            client.knowledge_base(kb_id)
        except KnowledgeSearchUnavailable as exc:
            # kb-service relays RAGFlow failures (missing base, cross-tenant
            # access, invalid id) as 502 upstream_error; a down stack raises
            # before any status exists. Both are publish blockers — a base we
            # cannot positively verify is never publishable — but the message
            # differs so the admin page can name the cause (#171 acceptance).
            if "HTTP " in str(exc):
                return KbDocumentState(kb_id=kb_id, exists=False)
            raise AgentPublishError("kb-service 不可用，无法校验知识库") from exc
        runs: set[str] = set()
        try:
            listing = client.documents(kb_id)
        except KnowledgeSearchUnavailable as exc:
            raise AgentPublishError("kb-service 不可用，无法校验知识库") from exc
        docs = listing.get("docs")
        if isinstance(docs, list):
            for doc in docs:
                if isinstance(doc, dict):
                    runs.add(str(doc.get("run") or "").upper())
        return KbDocumentState(
            kb_id=kb_id,
            parsing=bool(runs & _PARSING_RUNS),
            failed=bool(runs & _FAILED_RUNS),
            documents=len(docs) if isinstance(docs, list) else 0,
        )
