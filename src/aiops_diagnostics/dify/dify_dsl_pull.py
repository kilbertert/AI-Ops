"""Dify DSL 拉取汇聚与 Workflow 快照同步编排入口。

在 ``dify_dsl_client``（HTTP 客户端）与 ``dify_dsl_mapping``（纯映射规则）之上，
编排智能体草稿拉取收敛与 Workflow/Chatflow 绑定同步，并保留既有模块级重导出，
以兼容测试对 ``DifyDslClient`` 与 ``sync_binding_from_dify`` 的 monkeypatch 契约。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

import yaml

from aiops_diagnostics.dify.dify_dsl_client import (
    _APP_ID as _APP_ID,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    _EXPORT_RETRY as _EXPORT_RETRY,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DIFY_APP_DETAIL_PATH as DIFY_APP_DETAIL_PATH,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DIFY_DATASET_PATH as DIFY_DATASET_PATH,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DIFY_EXPORT_PATH as DIFY_EXPORT_PATH,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DIFY_TOOL_PROVIDERS_PATH as DIFY_TOOL_PROVIDERS_PATH,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DIFY_WORKFLOW_PUBLISH_PATH as DIFY_WORKFLOW_PUBLISH_PATH,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DIFY_WORKSPACE_HEADER as DIFY_WORKSPACE_HEADER,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslAuthRejected as DifyDslAuthRejected,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslClient as DifyDslClient,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslError as DifyDslError,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslMalformed as DifyDslMalformed,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslSource as DifyDslSource,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslUnreachable as DifyDslUnreachable,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    DifyDslVersionUnsupported as DifyDslVersionUnsupported,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    _dataset_envelope as _dataset_envelope,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    _error_mapping as _error_mapping,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    _export_envelope as _export_envelope,
)
from aiops_diagnostics.dify.dify_dsl_client import (
    _tool_providers_envelope as _tool_providers_envelope,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _DSL_VERSION as _DSL_VERSION,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    SUPPORTED_DSL_MAJOR as SUPPORTED_DSL_MAJOR,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    SUPPORTED_DSL_MINOR as SUPPORTED_DSL_MINOR,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _declared_dataset_ids as _declared_dataset_ids,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _declared_tools as _declared_tools,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _default_output_contract as _default_output_contract,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _dsl_kind as _dsl_kind,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _merge_workflow_snapshot as _merge_workflow_snapshot,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _optional_opening_statement as _optional_opening_statement,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _optional_suggested_questions as _optional_suggested_questions,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _require_supported_version as _require_supported_version,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _required_model as _required_model,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _required_prompt as _required_prompt,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    _resolve_declared_datasets as _resolve_declared_datasets,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    map_dsl_to_config as map_dsl_to_config,
)
from aiops_diagnostics.dify.dify_dsl_mapping import (
    workflow_content as workflow_content,
)
from aiops_diagnostics.publication.agent_lifecycle import (
    Agent,
    AgentConfig,
    AgentManager,
    config_from_snapshot,
)


def resolve_pulled_config(
    manager: AgentManager,
    context: Any,
    source: DifyDslSource,
    *,
    name: str,
    agent_type: str,
    output_contract: str | None = None,
    knowledge_base_ids: tuple[str, ...] | None = None,
    knowledge_registry: Mapping[str, Any] | None = None,
    tenant_id: str = "",
) -> AgentConfig:
    """Fetch, map and settle the config a pull would land — **no writes at all**.

    This is the single place the pulled config is decided. Dry-run and the real
    pull both call it, so what a dry-run reports is *the same object* the real
    pull would publish rather than a second implementation of the same rules.
    (A hand-rolled copy in the CLI's dry-run branch is exactly how the retained
    knowledge binding came to be reported as empty while the real pull kept it.)

    ``knowledge_base_ids`` is the operator's binding, not the DSL's; see
    :func:`map_dsl_to_config`. When it is ``None`` **and Dify declares no
    dataset**, the existing agent's binding is **kept**, never cleared: "this
    pull did not state a knowledge base" and "this agent has no knowledge base"
    are different statements, and only the second one should empty the field.
    Without that, pulling a dataset-free app would silently drop a live
    customer-service agent off the RAG path — the same silent-overwrite the
    manifest's prompt handling already guards against (#739).

    ``knowledge_registry`` is what makes an operator's Dify-side dataset change
    **take effect**. It is the *same* table the inbound retrieval route uses
    (:func:`parse_dify_knowledge_bindings`), read in the other direction: a
    dataset id becomes the operator's handle, and the handle names our
    knowledge-base ids. ``None`` keeps the pre-bridge behaviour — a declared
    dataset with nowhere to resolve is a hard failure, never a silent copy.
    """
    client = DifyDslClient(source)
    #: Set by the resolver below, which the mapper calls **only** when the DSL
    #: declares a dataset. That is how we tell "Dify said nothing" (keep the
    #: live binding) from "Dify named one" (its ids win) without parsing the
    #: DSL a second time and risking the two answers disagreeing.
    declared: list[tuple[str, ...]] = []

    def resolver(ids: tuple[str, ...]) -> tuple[str, ...]:
        declared.append(ids)
        return _resolve_declared_datasets(ids, knowledge_registry or {}, client, tenant_id=tenant_id)

    #: The ``aiops`` provider's UUID, resolved **once, here, from the console** —
    #: never from config (module docstring). Looked up lazily on first use so a
    #: DSL with no declared tools costs no extra console call; the console read
    #: happens only when the file actually names one.
    exported = client.export_dsl()
    existing = _find_by_name(manager, context, name)

    config = map_dsl_to_config(
        exported,
        agent_type=agent_type,
        output_contract=output_contract,
        knowledge_base_ids=knowledge_base_ids,
        dataset_resolver=resolver if knowledge_registry else None,
    )
    if knowledge_base_ids is None and not declared and existing is not None:
        # Only "Dify said nothing" keeps the live binding. When Dify *does*
        # declare a dataset, the resolved ids already won; keeping the live
        # ones on top would make the operator's edit invisible, which is the
        # whole point of this change.
        config = replace(config, knowledge_base_ids=existing.config.knowledge_base_ids)
    if existing is not None:
        # **``model`` 保持库中现值，永不从 Dify 覆盖。**
        #
        # 实测：运行时**不读** ``AgentConfig.model`` —— 真实模型来自 provider 配置
        # （``model_request`` / ``agent_lifecycle`` 读 ``provider.model``；全仓对
        # ``config.model`` 只有发布门与相等比较两处消费）。所以从 Dify 拉它**不改变线上模型**，
        # 却会经发布门产生一个用户可见的后果：运营在 Dify 里换了模型名而它不在
        # ``allowed_models`` 里时，**整个发布失败**；在允许列表里时，则是一次
        # 静默无效的改动 —— 两种都不是运营想要的答案。
        #
        # 因此这里的口径是「清单/库中值权威，Dify 那一侧不参与」：
        # 拉取不再动它，运营改它不会坏掉发布，也不会让谁以为线上模型变了。
        # 新建（库中不存在）时仍取 DSL 的值 —— 那时没有现值可保，且它要过发布门。
        config = replace(config, model=existing.config.model)
    return config


def pull_agent_draft(
    manager: AgentManager,
    context: Any,
    source: DifyDslSource,
    *,
    name: str,
    description: str,
    agent_type: str,
    output_contract: str | None = None,
    knowledge_base_ids: tuple[str, ...] | None = None,
    knowledge_registry: Mapping[str, Any] | None = None,
) -> Agent:
    """Pull ``source`` and land the result as a draft. Never publishes.

    The fetch, the parse and the mapping all complete before the first write,
    so any failure leaves the store exactly as it was — no half-built draft.
    The config itself comes from :func:`resolve_pulled_config`, which the
    dry-run path calls too, so the two cannot drift.
    """
    config = resolve_pulled_config(
        manager,
        context,
        source,
        name=name,
        agent_type=agent_type,
        output_contract=output_contract,
        knowledge_base_ids=knowledge_base_ids,
        knowledge_registry=knowledge_registry,
        tenant_id=str(getattr(context, "effective_tenant_id", "") or ""),
    )
    return converge_agent_draft(manager, context, config, name=name, description=description)


def converge_agent_draft(
    manager: AgentManager,
    context: Any,
    config: AgentConfig,
    *,
    name: str,
    description: str,
) -> Agent:
    """Bring the tenant's agent of this name to ``config`` as a draft.

    An existing draft is updated in place; an existing published agent is forked
    into a draft first, so the published snapshot an in-flight turn may be using
    stays immutable. Both go through ``expected_revision``, so a concurrent edit
    is a conflict rather than a silent overwrite. Converging twice is a no-op.
    """
    existing = _find_by_name(manager, context, name)
    if existing is None:
        return manager.create(context, name=name, description=description, config=config)

    draft = (
        existing
        if existing.status == "draft"
        else manager.fork_draft(context, existing.agent_id, expected_revision=existing.revision)
    )
    if draft.config == config and draft.description == description:
        return draft
    return manager.update(
        context,
        draft.agent_id,
        expected_revision=draft.revision,
        name=name,
        description=description,
        config=config,
    )


def _find_by_name(manager: AgentManager, context: Any, name: str) -> Agent | None:
    return next((agent for agent in manager.list(context) if agent.name == name), None)


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """One registry binding's result, named so the caller can print and count it.

    ``action`` is one of:

    * ``unchanged`` — Dify's prompt already equals the **published** version's
      prompt; nothing was written. This is the common case, and it is what makes
      re-running the command harmless — **not** a licence to put it on a timer
      (see :func:`sync_binding_from_dify` on why it must stay an explicit act).
    * ``synced`` — the prompt differed; a new version was frozen.
    * ``refused`` — the binding cannot be synced as declared (see ``note``).
    * ``failed`` — Dify or the publish gate rejected it (see ``note``).
    """

    tenant_id: str
    business_entry: str
    app_id: str
    agent_name: str
    action: str
    version_no: int | None = None
    note: str = ""
    #: **已发布 workflow 的身份**（不是 app_id，也不是 workflow_run_id）。
    #: 发布要把路由指针推到这一版，就必须把"这一版在 Dify 里到底是哪张图"一起记下来。
    #: 取不到时为空串：那是"不知道",调用方必须按未完成处理，不能拿 app_id 顶替。
    workflow_id: str = ""
    agent_id: str = ""
    route_scenario: str = ""


def sync_workflow_from_dify(
    manager: AgentManager,
    context: Any,
    binding: Any,
    source: DifyDslSource,
    *,
    consumer_settings: dict | None = None,
    consumer_callback_base: str = "",
    consumer_libraries=None,
    bootstrap: Callable[[dict], dict] | None = None,
    expected_workflow: dict | None = None,
    bootstrap_agent_id: str | None = None,
) -> SyncOutcome:
    """把一个**workflow**应用的当前内容冻结成新的已发布版本。

    与 `sync_binding_from_dify` 的差别只有一处，但它是概念上的：workflow 应用没有
    `model_config`，它的内容是一张图。所以这里冻结的快照是
    `{"workflow": <规范化后的 graph>, "workflow_id": ...}` —— **不是**一个 AgentConfig，
    也不假装是。版本仍然住在同一个 `agent_versions` 表里（内容不可变、可追发布者、
    revision 乐观锁都复用），只是 `snapshot` 的形状由应用类型决定。

    Dify 没有草稿区：保存即当前内容，所以"发布"= 把这个当前内容冻结。内容没变时
    按构造是 `unchanged`，重复点发布是 no-op。

    ``bootstrap`` 是给**页面开通的新客户端**留的口子：那一步之前库里没有这个 agent，
    而"先建一个只有配置的 V1、再发一个带编排的 V2"会让客户的第一个正式版本不是 V1。
    给定它时，agent 不存在就按它返回的配置建一个草稿，并在**同一个**版本里冻结编排；
    它拿到的是**已经导出一次**的那份文档，所以"建 agent 用的内容"与"冻结的内容"是同一份。
    不给它时行为逐字不变（不存在即 refused）。
    """
    from aiops_diagnostics.publication.agent_lifecycle import AgentConfig
    from aiops_diagnostics.publication.agent_manifest import admin_context

    client = DifyDslClient(source)
    existing = _find_by_name(manager, context, binding.agent_name)
    # 导出**只做一次**：bootstrap 需要同一份文档来取提示词与模型，而在这一步之后再导一次
    # 会让"建 agent 用的那一份"和"冻结的那一份"有可能不是同一份。
    dsl_text = client.export_dsl()
    if expected_workflow is not None and workflow_content(dsl_text) != expected_workflow:
        raise DifyDslMalformed("workflow changed after confirmation")
    previous = (
        manager.store.version(existing.agent_id, existing.tenant_id, existing.published_version).snapshot
        if existing is not None and existing.status == "published" and existing.published_version is not None
        else {}
    )
    candidate = workflow_content(dsl_text)
    if consumer_settings is not None:
        from aiops_diagnostics.consumer.agent_contract import publication_settings

        # 先验证当前草稿；选库错误不能留下一个阻止后续改正的半成品 Agent。
        consumer_settings = publication_settings(
            candidate,
            consumer_settings,
            consumer_callback_base,
            require_split=(previous.get("consumer_service") or {}).get("callback_version", 1) >= 2,
            require_picker=(previous.get("consumer_service") or {}).get("callback_version", 1) >= 3,
            owned_libraries=consumer_libraries,
        )
    if not previous:
        if bootstrap is None:
            return SyncOutcome(
                binding.tenant_id,
                binding.business_entry,
                binding.app_id,
                binding.agent_name,
                "refused",
                note="agent has no published configuration",
            )
        config = AgentConfig(**bootstrap(yaml.safe_load(dsl_text)))
        if consumer_settings is not None:
            config = replace(config, knowledge_base_ids=tuple(consumer_settings["knowledge_base_ids"]))
        manager._validate_config(config, enforce_model=True)
        if existing is None:
            existing = manager.create(
                admin_context(binding.tenant_id), name=binding.agent_name, description="", config=config
            )
        elif existing.status != "draft" or existing.revision != 1 or existing.agent_id != bootstrap_agent_id:
            return SyncOutcome(
                binding.tenant_id,
                binding.business_entry,
                binding.app_id,
                binding.agent_name,
                "refused",
                note="existing draft is not the one this operation created",
            )
        previous = {"name": existing.name, "description": existing.description, **config.to_dict()}
    previous_workflow = previous.get("workflow_id")
    # **只有同一应用的历史版本才能参与去重。** 快照里的 `workflow_id` 属于**当时那个
    # 应用**；注册表把 agent 改绑到复制出来的新 app 之后，拿旧 id 到新 app 上导出会失败，
    # 于是"内容没变"的发布直接崩掉 —— 看起来像发布坏了，其实是把别人的历史当自己的。
    same_app = (
        previous.get("app_id") == binding.app_id and previous.get("workspace_id") == source.workspace_id
    )
    # Dify 原生 publish 每次新建版本；先确认内容相同且原不可变版本仍有效。
    if (
        same_app
        and previous.get("consumer_service") == consumer_settings
        and previous.get("workflow") == candidate
        and previous_workflow
        and workflow_content(client.export_dsl(workflow_id=previous_workflow)) == candidate
    ):
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "unchanged",
            existing.published_version,
            workflow_id=previous_workflow,
            agent_id=existing.agent_id,
            route_scenario="consumer_service" if consumer_settings is not None else "",
        )
    if not client.publish_workflow(binding.app_id):
        raise DifyDslMalformed("Dify did not confirm the workflow publish")
    workflow_id = client.published_workflow_id(binding.app_id)
    frozen = workflow_content(client.export_dsl(workflow_id=workflow_id))
    if frozen != candidate:
        raise DifyDslMalformed("workflow changed during publish")
    snapshot_body = {
        "kind": "dify-workflow",
        "app_id": binding.app_id,
        "workspace_id": source.workspace_id,
        "workflow_id": workflow_id,
        "workflow": frozen,
    }
    if consumer_settings is not None:
        snapshot_body["consumer_service"] = consumer_settings
        # 工作流与兼容读取投影必须来自同一份发布内容，不能沿用上版提示词/选库。
        snapshot_body["knowledge_base_ids"] = list(consumer_settings["knowledge_base_ids"])
        if bootstrap is not None:
            current = bootstrap({"workflow": frozen})
            snapshot_body.update(prompt=current["prompt"], model=current["model"])
    return commit_workflow_snapshot(
        manager,
        context,
        binding,
        existing,
        previous,
        snapshot_body,
        workflow_id,
        consumer_settings is not None,
    )


def commit_workflow_snapshot(
    manager, context, binding, existing, previous, snapshot_body, workflow_id, consumer
):
    """冻结图的共同持久化步骤；来源验证留在原发布器/受管工件消费方。"""
    agent = (
        manager.fork_draft(context, existing.agent_id, expected_revision=existing.revision)
        if existing.status == "published"
        else existing
    )
    version = manager.store.publish(
        agent.agent_id,
        binding.tenant_id,
        agent.revision,
        _merge_workflow_snapshot(previous, snapshot_body, name=binding.agent_name),
        published_by="dify-console",
    )
    return SyncOutcome(
        binding.tenant_id,
        binding.business_entry,
        binding.app_id,
        binding.agent_name,
        "synced",
        version.version_no,
        workflow_id=workflow_id,
        agent_id=agent.agent_id,
        route_scenario="consumer_service" if consumer else "",
    )


def sync_binding_from_dify(
    manager: AgentManager,
    context: Any,
    binding: Any,
    source: DifyDslSource,
    *,
    dry_run: bool = False,
    knowledge_registry: Mapping[str, Any] | None = None,
) -> SyncOutcome:
    """Bring one registry binding's agent up to what **Dify currently holds**.

    Everything the pull needs is derived from the registry row and the agent it
    already names — ``app_id`` from the row, and ``agent_type``,
    ``output_contract``, ``description`` and ``knowledge_base_ids`` from the
    live agent. That is the whole point of the command: the operator edits a
    prompt in Dify, and *this* turns it into a published version without anyone
    re-typing seven parameters.

    **Idempotent by comparison, not by trust.** Dify has no draft area: saving
    a config is immediately what ``export`` returns, so Dify cannot tell us
    "this is unfinished". The only honest gate is therefore *change*: if the
    pulled prompt already equals the **published** version's prompt, nothing is
    written. Without this the command would fork and publish a new version on
    every run — several identical versions, and the one the runtime serves
    changes under it each time.

    Refusals — returned as ``refused`` and **written to nothing** — are:

    * the agent the row names does not exist in this tenant;
    * it is not ``published`` (a draft or disabled agent has no live version to
      compare against, and forking one would publish a state nobody froze);
    * the published snapshot cannot be read.

    Anything the **publish gate** rejects (model off the allowlist, a knowledge
    base that no longer resolves, a contract that does not match the type) is
    not a refusal here: it raises out of the converge/publish calls and the
    caller reports it as ``failed``. Keeping the two apart matters — ``refused``
    means "this binding is not syncable as declared", ``failed`` means "it was
    syncable and something upstream said no".

    Two disagreements are **not** checked, and deliberately so: the knowledge
    binding is taken from the live agent (never the DSL — module docstring), so
    it cannot differ; and the *type* cannot differ either, because
    ``AgentStore.update`` only touches rows whose ``status = 'draft'`` — a
    published agent's config is immutable until it is forked. A guard on either
    would be dead code that reads like a check.
    """
    from aiops_diagnostics.publication.agent_lifecycle import AgentNotFound

    existing = _find_by_name(manager, context, binding.agent_name)
    if existing is None:
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "refused",
            None,
            f"库里没有这个 agent（{binding.agent_name}）—— 先按清单创建它",
        )
    if existing.status != "published" or existing.published_version is None:
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "refused",
            existing.published_version,
            f"agent 状态是 {existing.status}，没有可对比的已发布版本；先让它成为一个已发布版本",
        )

    # 发布要把路由指针推到"这一版在 Dify 里是哪张图"，所以身份要在**冻结版本之前**
    # 取到。取不到时**不阻断版本冻结**（CLI 的 `sync-from-dify` 与既有部署不依赖路由
    # 指针），但把 `workflow_id` 留空 —— 调用方必须按"未完成"处理，**不能**拿 app_id 顶替。
    workflow_id = ""
    workflow_identity_error = ""
    try:
        workflow_id = DifyDslClient(source).published_workflow_id(binding.app_id)
    except (DifyDslUnreachable, DifyDslAuthRejected, DifyDslMalformed) as exc:
        workflow_identity_error = f"读不到已发布 workflow 身份：{type(exc).__name__}"

    live = existing.config
    # ``knowledge_base_ids=None`` on purpose: it lets Dify's own dataset
    # declaration decide. Passing the live agent's ids here — which this did
    # until review — makes the knowledge bridge **dead on the sync path**,
    # because a non-``None`` value skips the resolver entirely. Sync is the
    # path an operator actually triggers, so that would have shipped a bridge
    # that never ran. ``resolve_pulled_config`` keeps the live binding by
    # itself when Dify declares nothing, which is the behaviour this preserves.
    config = resolve_pulled_config(
        manager,
        context,
        source,
        name=binding.agent_name,
        agent_type=live.agent_type,
        output_contract=live.output_contract,
        knowledge_base_ids=None,
        knowledge_registry=knowledge_registry,
        tenant_id=binding.tenant_id,
    )

    try:
        published = manager.version(context, existing.agent_id, existing.published_version)
    except AgentNotFound:  # pragma: no cover - published_version pointed at a missing row
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "refused",
            existing.published_version,
            "已发布版本的快照读不到",
        )
    snapshot = published.snapshot
    # Compare **the whole config**, never a hand-picked pair of fields.
    #
    # This was `config.prompt == snapshot["prompt"] and config.model ==
    # snapshot["model"]`, which made every OTHER field invisible: a pull that
    # changed only `knowledge_base_ids` (or any field added later) reported
    # `unchanged` and published nothing, while the report said there was
    # nothing to do. `config_from_snapshot` is the inverse of `to_dict`, so a
    # field added to the dataclass joins this comparison automatically instead
    # of needing someone to remember to extend an `and` here.
    if config == config_from_snapshot(snapshot):
        # Dify holds exactly what the live version runs — the common case.
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "unchanged",
            existing.published_version,
            workflow_identity_error,
            workflow_id=workflow_id,
        )

    if dry_run:
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "synced",
            existing.published_version,
            "dry-run：Dify 与已发布版本不同，会冻结一个新版本",
            workflow_id=workflow_id,
        )

    # Converge the live agent onto the pulled config, then publish it. This is
    # the same fork→update→publish path `admin reconcile` and `/v1/agents` use.
    agent = converge_agent_draft(
        manager, context, config, name=binding.agent_name, description=existing.description
    )
    if agent.status != "draft":
        agent = manager.get(context, agent.agent_id)
    version = manager.publish(context, agent.agent_id, expected_revision=agent.revision)
    return SyncOutcome(
        binding.tenant_id,
        binding.business_entry,
        binding.app_id,
        binding.agent_name,
        "synced",
        version.version_no,
        workflow_identity_error,
        workflow_id=workflow_id,
    )
