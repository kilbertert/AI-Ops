"""读取已发布的客服元数据，供开场白和快捷动作使用；不执行模型。"""

from __future__ import annotations

from dataclasses import dataclass

from aiops_diagnostics.publication.agent_lifecycle import AgentStore


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
    agent_name: str | None = None,
) -> CustomerAgentSelection | None:
    """Return the published customer agent that serves this tenant, or None.

    The store is tenant-scoped: a missing or cross-tenant agent is
    indistinguishable (absent), so no existence is leaked. Disabled agents and
    drafts are never selected; agents without knowledge bases cannot serve
    the RAG path. Agents pinned as a published shortcut's promotional target
    (#231) never serve plain customer QA — that path belongs to the
    customer-service agent; a promotional agent answers only via the
    promotional route.

    ``agent_name`` (#584) is the runtime registry's answer to "which agent
    serves this ``(tenant, entry)``". When it is given, only that agent is
    considered — the selection stops being a search and becomes a lookup, which
    is the point: the newest published agent is a reasonable default only while
    nobody has declared otherwise. ``None`` keeps the pre-registry rule, for the
    environments that have not adopted the registry yet.
    """
    from aiops_diagnostics.publication.agent_lifecycle import AgentNotFound

    promo_targets = _pinned_promo_agents(store, tenant_id)
    try:
        agents = store.list(tenant_id)
    except Exception:  # store unavailable — fall back to the zero-order path
        return None
    for agent in agents:
        if agent_name is not None and agent.name != agent_name:
            continue
        if agent.agent_id in promo_targets:
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
    from aiops_diagnostics.publication.shortcut_lifecycle import ShortcutStore

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
