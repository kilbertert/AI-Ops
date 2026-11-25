"""Gateway Dify 绑定应用发布、历史版本恢复、受管试用草稿与发布审计编排。

本模块承接原 ``gateway_api.py`` 中的 Dify 发布生命周期编排，保持单向依赖
（不反向导入 ``gateway_api``）。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import status

from aiops_diagnostics.consumer import consumer_knowledge
from aiops_diagnostics.dify.dify_trial import TrialInitiator
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.gateway_config import parse_dify_knowledge_bindings
from aiops_diagnostics.identity.order_identity import order_identifier_candidates
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.publication.route_advancement import (
    _declared_in_file as _declared_in_file,
)
from aiops_diagnostics.publication.route_advancement import (
    _key_ref_for as _key_ref_for,
)
from aiops_diagnostics.publication.route_advancement import (
    advance_route_binding as _advance_route_binding,
)

_LOGGER = logging.getLogger("aiops.gateway")


def _publish_initiator(
    company_user_id: str | None,
    company_username: str | None,
    dify_account_id: str | None,
) -> tuple[str, str, str]:
    """Normalise the initiator headers (#780) — trimmed，never an authorization input."""
    return (
        (company_user_id or "").strip()[:128],
        (company_username or "").strip()[:128],
        (dify_account_id or "").strip()[:128],
    )


def _trial_run_context(context: Any, initiator: TrialInitiator | None) -> Any:
    """试运行的范围：**提交与读取都走这里**，因为两者必须算出同一个指纹。"""
    if initiator is None:
        return context.runtime.trial_run_context()
    return context.runtime.trial_context_for(initiator)


def _record_publish_audit(
    context: Any,
    *,
    app_id: str,
    action: str,
    reason_code: str,
    initiator: tuple[str, str, str],
    binding: Any = None,
    version_no: int | None = None,
) -> None:
    """Write one ledger row, best-effort. **Never raises into the publish path.**"""
    company_user_id, company_username, dify_account_id = initiator
    store = getattr(context, "store", None)
    if store is None or not hasattr(store, "record_publish_audit"):  # pragma: no cover
        return
    with contextlib.suppress(Exception):
        store.record_publish_audit(
            app_id=app_id[:128],
            action=action,
            reason_code=reason_code[:64],
            tenant_id=str(getattr(binding, "tenant_id", "") or "")[:128],
            business_entry=str(getattr(binding, "business_entry", "") or "")[:64],
            agent_name=str(getattr(binding, "agent_name", "") or "")[:128],
            company_user_id=company_user_id,
            company_username=company_username,
            dify_account_id=dify_account_id,
            version_no=version_no,
        )


def _sync_bound_app(context: Any, binding: Any, source: Any, admin: Any) -> Any:
    """按 **DSL 自己声明的应用类型** 选映射。"""
    from aiops_diagnostics.consumer import consumer_documents, consumer_publication
    from aiops_diagnostics.dify import dify_dsl_pull as pull

    target = consumer_publication.target_for(context.settings, binding)
    if not target and consumer_publication.route_scenario(context.settings, binding) == "consumer_service":
        raise ValueError("consumer publication target removed")
    if target:
        dsl = pull.DifyDslClient(source).export_dsl()
        if pull._dsl_kind(dsl) != "advanced-chat":
            raise ValueError("consumer publication requires Chatflow")
        return pull.sync_workflow_from_dify(
            context.agent_manager,
            admin,
            binding,
            source,
            consumer_settings=consumer_publication.frozen_settings(
                target,
                documents=consumer_documents.enabled(pull.workflow_content(dsl)),
            ),
            consumer_callback_base=context.settings.public_base_url,
            consumer_libraries=lambda: consumer_knowledge.owned_libraries(
                context.runtime, binding.tenant_id, binding.app_id
            ),
        )
    try:
        return pull.sync_binding_from_dify(
            context.agent_manager,
            admin,
            binding,
            source,
            knowledge_registry=parse_dify_knowledge_bindings(context.settings.dify_knowledge_bindings),
        )
    except pull.DifyDslMalformed as exc:
        if "model_config" not in str(exc):
            raise
        return pull.sync_workflow_from_dify(context.agent_manager, admin, binding, source)


class _TenantOnly:
    """只带租户的审计条目：解析失败时绑定还没查到，但"哪个租户"已经知道。"""

    tenant_id: str
    business_entry = ""
    agent_name = ""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id


def _workspace_for_tenant(
    context: Any, tenant_id: str, *, initiator: tuple[str, str, str] = ("", "", "")
) -> str:
    """拿到**这个租户**的 workspace，或拒绝（#884 发布 / #885 试运行）。"""
    try:
        return context.dify_workspaces.resolve(tenant_id, context.settings.environment)
    except DifyWorkspaceError as exc:
        _record_publish_audit(
            context,
            app_id="",
            action="refused",
            reason_code=exc.code,
            initiator=initiator,
            binding=_TenantOnly(tenant_id),
        )
        raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc


def _current_route_revision(context: Any, binding: Any, environment: str) -> int | None:
    """外部同步**之前**这条路由的 revision；没有行时 0。"""
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database:
        return None
    from aiops_diagnostics.consumer.consumer_publication import route_scenario
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    try:
        store = ChatRouteBindingStore(Path(database).expanduser().absolute())
        entry = str(getattr(binding, "business_entry", "") or "consumer")
        row = store.get(environment, binding.tenant_id, entry, route_scenario(context.settings, binding))
    except Exception as exc:  # noqa: BLE001 - 没有初始 CAS 证据，发布必须拒绝。
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable") from exc
    return row.revision if row is not None else 0


def _publish_bound_app(
    context: Any,
    app_id: str,
    *,
    initiator: tuple[str, str, str] = ("", "", ""),
    sync_bound_app_fn: Callable[[Any, Any, Any, Any], Any] = _sync_bound_app,
) -> dict[str, Any]:
    """把一个**已绑定**的 Dify app 的当前内容冻结成新的已发布版本。"""
    binding = next(
        (item for item in context.runtime.dify_app_registry.all() if item.app_id == app_id),
        None,
    )
    if binding is None:
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code="DIFY_APP_NOT_BOUND",
            initiator=initiator,
        )
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND, "DIFY_APP_NOT_BOUND", "app is not a bound agent source"
        )
    from aiops_diagnostics.consumer.agent_contract import ConsumerFlowError
    from aiops_diagnostics.dify.dify_dsl_pull import DifyDslError, DifyDslSource
    from aiops_diagnostics.publication.agent_manifest import admin_context

    console = context.settings
    if not console.dify_console_base_url or not console.dify_console_api_key:
        raise StandardAPIError(
            status.HTTP_502_BAD_GATEWAY,
            "DIFY_CONSOLE_UNAVAILABLE",
            "the Dify console credential is not configured on this server",
        )
    workspace_id = _workspace_for_tenant(context, binding.tenant_id, initiator=initiator)
    source = DifyDslSource(
        base_url=console.dify_console_base_url,
        app_id=binding.app_id,
        api_key=console.dify_console_api_key,
        workspace_id=workspace_id,
    )
    from aiops_diagnostics.consumer import consumer_publication

    consumer_target = consumer_publication.target_for(context.settings, binding)
    if consumer_target and console.chat_route_bindings_file:
        from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

        other = ChatRouteBindingStore(console.chat_route_bindings_file).get(
            console.execution_environment,
            binding.tenant_id,
            binding.business_entry,
            chat_execution.SCENARIO_OWN_ORDER_STATUS,
        )
        if other is not None and other.app_id == binding.app_id:
            raise StandardAPIError(409, "CONSUMER_ROUTE_MIGRATION_REQUIRED", "app already owns another route")
    expected_revision = _current_route_revision(
        context,
        binding,
        str(getattr(context.settings, "execution_environment", "") or ""),
    )
    from aiops_diagnostics.execution import node_publication

    node_target = node_publication.target_for(context.settings, binding)
    if node_target and consumer_target:
        raise StandardAPIError(409, "CONSUMER_TARGET_CONFLICT", "app has two execution targets")
    if console.chat_route_bindings_file and not node_target and not consumer_target:
        from aiops_diagnostics.consumer.consumer_publication import route_scenario
        from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

        managed = ChatRouteBindingStore(Path(console.chat_route_bindings_file)).get(
            console.execution_environment,
            binding.tenant_id,
            binding.business_entry,
            route_scenario(context.settings, binding),
        )
        if managed and managed.engine == "node":
            raise StandardAPIError(409, "NODE_TARGET_MISSING", "node target unavailable")
    if node_target and (not console.chat_route_bindings_file or binding.business_entry != node_target.entry):
        raise StandardAPIError(409, "NODE_ROUTE_UNAVAILABLE", "node route is not configured")
    if node_target and _declared_in_file(
        console,
        node_target.environment,
        binding.tenant_id,
        node_target.entry,
        node_target.scenario,
    ):
        raise StandardAPIError(409, "NODE_ROUTE_COLLISION", "node route is declared in both places")
    try:
        outcome = (
            node_publication.publish(
                context.agent_manager, admin_context(binding.tenant_id), binding, source, node_target
            )
            if node_target
            else sync_bound_app_fn(context, binding, source, admin_context(binding.tenant_id))
        )
    except ConsumerFlowError as exc:
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code="CONSUMER_FLOW_INVALID",
            binding=binding,
            initiator=initiator,
        )
        raise StandardAPIError(409, "CONSUMER_FLOW_INVALID", str(exc)) from exc
    except DifyDslError as exc:
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code=exc.code,
            binding=binding,
            initiator=initiator,
        )
        _LOGGER.warning("dify pull failed: %s: %s", exc.code, exc)
        raise StandardAPIError(status.HTTP_502_BAD_GATEWAY, exc.code, "dify pull failed") from exc
    except Exception as exc:  # noqa: BLE001 - 发布门自己的规则，原样转达
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code=f"PUBLISH_GATE_{type(exc).__name__}",
            binding=binding,
            initiator=initiator,
        )
        raise StandardAPIError(
            status.HTTP_409_CONFLICT, "DIFY_PUBLISH_REJECTED", f"publish rejected: {type(exc).__name__}"
        ) from exc
    if outcome.action not in {"synced", "unchanged"}:
        raise StandardAPIError(409, "DIFY_PUBLISH_REJECTED", "publish was not completed")
    if node_target:
        try:
            node_publication.advance(context, outcome, binding, node_target, expected_revision)
        except node_publication.NodeRouteConflict as exc:
            raise StandardAPIError(409, "NODE_ROUTE_NOT_ADVANCED", "node route was not advanced") from exc
    else:
        _advance_route_binding(
            context, outcome, binding, initiator=initiator, expected_revision=expected_revision
        )

    _record_publish_audit(
        context,
        app_id=outcome.app_id,
        action="published" if outcome.action == "synced" else "unchanged",
        reason_code="",
        binding=binding,
        initiator=initiator,
        version_no=outcome.version_no if isinstance(outcome.version_no, int) else None,
    )
    return {
        "app_id": outcome.app_id,
        "tenant_id": outcome.tenant_id,
        "business_entry": outcome.business_entry,
        "agent_name": outcome.agent_name,
        "action": outcome.action,
        "version_no": outcome.version_no,
        "note": outcome.note,
    }


