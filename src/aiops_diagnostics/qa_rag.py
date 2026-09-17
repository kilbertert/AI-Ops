"""Customer QA RAG runtime (T3/#170).

Runs a published customer agent in the Codex harness with the bounded
``knowledge_search`` tool (T1/#168) and returns the blocks-v1 contract
(``blocks[]`` + ``retrieval_status``) for the unified assistant ``qa`` path.

The harness — not the model — owns tenant, published agent version, knowledge
base allow-list, call limits, and media authorization.  Codex may only submit
query text; when a business question needs knowledge but the model skipped
retrieval, the harness asks for one supplementary search (two calls total,
enforced by ``KnowledgeSearchGuard``).
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aiops_diagnostics.agent_contracts import (
    QaAnswer,
    QaBlock,
    qa_rag_turn_schema,
)
from aiops_diagnostics.agent_lifecycle import AgentStore
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import AgentContractError, SDKCodexSession
from aiops_diagnostics.config import AgentSettings, ProviderConfig
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, QA_FALLBACK_MESSAGES, language_name
from aiops_diagnostics.knowledge_retrieval import (
    KNOWLEDGE_SEARCH_TOOL,
    KnowledgeSearchClient,
    KnowledgeSearchGuard,
    MediaResource,
    MediaResourceSigner,
    RetrievalStatus,
)

_MAX_RUNS = 5  # initial turn + ≤2 search rounds + forced re-search + final
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


@dataclass(frozen=True, slots=True)
class CustomerAgentSelection:
    """The published customer agent version that will serve this question."""

    agent_id: str
    version_no: int
    prompt: str
    knowledge_base_ids: tuple[str, ...]


def select_customer_agent(
    store: AgentStore,
    tenant_id: str,
) -> CustomerAgentSelection | None:
    """Return the newest published customer agent for the tenant, or None.

    The store is tenant-scoped: a missing or cross-tenant agent is
    indistinguishable (absent), so no existence is leaked. Disabled agents and
    drafts are never selected; agents without knowledge bases cannot serve
    the RAG path. Agents pinned as a published shortcut's promotional target
    (#231) never serve plain customer QA — that path belongs to the
    customer-service agent; a promotional agent answers only via the
    promotional route.
    """
    from aiops_diagnostics.agent_lifecycle import AgentNotFound

    promo_targets = _pinned_promo_agents(store, tenant_id)
    try:
        agents = store.list(tenant_id)
    except Exception:  # store unavailable — fall back to the zero-order path
        return None
    for agent in agents:
        if agent.agent_id in promo_targets:
            continue
        if agent.status != "published" or agent.published_version is None:
            continue
        if agent.status != "published" or agent.published_version is None:
            continue
        try:
            version = store.version(agent.agent_id, tenant_id, agent.published_version)
        except AgentNotFound:
            continue
        snapshot = version.snapshot
        if str(snapshot.get("agent_type")) != "customer":
            continue
        kb_ids = tuple(str(item) for item in (snapshot.get("knowledge_base_ids") or ()) if str(item).strip())
        if not kb_ids:
            continue
        return CustomerAgentSelection(
            agent_id=agent.agent_id,
            version_no=version.version_no,
            prompt=str(snapshot.get("prompt") or ""),
            knowledge_base_ids=kb_ids,
        )
    return None


def _pinned_promo_agents(store: AgentStore, tenant_id: str) -> frozenset[str]:
    """Agent ids pinned by any published shortcut as a promotional target.

    #231 follow-up: a promotional agent is distinguishable from the
    customer-service agent by its shortcut pin, so no extra agent field is
    needed. Store failures degrade to the empty set (old selection behavior).
    """
    from aiops_diagnostics.shortcut_lifecycle import ShortcutStore

    try:
        rows = ShortcutStore(store.path).list_published(tenant_id, "consumer")
    except Exception:  # noqa: BLE001 - absent shortcut table keeps old behavior
        return frozenset()
    pinned = set()
    for row in rows:
        target = row.target_agent_version or ""
        if target.startswith("agt_"):
            pinned.add(target.partition("#")[0])
    return frozenset(pinned)


@dataclass(slots=True)
class _TurnRetrieval:
    """Authorized ids and media resources collected from this turn's searches."""

    reference_ids: set[str] = field(default_factory=set)
    media_by_id: dict[str, MediaResource] = field(default_factory=dict)
    last_status: RetrievalStatus | None = None
    searches_used: int = 0


def run_customer_qa_answer(
    question: str,
    selection: CustomerAgentSelection,
    agent_settings: AgentSettings,
    *,
    search_client: KnowledgeSearchClient,
    media_signer: MediaResourceSigner,
    tenant_id: str,
    provider: ProviderConfig | None = None,
    key_slot: str | None = None,
    project_root: Path | None = None,
    session_factory: Any | None = None,
    language: str = DEFAULT_LANGUAGE,
    initial_prompt: str | None = None,
) -> dict[str, Any]:
    """Answer one customer question via the published agent + bounded retrieval.

    Returns the public QA result dict: ``{"blocks": [...], "retrieval_status":
    ...}`` where media blocks carry the signed media descriptor for this turn.
    Raises ``AgentRuntimeError`` on harness-level failures (the caller maps
    that to a failed job). ``initial_prompt`` (#231) replaces the built-in
    customer prompt when the promotional agent serves the run — same harness
    contract, different instructions and knowledge bases.
    """
    root = project_root or Path(__file__).resolve().parents[1]
    selected_provider = agent_settings.select_provider(provider.name if provider else None)
    workspace = AgentWorkspace.create_qa(
        root,
        Path(agent_settings.run_root).expanduser().resolve(),
        provider_base_url=selected_provider.base_url,
        provider=selected_provider.name,
        key_slot=key_slot or selected_provider.resolved_key_slot(),
    )
    guard = KnowledgeSearchGuard(
        search_client,
        tenant_id=tenant_id,
        agent_version=f"{selection.agent_id}#v{selection.version_no}",
        knowledge_base_ids=selection.knowledge_base_ids,
        media_signer=media_signer,
        session_id=workspace.run_id,
    )
    retrieval = _TurnRetrieval()
    session: SDKCodexSession | None = None
    try:
        if session_factory is not None:
            session = session_factory(workspace, agent_settings, selected_provider, None)
        else:
            session = SDKCodexSession(workspace, agent_settings, provider=selected_provider)
        prompt = initial_prompt or _initial_prompt(selection, question, language)
        searched = False
        for _ in range(_MAX_RUNS):
            output = session.run(prompt, output_schema=qa_rag_turn_schema())
            turn = _parse_rag_turn(output.final_response)
            if turn is None:
                raise AgentContractError("customer QA turn returned invalid JSON")
            if turn.get("kind") == "tool_requests":
                requests = turn.get("tool_requests") or []
                empty_request = not requests
                if not requests:
                    if searched:
                        return _fallback_result(
                            retrieval.last_status,
                            searches=retrieval.searches_used,
                            language=language,
                        )
                    # A few providers emit the tool discriminator without a
                    # payload. Use the user's question as a bounded fallback
                    # query once, then keep the normal answer contract.
                    requests = [
                        {
                            "tool": KNOWLEDGE_SEARCH_TOOL,
                            "query": question,
                            "reason": "model emitted an empty tool request",
                        }
                    ]
                results = _execute_searches(guard, retrieval, requests)
                searched = True
                if empty_request and retrieval.last_status in {
                    RetrievalStatus.NOT_FOUND,
                    RetrievalStatus.UNAVAILABLE,
                }:
                    return _fallback_result(
                        retrieval.last_status,
                        searches=retrieval.searches_used,
                        language=language,
                    )
                prompt = _search_results_prompt(results, language)
                continue
            answer = _normalize_answer_turn(turn)
            if answer is None:
                raise AgentContractError("answer turn carried no answer payload")
            # Business question but the model skipped retrieval: force one
            # supplementary search before accepting the answer (T1/T3 rule).
            if not searched and _needs_retrieval(question):
                prompt = _forced_search_prompt(question, language)
                continue
            return _finalize(answer, retrieval)
        return _fallback_result(language=language)
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()


def _execute_searches(
    guard: KnowledgeSearchGuard,
    retrieval: _TurnRetrieval,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for request in requests:
        if str(request.get("tool")) != KNOWLEDGE_SEARCH_TOOL:
            payloads.append({"tool": str(request.get("tool")), "error": "unsupported tool"})
            continue
        query = str(request.get("query") or request.get("reason") or "")
        try:
            result = guard.search(query)
        except ValueError as exc:
            payloads.append({"tool": KNOWLEDGE_SEARCH_TOOL, "error": str(exc)})
            continue
        retrieval.searches_used += 1
        retrieval.last_status = result.status
        for chunk in result.chunks:
            retrieval.reference_ids.add(chunk.reference_id)
            for item in chunk.media:
                retrieval.media_by_id[item.resource.resource_id] = item.resource
        payloads.append(
            {
                "tool": KNOWLEDGE_SEARCH_TOOL,
                "retrieval_status": result.status.value,
                "calls_used": result.calls_used,
                "chunks": [chunk.to_dict() for chunk in result.chunks],
            }
        )
    return payloads


def _finalize(answer: dict[str, Any], retrieval: _TurnRetrieval) -> dict[str, Any]:
    """Validate the model answer against this turn's actual retrieval.

    Media blocks may only cite resource ids issued by THIS turn's searches;
    reference blocks only the chunk reference ids actually returned. Anything
    the model invented is dropped, never trusted.
    """
    try:
        parsed = QaAnswer.model_validate(
            {
                "blocks": answer.get("blocks") or [],
                # A greeting-style answer legitimately needs no retrieval, but
                # the public contract has no "not_needed" value — a model that
                # self-reports it (real-model behavior) is normalized here to
                # not_found instead of failing the whole run.
                "retrieval_status": answer.get("retrieval_status")
                if answer.get("retrieval_status") in {"found", "not_found", "unavailable", "limited"}
                else RetrievalStatus.NOT_FOUND.value,
            }
        )
    except ValidationError as exc:
        raise AgentContractError(f"customer QA answer failed contract validation: {exc}") from exc

    blocks: list[QaBlock] = []
    for block in parsed.blocks:
        if block.kind in ("image", "video") and block.resource_id not in retrieval.media_by_id:
            continue  # model cited a non-authorized media id — drop the block
        if block.kind == "reference" and block.reference_id not in retrieval.reference_ids:
            continue
        blocks.append(block)
    if not any(block.kind == "text" for block in blocks):
        raise AgentContractError("customer QA answer lost every text block")

    status = parsed.retrieval_status
    if status == "found" and not retrieval.reference_ids:
        # The model claims knowledge backing it never retrieved or received.
        status = RetrievalStatus.NOT_FOUND.value
    if retrieval.last_status == RetrievalStatus.UNAVAILABLE:
        status = RetrievalStatus.UNAVAILABLE.value

    cleaned = QaAnswer(blocks=blocks, retrieval_status=status)
    media_by_id = {key: value.to_dict() for key, value in retrieval.media_by_id.items()}
    payload = cleaned.to_public_dict(media_by_id=media_by_id)
    # Runtime-owned run metadata (harness search budget usage) alongside the
    # blocks contract; callers ignore it, the metrics seam reads it.
    payload["searches"] = retrieval.searches_used
    return payload


def _parse_rag_turn(final_response: str) -> dict[str, Any] | None:
    """Parse the model turn: raw JSON, fenced block, or outermost {...} span."""
    text = (final_response or "").strip()
    if not text:
        return None
    candidates: list[str] = [text]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE.finditer(text))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalize_answer_turn(turn: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the answer payload from a parsed answer turn, tolerantly.

    The blocks-v1 wire contract is ``{"kind": "answer", "answer": {"blocks":
    [...], "retrieval_status": ...}}``. Real models on some providers emit
    close-but-different shapes even under output_schema: the answer fields at
    turn top level (no ``answer`` wrapper) and block discriminators as
    ``"type"`` instead of ``"kind"`` (found in the P0 real canary, 2026-09-11).
    Normalize both variants to the contract shape; anything still shapeless
    returns None and the run fails as before.
    """
    answer = turn.get("answer")
    if isinstance(answer, dict):
        return answer
    blocks = turn.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return None
    normalized_blocks: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        normalized = dict(block)
        if not normalized.get("kind") and normalized.get("type"):
            normalized["kind"] = normalized.pop("type")
        # Real models add display metadata the strict blocks-v1 contract does
        # not carry (mime_type, caption, ...) — keep only contract fields so
        # pydantic's extra=forbid guard stays strict downstream.
        #
        # They also routinely overfill a block: a text block carrying the
        # reference id it cites, or a reference block echoing the chunk text.
        # The contract forbids those combinations and rejects the whole answer
        # (41 live, 2026-09-17: "text block must not carry resource/reference
        # ids"). Keeping the fields for this block's kind and dropping the rest
        # is what the model meant — the citation belongs in a sibling
        # reference block, and its loss is not worth failing the answer over.
        allowed_by_kind = {
            "text": ("kind", "text"),
            "image": ("kind", "resource_id", "title"),
            "video": ("kind", "resource_id", "title"),
            "reference": ("kind", "reference_id", "title"),
        }
        allowed = allowed_by_kind.get(str(normalized.get("kind")))
        if allowed is None:
            # Unknown kind: the contract will reject it, and inventing fields
            # for a shape we do not own is not this normalizer's job.
            normalized_blocks.append(normalized)
            continue
        normalized = {key: normalized[key] for key in allowed if key in normalized}
        normalized_blocks.append(normalized)
    if not normalized_blocks:
        return None
    return {
        "blocks": normalized_blocks,
        "retrieval_status": turn.get("retrieval_status"),
    }


def _needs_retrieval(question: str) -> bool:
    """A conservative heuristic for "business knowledge needed but unsearched".

    Greetings/chit-chat need no retrieval; anything mentioning business
    conduct words does. Deliberately errs toward forcing one search — the
    cost is a single bounded call, the miss is an unsourced answer.
    """
    text = question.strip()
    if not text:
        return False
    casual = (
        "你好",
        "在吗",
        "谢谢",
        "再见",
        "hello",
        "hi",
        "hola",
        "olá",
        "bonjour",
        "salut",
        "hallo",
        "danke",
        "merci",
        "gracias",
        "obrigado",
        "obrigada",
    )
    return not (len(text) <= 6 and any(word in text.lower() for word in casual))


def _search_results_prompt(results: list[dict[str, Any]], language: str) -> str:
    payload = json.dumps(results, ensure_ascii=False, indent=2)
    output_language = language_name(language)
    return f"""The harness executed your bounded knowledge_search requests.

Search outcomes:
```json
{payload}
```

Each chunk carries `content` (sanitized), `title`, `score`, and `media` entries
describing authorized resources. Return the final `answer` turn now: build
`blocks[]` with `text` blocks for prose, `image`/`video` blocks citing ONLY the
`resource_id` values from the media entries above, and `reference` blocks
citing the chunks' `reference_id`. If retrieval was empty or unavailable, say
so honestly in text and set `retrieval_status` to `not_found` or `unavailable`
as reported. Do not invent media ids or external URLs.
Write every `text` block in {output_language}.
"""


def _forced_search_prompt(question: str, language: str) -> str:
    return f"""Your previous answer skipped knowledge retrieval, but this
question looks like it needs business knowledge. One supplementary
knowledge_search is required before you answer. Request it now with a focused
`query` derived from the customer question:

\"\"\"{question}\"\"\"

The final `answer` turn must be written in {language_name(language)}.
"""


def _fallback_result(
    status: RetrievalStatus | None = None,
    *,
    searches: int = 0,
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    status = (
        status
        if status
        in {
            RetrievalStatus.NOT_FOUND,
            RetrievalStatus.UNAVAILABLE,
            RetrievalStatus.LIMITED,
        }
        else RetrievalStatus.LIMITED
    )
    messages = QA_FALLBACK_MESSAGES.get(language) or QA_FALLBACK_MESSAGES[DEFAULT_LANGUAGE]
    return {
        "blocks": [{"kind": "text", "text": messages[status.value]}],
        "retrieval_status": status.value,
        "searches": searches,
    }


def _initial_prompt(selection: CustomerAgentSelection, question: str, language: str) -> str:
    output_language = language_name(language)
    return f"""Answer the customer's charging/new-energy question as the
published customer service agent defined below. You have ONE bounded tool:
`knowledge_search` — request it when the question needs business knowledge
(policies, operations, product usage). `query` is REQUIRED and must be a
SHORT keyword phrase (3-8 words) from the user's question, never a full
sentence; `reason` stays to one brief sentence. Simple greetings or
chit-chat need no retrieval.

Agent business behavior instructions (authoritative for tone and scope):
\"\"\"{selection.prompt}\"\"\"

Customer question (may arrive in any language):
\"\"\"{question}\"\"\"

Output language: every customer-facing `text` block MUST be written in
{output_language}. The agent instructions above are the authoritative business
script — their facts, policies and numbers stay binding regardless of output
language. Knowledge-base excerpts may keep their original language; never
translate identifiers, prices or status codes.

Return a structured turn: either `kind=tool_requests` with up to one
`knowledge_search` request, or `kind=answer` with `blocks[]` (at least one
`text` block in {output_language}) and the honest `retrieval_status`.
Media and reference blocks may ONLY cite ids returned by this turn's searches.
"""
