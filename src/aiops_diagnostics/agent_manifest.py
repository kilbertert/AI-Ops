"""Declarative environment manifest + gateway agent reconciliation.

`ops/environments/<env>.toml` expresses the desired published agents for one
environment. ``aiops admin reconcile`` converges the gateway store toward the
manifest **through :class:`AgentManager`** — the same role-checked, validated,
KB-liveness-checked code path the HTTP API uses — with a synthetic admin
scope context. This replaces on-box sqlite surgery for environments whose
UPMS does not carry the ROLE_AGENT_ADMIN role family yet.

The same file carries a second, narrower section (``[[dify_apps]]``) that
converges the **runtime registry**: which Dify app serves a ``(tenant,
business entry)``. It is the same idea — desired state declared here, converged
by one reviewed command — but the two halves have different authorities and the
manifest states them apart rather than blurring them:

* ``[[agents]]`` is the **publish** authority for the pre-Dify path: an agent
  exists, is published, and is versioned because this file says so.
* ``[[dify_apps]]`` is a **runtime registry**, not a publish authority. It
  publishes nothing and versions nothing; it records the mapping the runtime
  reads to decide which app a tenant's question belongs to. An agent pulled from
  Dify is still published by our own publish action, never by this file.

The reconcile is idempotent: a second run against an unchanged manifest
reports every agent ``unchanged`` and writes nothing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES
from aiops_diagnostics.agent_lifecycle import (
    AgentConfig,
    AgentManager,
)
from aiops_diagnostics.dify_app_registry import (
    DifyAppBinding,
    DifyAppRegistry,
    DifyAppRegistryError,
    validate_bindings,
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
    dify_apps: tuple[DifyAppBinding, ...] = ()


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


DifyRegistryAction = Literal["registered", "unchanged", "removed"]


@dataclass(frozen=True, slots=True)
class DifyRegistryReport:
    tenant_id: str
    business_entry: str
    app_id: str
    action: DifyRegistryAction
    note: str = ""


class ManifestError(ValueError):
    """The manifest cannot be turned into valid AgentConfig objects."""


#: ``[[agents]]`` 里缺 ``prompt`` 时的占位值。**不是一个合法提示词**：装载期守卫要求
#: 「有 Dify 绑定 ⇔ 缺 prompt」，而 converge 要求「值是它 ⇔ 时不覆盖库中 prompt」。
#: 用哨兵而不是空串，是因为空串是**一个真实的意图**（"把这个 agent 的提示词设空"），
#: 与"我没写、它由别处来"必须能分开。
_PROMPT_FROM_DIFY = "\x00prompt-from-dify\x00"

#: 同上的哨兵，给工具白名单：清单没写 = 它由 Dify 拥有（R3）。
#: 空元组与它不同 —— 空元组是一个**真实意图**（"这个 agent 一个工具都不许跑"）。
_TOOLS_FROM_DIFY = ("\x00tools-from-dify\x00",)


def prompt_from_dify(agent: ManifestAgent) -> bool:
    """这个 agent 的提示词是不是**不来自清单**（即由 Dify 那一侧提供）。"""
    return agent.config.prompt == _PROMPT_FROM_DIFY


def tools_from_dify(agent: ManifestAgent) -> bool:
    """这个 agent 的工具白名单是不是**不来自清单**（即由 Dify 那一侧提供）。"""
    return agent.config.allowed_tools == _TOOLS_FROM_DIFY


#: 由 **Dify** 拥有、清单**不得**承载的字段（R3）。提示词是第一个（#739），
#: 工具白名单是第二个（#577 的 PR-3）：两者都经发布门从 Dify 拉回。
#:
#: 为什么需要一个**具名集合**而不是继续逐个特判：漏掉一个的后果是不对称的 ——
#: 漏掉 `prompt` 会让 reconcile 把线上正文覆盖成哨兵（#739 逮到过），漏掉
#: `allowed_tools` 会让它把运营刚设好的白名单改回默认。两种都是"看着收敛成功、
#: 实际把运营的改动改掉了"。加字段时**只改这一处**。
DIFY_OWNED_FIELDS: tuple[str, ...] = ("prompt", "allowed_tools")


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
    bindings: list[DifyAppBinding] = []
    for index, entry in enumerate(data.get("dify_apps", [])):
        try:
            bindings.append(_manifest_dify_app(entry))
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"dify_apps[{index}]: {exc}") from exc
    try:
        validated = validate_bindings(bindings)
    except DifyAppRegistryError as exc:
        raise ManifestError(str(exc)) from exc
    agents = _require_bound_agents_declared(tuple(agents), validated)
    return EnvironmentManifest(agents=agents, dify_apps=validated)


def _require_bound_agents_declared(
    agents: tuple[ManifestAgent, ...], bindings: tuple[DifyAppBinding, ...]
) -> tuple[ManifestAgent, ...]:
    """两份声明必须互为**同一个事实**：绑定必须指向已声明的 agent，且二者不可各写一份提示词。

    两条都在**装载期**判，不在 converge 判。理由与模型白名单同源：注册行与它指向的
    agent 是同一个事实，半错的事实必须在写库**之前**失败，而不是之后。

    第二条（本条是 #577「Dify 是权威编辑面」缺的那道门）：一个 agent **要么**由 Dify
    编辑（有 ``dify_apps`` 绑定 ⇒ 不写 ``prompt``），**要么**由清单承载（写 ``prompt``
    ⇒ 不绑定）。两者都写就是**两个权威**，而线上该用哪一份**没有规则**可以回答 ——
    本仓确实撞上过：绑定指向 Dify 上的 129 字存根，而 reconcile 发布的是清单里的 758 字。
    允许这种状态，等于允许一份只有专门去查才看得见的漂移。
    """
    declared = {(agent.tenant_id, agent.name) for agent in agents}
    unknown = sorted(
        f"{binding.tenant_id}/{binding.business_entry} → {binding.agent_name}"
        for binding in bindings
        if (binding.tenant_id, binding.agent_name) not in declared
    )
    if unknown:
        raise ManifestError(
            "dify_apps 绑定的 agent_name 未在 [[agents]] 中声明（同一租户内）: " + "; ".join(unknown)
        )

    # 上一轮被绑定、这一轮**既没带绑定、又改了正文**的 agent：装载期看不到库里的旧注册行，
    # 因此这里查不出来 —— 那道判定必须在能读到注册表的地方做（`_preflight`）。
    bound = {(binding.tenant_id, binding.agent_name) for binding in bindings}
    both = sorted(
        f"{agent.tenant_id}/{agent.name}"
        for agent in agents
        if (agent.tenant_id, agent.name) in bound and not prompt_from_dify(agent)
    )
    if both:
        raise ManifestError(
            "同一个 agent 不能既绑定 Dify app、又在 [[agents]] 里写 prompt（两个权威，"
            "线上该用哪一份没有规则可答）。绑定了 Dify 的请删掉 prompt；不打算由 Dify 编辑的请删掉绑定: "
            + "; ".join(both)
        )

    # 反向：**没有** Dify 绑定的 agent，prompt 就是它唯一的正文来源。
    # 少了这一条，「prompt 可选」会顺手允许一个"既没有 Dify、也没有正文"的 agent ——
    # 那是把「必须有一个来源」这条规则换成「可以一个都没有」。
    orphan = sorted(
        f"{agent.tenant_id}/{agent.name}"
        for agent in agents
        if (agent.tenant_id, agent.name) not in bound and prompt_from_dify(agent)
    )
    if orphan:
        raise ManifestError(
            "未绑定 Dify app 的 agent 必须有 prompt（否则它没有任何正文来源）: " + "; ".join(orphan)
        )

    # R3 · 工具白名单与 prompt 同构：**Dify 拥有它** ⇒ 绑定的 agent 不得在清单里写。
    # 写了就是一个会静默生效的第二权威：reconcile 会把运营在 Dify 里设好的白名单
    # 覆盖成清单里那一份，而报告只说「updated」。
    tools_both = sorted(
        f"{agent.tenant_id}/{agent.name}"
        for agent in agents
        if (agent.tenant_id, agent.name) in bound and not tools_from_dify(agent)
    )
    if tools_both:
        raise ManifestError(
            "绑定了 Dify app 的 agent 不能又在 [[agents]] 里写 allowed_tools（工具白名单由 Dify 拥有）: "
            + "; ".join(tools_both)
        )

    # 未绑定的 agent 没写 ``allowed_tools`` 时哨兵必须**归一成默认值**，否则它会带着
    # 那个不可能匹配任何 ``ToolName`` 的字符串进发布门，被当成"未知工具名"拒掉 ——
    # 一个只是没写这个字段的合法清单会因此装载失败。
    return tuple(
        replace(agent, config=replace(agent.config, allowed_tools=ALL_TOOL_NAMES))
        if tools_from_dify(agent) and (agent.tenant_id, agent.name) not in bound
        else agent
        for agent in agents
    )


def _manifest_dify_app(entry: dict[str, Any]) -> DifyAppBinding:
    """One registry row. Every field is required and none is derived.

    ``agent_name`` in particular is not defaulted to the app id or the tenant's
    only agent: the whole point of this registry is that the runtime is told
    which agent serves an entry, instead of working it out.
    """
    return DifyAppBinding(
        tenant_id=str(entry["tenant_id"]),
        business_entry=str(entry["business_entry"]),
        app_id=str(entry["app_id"]),
        agent_name=str(entry["agent_name"]),
    )


def _manifest_agent(entry: dict[str, Any]) -> ManifestAgent:
    agent_type = str(entry["agent_type"])
    output_contract = str(entry.get("output_contract") or DEFAULT_OUTPUT_CONTRACTS[agent_type])
    config = AgentConfig(
        agent_type=agent_type,
        # ``prompt`` 现在**可选**：绑定了 Dify app 的 agent，其提示词由 Dify 编辑、
        # 经发布门拉回（#577）。缺失时用这个哨兵占位，是否合法由装载期的守卫判定 ——
        # 这里是**结构**解析，守卫是**语义**判定，两者分开。
        prompt=str(entry["prompt"]) if "prompt" in entry else _PROMPT_FROM_DIFY,
        knowledge_base_ids=tuple(str(item) for item in entry.get("knowledge_base_ids", [])),
        model=str(entry["model"]),
        output_contract=output_contract,
        opening_questions=tuple(str(item) for item in entry.get("opening_questions", [])),
        quick_commands=tuple(str(item) for item in entry.get("quick_commands", [])),
        # 与 ``prompt`` 同构：缺失 = 哨兵（由 Dify 拥有），是否合法由装载期守卫判定。
        allowed_tools=(
            tuple(str(item) for item in entry["allowed_tools"])
            if "allowed_tools" in entry
            else _TOOLS_FROM_DIFY
        ),
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
    """Converge the manager's store toward the ``[[agents]]`` half; idempotent.

    All structural and model preflight checks run before the first write, so an
    invalid manifest leaves the store untouched. The ``[[dify_apps]]`` half is a
    separate table with a separate report and converges in the same run through
    :func:`reconcile_dify_registry` — two stores, one declared state.
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