def _managed_app_route(context: Any, app_id: str):
    from aiops_diagnostics.consumer.consumer_publication import route_scenario
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    binding = next((item for item in context.runtime.dify_app_registry.all() if item.app_id == app_id), None)
    if binding is None:
        raise StandardAPIError(404, "DIFY_APP_NOT_BOUND", "app not bound")
    if not context.settings.chat_route_bindings_file or not context.settings.execution_environment:
        raise StandardAPIError(404, "CHAT_ROUTE_BINDINGS_DISABLED", "managed routes disabled")
    store = ChatRouteBindingStore(Path(context.settings.chat_route_bindings_file))
    row = store.get(
        context.settings.execution_environment,
        binding.tenant_id,
        binding.business_entry,
        route_scenario(context.settings, binding),
    )
    if row is None or row.app_id != app_id:
        raise StandardAPIError(409, "CHAT_ROUTE_BINDING_MISMATCH", "app route not found")
    workspace = _workspace_for_tenant(context, binding.tenant_id)
    return binding, row, workspace


def _managed_app_draft(context: Any, app_id: str):
    """受管但还没成为正式版的副本：给试用一条**合成**路由，不写任何指针。"""
    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.execution.chat_route_bindings import RouteBinding

    row = SelfServiceStore(context.settings.database_file).app(app_id)
    if row is None:
        raise StandardAPIError(404, "DIFY_APP_NOT_BOUND", "app not bound")
    if row["entry"] != "consumer":
        raise StandardAPIError(409, "DIFY_APP_NOT_CONSUMER", "draft preview is for consumer apps")
    if not context.settings.chat_route_bindings_file or not context.settings.execution_environment:
        raise StandardAPIError(404, "CHAT_ROUTE_BINDINGS_DISABLED", "managed routes disabled")
    workspace = _workspace_for_tenant(context, row["tenant_id"])
    binding = DifyAppBinding(row["tenant_id"], "consumer", app_id, row["agent_name"])
    route = RouteBinding(
        environment=context.settings.execution_environment,
        tenant_id=row["tenant_id"],
        entry="consumer",
        scenario="consumer_service",
        engine="dify",
        agent_id="",
        agent_version_no=0,
        app_id=app_id,
        state="draft",
        revision=0,
    )
    return binding, route, workspace


