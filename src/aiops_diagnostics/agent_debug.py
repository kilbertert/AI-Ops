"""Draft debug-run and real knowledge-binding validation (T5/#171).

Two pieces share this module:

- ``KbBindingResolver`` — the real ``KnowledgeBindingResolver`` for publish
  validation. It asks kb-service per knowledge base: tenant mismatches and
  missing bases surface as "不存在", documents still parsing or failed
  surface as their own reasons (#171 acceptance: publish errors must name
  the cause). An unreachable kb-service fails closed.
- ``run_agent_debug_answer`` — an isolated preview of a draft agent config
  through the exact production customer-QA harness (``run_customer_qa_answer``)
  with a draft-scoped selection. It never creates a conversation turn and
  never touches order data; media grants are tagged with a ``debug-`` session
  id so debug previews stay distinguishable from production sessions.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_lifecycle import (
    AgentPublishError,
    KnowledgeBindingResolver,
)
from aiops_diagnostics.config import AgentSettings, ProviderConfig
from aiops_diagnostics.knowledge_retrieval import (
    KnowledgeSearchClient,
    KnowledgeSearchUnavailable,
    MediaResourceSigner,
)
from aiops_diagnostics.qa_rag import CustomerAgentSelection, run_customer_qa_answer

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
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, headers={"tenant-id": self.tenant_id}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                status_code = response.status
        except urllib.error.HTTPError as exc:
            # kb-service turns RAGFlow failures (missing base, cross-tenant
            # access, invalid id) into 502 upstream_error responses.
            raise KnowledgeSearchUnavailable(f"kb-service returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
            raise KnowledgeSearchUnavailable("kb-service is unavailable") from exc
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise KnowledgeSearchUnavailable("kb-service returned an invalid response") from exc
        if status_code != 200 or not isinstance(payload, dict):
            raise KnowledgeSearchUnavailable(f"kb-service returned HTTP {status_code}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise KnowledgeSearchUnavailable("kb-service returned an invalid response")
        return data


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


def run_agent_debug_answer(
    question: str,
    *,
    agent_id: str,
    revision: int,
    prompt: str,
    knowledge_base_ids: tuple[str, ...],
    agent_settings: AgentSettings,
    search_client: KnowledgeSearchClient,
    media_signer: MediaResourceSigner,
    tenant_id: str,
    provider: ProviderConfig | None = None,
    key_slot: str | None = None,
    project_root: Path | None = None,
    session_factory: Any | None = None,
) -> dict[str, Any]:
    """Run one isolated preview turn of a draft customer agent config.

    Same harness, same guard, same blocks-v1 contract as production
    ``run_customer_qa_answer``; only the selection is draft-scoped: the
    result and media grants carry ``#draft-r<revision>`` so a debug preview
    never claims a published version. No conversation turn is created and no
    order path is touched.
    """
    selection = CustomerAgentSelection(
        agent_id=agent_id,
        version_no=revision,
        prompt=prompt,
        knowledge_base_ids=tuple(dict.fromkeys(knowledge_base_ids)),
    )
    result = run_customer_qa_answer(
        question,
        selection,
        agent_settings,
        search_client=search_client,
        media_signer=media_signer,
        tenant_id=tenant_id,
        provider=provider,
        key_slot=key_slot,
        project_root=project_root,
        session_factory=session_factory,
    )
    result["agent_version"] = f"{agent_id}#draft-r{revision}"
    result["debug"] = True
    return result