def _require_no_source_switch(registry: DifyAppRegistry, manifest: EnvironmentManifest) -> None:
    """从「Dify 编辑」切回「清单编辑」时，库里那条旧绑定必须**先被清掉**。

    装载期只能看见**文件里**的绑定，所以「上一轮绑定、这一轮改回内联 prompt」在文件上
    看起来完全合法。而本函数默认 `prune=False`（未变），旧注册行会留着 ⇒
    **线上的双来源继续存在**，只是看不见了。因此判定必须落在**读得到注册表的地方**。

    （第一版写在 `_preflight` 里、用 `getattr(manager, "dify_registry", None)` 取注册表 ——
    而 `AgentManager` 根本没有这个属性，`getattr` 恒为 None ⇒ **那段守卫是死的**。
    评审的这一条把两个问题一起暴露了：判定要能读到注册表，而且不能靠猜属性名。）
    """
    live = {(binding.tenant_id, binding.agent_name) for binding in registry.all()}
    declared = {(binding.tenant_id, binding.agent_name) for binding in manifest.dify_apps}
    switched = sorted(
        f"{agent.tenant_id}/{agent.name}"
        for agent in manifest.agents
        if (agent.tenant_id, agent.name) in live
        and (agent.tenant_id, agent.name) not in declared
        and not prompt_from_dify(agent)
    )
    if switched:
        raise ManifestError(
            "这些 agent 库里仍绑着 Dify app，本轮却改由清单提供正文（旧绑定不会被自动清除，"
            "线上会出现两个来源）。请在清单里保留绑定、把正文交给 Dify，"
            "或先带 --prune 清掉旧绑定再切: " + "; ".join(switched)
        )