def _tenant_in_scope(record_tenant: object, allowed_tenant: object) -> bool:
    """这一行的租户是否落在给定范围内 —— 用共享规则判，不就地写比较。"""
    from aiops_diagnostics.identity.order_visibility import (
        TenantVisibility,
        normalize_tenant,
        visible_orders,
    )

    allowed = normalize_tenant(allowed_tenant)
    if allowed is None:
        return False
    visibility = TenantVisibility(allowed=frozenset({allowed}))
    return bool(visible_orders([{"tenant_id": record_tenant}], visibility).rows)


def _workflow_trial_draft(
    context: Any, app_id: str, run_context: ScopeContext, question="check order status", selected_order=""
):
    from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, DifyDslSource
    from aiops_diagnostics.dify.dify_workflow_trial import WorkflowTrial

    try:
        binding, row, workspace = _managed_app_route(context, app_id)
        draft = False
    except StandardAPIError:
        binding, row, workspace = _managed_app_draft(context, app_id)
        draft = True
    if (not draft and not row.active) or not _tenant_in_scope(
        binding.tenant_id, run_context.effective_tenant_id
    ):
        raise StandardAPIError(403, "TRIAL_SCOPE_MISMATCH", "trial scope does not match the bound app")
    source = DifyDslSource(
        context.settings.dify_console_base_url, app_id, context.settings.dify_console_api_key, workspace
    )
    if row.engine == "node":
        from aiops_diagnostics.execution.node_publication import NodeTrial, candidate, target_for

        target = target_for(context.settings, binding)
        if target is None:
            raise StandardAPIError(409, "NODE_TARGET_MISSING", "node target unavailable")
        if target.entry == "operator":
            raise StandardAPIError(
                409, "OPERATOR_TRIAL_IDENTITY_REQUIRED", "operator trial requires company authorization"
            )
        return NodeTrial(candidate(source, target), row)
    dsl = DifyDslClient(source).export_dsl()
    if row.scenario == chat_execution.SCENARIO_CONSUMER_SERVICE:
        from aiops_diagnostics.consumer import consumer_documents
        from aiops_diagnostics.consumer.agent_contract import publication_settings
        from aiops_diagnostics.consumer.consumer_publication import frozen_settings, target_for
        from aiops_diagnostics.consumer.consumer_trial import ConsumerTrial
        from aiops_diagnostics.dify.dify_dsl_pull import workflow_content

        target = target_for(context.settings, binding)
        if target is None:
            raise StandardAPIError(409, "CONSUMER_TARGET_MISSING", "consumer trial target missing")
        candidates = set(order_identifier_candidates(question))
        if candidates and candidates != {selected_order}:
            raise StandardAPIError(400, "TRIAL_ORDER_MISMATCH", "试用问题与选定订单不一致")
        previous = (
            context.agent_manager.store.version(row.agent_id, row.tenant_id, row.agent_version_no).snapshot
            if row.agent_id and row.agent_version_no
            else {}
        )
        try:
            frozen = publication_settings(
                workflow_content(dsl),
                frozen_settings(target, documents=consumer_documents.enabled(workflow_content(dsl))),
                context.settings.public_base_url,
                require_split=(previous.get("consumer_service") or {}).get("callback_version", 1) >= 2,
                require_picker=(previous.get("consumer_service") or {}).get("callback_version", 1) >= 3,
                owned_libraries=lambda: consumer_knowledge.owned_libraries(
                    context.runtime, row.tenant_id, row.app_id
                ),
            )
        except ValueError as exc:
            raise StandardAPIError(409, "CONSUMER_FLOW_INVALID", str(exc)) from exc
        return ConsumerTrial(dsl, source, row, frozen, question, draft=draft)
    return WorkflowTrial(dsl, source, row)


