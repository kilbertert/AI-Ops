"""Declarative environment manifest + gateway agent reconciliation.

`ops/environments/<env>.toml` expresses the desired published agents for one
environment. ``aiops admin reconcile`` converges the gateway store toward the
manifest **through :class:`AgentManager`** — the same role-checked, validated,
KB-liveness-checked code path the HTTP API uses — with a synthetic admin
scope context. This replaces on-box sqlite surgery for environments whose
UPMS does not carry the ROLE_AGENT_ADMIN role family yet.

The reconcile is idempotent: a second run against an unchanged manifest
reports every agent ``unchanged`` and writes nothing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from aiops_diagnostics.agent_lifecycle import (
    AgentConfig,
    AgentManager,
)

# Only the fields AgentManager.create/publish need; output_contract defaults
# by agent_type (same pairing _validate_config enforces).
DEFAULT_OUTPUT_CONTRACTS = {"customer": "blocks-v1", "operations": "diagnosis-v1"}
MANIFEST_STATES = frozenset({"published", "disabled"})


@dataclass(frozen=True, slots=True)
class ManifestAgent:
    tenant_id: str
    name: str
    description: str
    config: AgentConfig
    state: str = "published"


@dataclass(frozen=True, slots=True)
class EnvironmentManifest:
    agents: tuple[ManifestAgent, ...]


ReconcileAction = Literal[
    "created",
    "published",
    "updated",
    "unchanged",
    "disabled",
    "manual-action-required",
    "pruned-disabled",
    "pruned-deleted",
]


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    tenant_id: str
    name: str
    agent_id: str
    action: ReconcileAction
    version: int | None
    note: str = ""


class ManifestError(ValueError):
    """The manifest cannot be turned into valid AgentConfig objects."""


def load_manifest(path: Path) -> EnvironmentManifest:
    """Parse and structurally validate a TOML environment manifest."""
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    agents: list[ManifestAgent] = []
    for index, entry in enumerate(data.get("agents", [])):
        try:
            agents.append(_manifest_agent(entry))
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"agents[{index}]: {exc}") from exc
    return EnvironmentManifest(agents=tuple(agents))


def _manifest_agent(entry: dict[str, Any]) -> ManifestAgent:
    agent_type = str(entry["agent_type"])
    output_contract = str(entry.get("output_contract") or DEFAULT_OUTPUT_CONTRACTS[agent_type])
    config = AgentConfig(
        agent_type=agent_type,
        prompt=str(entry["prompt"]),
        knowledge_base_ids=tuple(str(item) for item in entry.get("knowledge_base_ids", [])),
        model=str(entry["model"]),
        output_contract=output_contract,
        opening_questions=tuple(str(item) for item in entry.get("opening_questions", [])),
        quick_commands=tuple(str(item) for item in entry.get("quick_commands", [])),
    )
    state = str(entry.get("state") or "published")
    if state not in MANIFEST_STATES:
        raise ValueError(f"state 必须是 {sorted(MANIFEST_STATES)}，收到 {state!r}")
    return ManifestAgent(
        tenant_id=str(entry["tenant_id"]),
        name=str(entry["name"]),
        description=str(entry.get("description") or ""),
        config=config,
        state=state,
    )


def reconcile(
    manager: AgentManager,
    manifest: EnvironmentManifest,
    *,
    prune: bool = False,
    dry_run: bool = False,
) -> list[ReconcileReport]:
    """Converge the manager's store toward the manifest; idempotent.

    All structural and model preflight checks run before the first write,
    so an invalid manifest leaves the store untouched.
    """
    _preflight(manager, manifest)
    context_factory = _admin_context_factory()
    reports: list[ReconcileReport] = []
    seen: dict[str, set[str]] = {}
    for agent in manifest.agents:
        context = context_factory(agent.tenant_id)
        existing = _find_by_name(manager, context, agent.name)
        reports.append(_converge_one(manager, context, agent, existing, dry_run))
        seen.setdefault(agent.tenant_id, set()).add(agent.name)
    if prune:
        for tenant_id, names in seen.items():
            context = context_factory(tenant_id)
            reports.extend(_prune_tenant(manager, context, names, dry_run))
    return reports


def _preflight(manager: AgentManager, manifest: EnvironmentManifest) -> None:
    """Fail before any write when the manifest cannot publish cleanly."""
    if not manifest.agents:
        return
    bad_models = sorted(
        {
            f"{agent.tenant_id}/{agent.name}: {agent.config.model}"
            for agent in manifest.agents
            if agent.config.model not in manager.allowed_models
        }
    )
    if bad_models:
        allowed = ", ".join(sorted(manager.allowed_models))
        raise ManifestError(
            f"模型不在白名单（先改 manifest 或 Settings.providers）: {'; '.join(bad_models)}；允许: {allowed}"
        )


def _find_by_name(manager: AgentManager, context: Any, name: str) -> Any:
    for agent in manager.list(context):
        if agent.name == name:
            return agent
    return None


def _converge_one(
    manager: AgentManager,
    context: Any,
    agent: ManifestAgent,
    existing: Any,
    dry_run: bool,
) -> ReconcileReport:
    existing_id = getattr(existing, "agent_id", "")

    def report(action: str, version: int | None, note: str = "") -> ReconcileReport:
        return ReconcileReport(agent.tenant_id, agent.name, existing_id, action, version, note)

    if agent.state == "disabled":
        if existing is None:
            return report("unchanged", None, "清单状态 disabled 且库里不存在，无需操作")
        if existing.status == "disabled":
            return report("unchanged", existing.published_version)
        if dry_run:
            return report("disabled", existing.published_version, "dry-run")
        manager.disable(context, existing.agent_id, expected_revision=existing.revision)
        return report("disabled", existing.published_version)

    if existing is None:
        if dry_run:
            return report("created", None, "dry-run")
        created = manager.create(context, name=agent.name, description=agent.description, config=agent.config)
        version = manager.publish(context, created.agent_id, expected_revision=created.revision)
        return report("created", version.version_no)

    if existing.status == "disabled":
        # store 无 disabled→published 路径（fork 要求 published）——报告人工处理
        return report(
            "manual-action-required", existing.published_version, "库中已 disabled，需人工重建后收敛"
        )

    drift = existing.config != agent.config or existing.description != agent.description
    if existing.status == "draft":
        if not drift:
            if dry_run:
                return report("published", existing.published_version, "dry-run")
            version = manager.publish(context, existing.agent_id, expected_revision=existing.revision)
            return report("published", version.version_no)
        if dry_run:
            return report("updated", None, "dry-run")
        updated = manager.update(
            context,
            existing.agent_id,
            expected_revision=existing.revision,
            name=agent.name,
            description=agent.description,
            config=agent.config,
        )
        version = manager.publish(context, updated.agent_id, expected_revision=updated.revision)
        return report("updated", version.version_no)

    if not drift:
        return report("unchanged", existing.published_version)
    if dry_run:
        return report("updated", None, "dry-run")
    draft = manager.fork_draft(context, existing.agent_id, expected_revision=existing.revision)
    updated = manager.update(
        context,
        draft.agent_id,
        expected_revision=draft.revision,
        name=agent.name,
        description=agent.description,
        config=agent.config,
    )
    version = manager.publish(context, updated.agent_id, expected_revision=updated.revision)
    return report("updated", version.version_no)


def _prune_tenant(
    manager: AgentManager,
    context: Any,
    keep_names: set[str],
    dry_run: bool,
) -> list[ReconcileReport]:
    reports: list[ReconcileReport] = []
    tenant_id = context.effective_tenant_id
    for agent in manager.list(context):
        if agent.name in keep_names:
            continue
        if agent.status == "published":
            if dry_run:
                reports.append(
                    ReconcileReport(
                        tenant_id,
                        agent.name,
                        agent.agent_id,
                        "pruned-disabled",
                        agent.published_version,
                        "dry-run",
                    )
                )
                continue
            manager.disable(context, agent.agent_id, expected_revision=agent.revision)
            reports.append(
                ReconcileReport(
                    tenant_id, agent.name, agent.agent_id, "pruned-disabled", agent.published_version
                )
            )
        elif agent.status == "draft" and agent.published_version is None:
            if dry_run:
                reports.append(
                    ReconcileReport(tenant_id, agent.name, agent.agent_id, "pruned-deleted", None, "dry-run")
                )
                continue
            manager.delete(context, agent.agent_id, expected_revision=agent.revision)
            reports.append(ReconcileReport(tenant_id, agent.name, agent.agent_id, "pruned-deleted", None))
    return reports


def _admin_context_factory() -> Any:
    """Synthetic admin ScopeContext per tenant (mirrors the test _context
    pattern in tests/test_agent_lifecycle.py; b_user_id flows into
    created_by/published_by so reconcile writes stay auditable)."""
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

    def _context(tenant_id: str):
        subject = SubjectRecord(b_user_id="aiops-admin", tenant_id=tenant_id)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant_id,
            data_scope=DataScope(type="self"),
            roles=frozenset({"ROLE_AGENT_ADMIN", "ROLE_PLATFORM_ADMIN"}),
            permissions=frozenset({"aiops:agents:manage"}),
        )

    return _context