def reconcile_dify_registry(
    registry: DifyAppRegistry,
    manifest: EnvironmentManifest,
    *,
    prune: bool = False,
    dry_run: bool = False,
) -> list[DifyRegistryReport]:
    """Converge the runtime registry toward ``[[dify_apps]]``; idempotent.

    A row is written for every declared binding, and an unchanged binding writes
    nothing. A binding that is in the store and not in the manifest is removed
    only under ``prune`` — the same flag that governs agent removal, for the
    same reason: deleting is the direction that needs to be asked for, and a
    stray binding is visible (the runtime keeps serving it, and the report names
    it) in a way a silently deleted one is not.

    What this does **not** do is check that the named agent exists — that is a
    property of the manifest alone and is enforced where the manifest is read
    (:func:`load_manifest`), so both halves fail together and before any write.
    """
    _require_no_source_switch(registry, manifest)
    desired = {(binding.tenant_id, binding.business_entry): binding for binding in manifest.dify_apps}
    existing = {(binding.tenant_id, binding.business_entry): binding for binding in registry.all()}
    reports: list[DifyRegistryReport] = []
    for binding in desired.values():
        key = (binding.tenant_id, binding.business_entry)
        if existing.get(key) == binding:
            reports.append(
                DifyRegistryReport(binding.tenant_id, binding.business_entry, binding.app_id, "unchanged")
            )
            continue
        if not dry_run:
            registry.put(binding)
        reports.append(
            DifyRegistryReport(
                binding.tenant_id,
                binding.business_entry,
                binding.app_id,
                "registered",
                "dry-run" if dry_run else "",
            )
        )
    if not prune:
        return reports
    for key, binding in existing.items():
        if key in desired:
            continue
        if not dry_run:
            registry.delete(*key)
        reports.append(
            DifyRegistryReport(
                binding.tenant_id,
                binding.business_entry,
                binding.app_id,
                "removed",
                "dry-run" if dry_run else "清单未声明",
            )
        )
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

    # **提示词不由清单承载时，收敛必须保留库里那一份。**
    #
    # 不这么做会有一个很坏的下场：这类 agent 的 ``config.prompt`` 是哨兵，与库中
    # 现值不等 ⇒ ``drift`` 恒为真 ⇒ 每一次 reconcile 都会把线上提示词**覆盖成哨兵**
    # （grep 出来的字面量，不是提示词）。而"把清单里的 prompt 删掉、改由 Dify 编辑"
    # 恰恰是这个改动要促成的状态 —— 也就是说，不修这里，"收敛"会把功能一起收掉。
    #
    # 其余字段仍以清单为准：``DIFY_OWNED_FIELDS`` 里的每一个都走这条分支，别的一个都不走。
    # 逐个字段而不是"整个 config 都继承"：知识库/模型/契约仍是清单的权威，只有
    # Dify 真正拥有的那两个不是。
    if existing is not None:
        preserved: dict[str, Any] = {}
        if prompt_from_dify(agent):
            preserved["prompt"] = existing.config.prompt
        if tools_from_dify(agent):
            preserved["allowed_tools"] = existing.config.allowed_tools
        if preserved:
            agent = replace(agent, config=replace(agent.config, **preserved))

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
        # 🔴 **没有库中版本时，占位符绝不能成为线上提示词。**
        # 一条"只绑定 Dify、不写 prompt"的清单在**空库**上收敛时，这里没有可继承的现值 ——
        # 而 `AgentManager._validate_config` 只查长度（实测），所以哨兵会**通过校验并被发布**，
        # 那个入口从此用一段 grep 出来的字面量回答用户。（评审在 #739 上逮到。）
        if prompt_from_dify(agent):
            return report(
                "manual-action-required",
                None,
                "库中还没有这个 agent，正文又不在清单里 —— 先把它创建并发布（或写 prompt），"
                "Dify 侧的正文要经 `admin pull-dify` 落成已发布版本后才存在",
            )
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


def admin_context(tenant_id: str) -> Any:
    """The synthetic admin scope one tenant's reconcile/publish runs under.

    Public because it has a second caller now (#625): the Dify pull action needs
    the same "an admin is publishing this" context, and two copies of it would
    drift in exactly the way that matters — one of them would publish under a
    context whose roles no longer satisfy ``AgentManager.PUBLISH_ROLES``.
    """
    return _admin_context_factory()(tenant_id)


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