def _restore_bound_app(context: Any, app_id: str, version_no: int, initiator: tuple[str, str, str]):
    from aiops_diagnostics.dify.dify_dsl_pull import (
        DifyDslClient,
        DifyDslSource,
        SyncOutcome,
        workflow_content,
    )

    binding, row, workspace = _managed_app_route(context, app_id)
    version = context.agent_manager.store.version(row.agent_id, binding.tenant_id, version_no)
    snapshot = version.snapshot
    if row.engine == "node":
        from aiops_diagnostics.execution import node_publication

        target = node_publication.target_for(context.settings, binding)
        if (
            target is None
            or snapshot.get("kind") != "node-sop"
            or snapshot.get("app_id") != app_id
            or snapshot.get("workspace_id") != workspace
        ):
            raise StandardAPIError(409, "NODE_VERSION_MISMATCH", "node version unavailable")
        execution_manifest.from_text(snapshot["node_manifest"])
        outcome = SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            app_id,
            binding.agent_name,
            "synced",
            version_no,
            agent_id=row.agent_id,
        )
        node_publication.advance(context, outcome, binding, target, row.revision)
        _record_publish_audit(
            context,
            app_id=app_id,
            action="restored",
            reason_code="",
            binding=binding,
            initiator=initiator,
            version_no=version_no,
        )
        return {"app_id": app_id, "action": "synced", "version_no": version_no}
    workflow_id = snapshot.get("workflow_id")
    if (
        snapshot.get("kind") != "dify-workflow"
        or snapshot.get("app_id") != app_id
        or snapshot.get("workspace_id") != workspace
        or not workflow_id
    ):
        raise StandardAPIError(409, "DIFY_VERSION_MISMATCH", "version is not a verified app workflow")
    source = DifyDslSource(
        context.settings.dify_console_base_url, app_id, context.settings.dify_console_api_key, workspace
    )
    if workflow_content(DifyDslClient(source).export_dsl(workflow_id=workflow_id)) != snapshot.get(
        "workflow"
    ):
        raise StandardAPIError(409, "DIFY_VERSION_MISMATCH", "published workflow differs from its snapshot")
    outcome = SyncOutcome(
        binding.tenant_id,
        binding.business_entry,
        app_id,
        binding.agent_name,
        "synced",
        version_no,
        workflow_id=workflow_id,
        agent_id=row.agent_id,
        route_scenario=row.scenario,
    )
    _advance_route_binding(context, outcome, binding, initiator=initiator, expected_revision=row.revision)
    _record_publish_audit(
        context,
        app_id=app_id,
        action="restored",
        reason_code="",
        binding=binding,
        initiator=initiator,
        version_no=version_no,
    )
    return {"app_id": app_id, "action": "synced", "version_no": version_no}


def _publish_allowlist(configured: tuple[str, ...], settings: Any) -> tuple[str, ...]:
    """发布门的模型白名单 = 平台注册表的模型 + 各 profile 已批准的端模型。"""
    profiles_file = getattr(settings, "operator_profiles_file", "")
    if not profiles_file:
        return configured
    from aiops_diagnostics.dify.self_service_config import approved_model_names

    return tuple(dict.fromkeys([*configured, *approved_model_names(settings)]))
