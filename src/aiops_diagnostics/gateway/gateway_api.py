from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from aiops_diagnostics import __version__
from aiops_diagnostics.common.async_job_lifecycle import DIAGNOSIS, QUESTION
from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.i18n import (
    DEFAULT_LANGUAGE,
    QA_FALLBACK_MESSAGES,
    clarification_message,
    diagnosis_error_message,
    effective_language,
    free_text_unavailable_message,
    localized_pack,
    rendering_language,
)
from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file, write_private_text
from aiops_diagnostics.common.scalar_rules import format_stored_time
from aiops_diagnostics.consumer import consumer_knowledge
from aiops_diagnostics.consumer import knowledge_access as consumer_knowledge_selection
from aiops_diagnostics.consumer.presentation import (
    COMPOSED_LANGUAGES,
    asks_latest_order,
    status_answer,
)
from aiops_diagnostics.conversation.clarification import (
    CLARIFY_LANGUAGE_UNROUTABLE,
    CLARIFY_ORDER_REQUIRED,
    MESSAGE_KEY_BY_CODE,
    missing_fields_for,
)
from aiops_diagnostics.conversation.conversation_store import (
    ConversationBusy,
    ConversationError,
    ConversationStore,
)
from aiops_diagnostics.conversation.faq import (
    PLATFORM_AMBIGUOUS,
    PLATFORM_FORBIDDEN,
    PLATFORM_UNAVAILABLE,
    FAQCatalog,
    FAQError,
    MySQLPlatformDirectory,
    PlatformIdentityResolver,
)
from aiops_diagnostics.data.charging_status import ChargingStatusError, read_charging_status
from aiops_diagnostics.data.sources import SourceError, scoped_mysql_source
from aiops_diagnostics.dify.dify_run_store import DifyRunStore
from aiops_diagnostics.dify.dify_trial import TrialInitiator
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError, DifyWorkspaces
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import (
    AgentRun,
    AgentRunStore,
    scope_from_json,
)
from aiops_diagnostics.execution.agent_runs import (
    SnailDispatcher as SnailDispatcher,
)
from aiops_diagnostics.execution.run_deliverability import deliverable
from aiops_diagnostics.gateway import metrics_store as metrics_dims
from aiops_diagnostics.gateway import request_observation
from aiops_diagnostics.gateway.dependencies import (
    STANDARD_DIAGNOSIS_SCOPE as STANDARD_DIAGNOSIS_SCOPE,
)
from aiops_diagnostics.gateway.dependencies import (
    STANDARD_FAQ_SCOPE as STANDARD_FAQ_SCOPE,
)
from aiops_diagnostics.gateway.dependencies import (
    STANDARD_ORDER_READ_SCOPE as STANDARD_ORDER_READ_SCOPE,
)
from aiops_diagnostics.gateway.dependencies import (
    build_dependencies,
    install_exception_handlers,
)
from aiops_diagnostics.gateway.dependencies import (
    localized_copy_language as localized_copy_language,
)
from aiops_diagnostics.gateway.dependencies import (
    request_language as request_language,
)
from aiops_diagnostics.gateway.errors import StandardAPIError as StandardAPIError
from aiops_diagnostics.gateway.gateway_config import (
    GatewayServerSettings,
    parse_dify_knowledge_bindings,
)
from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime, close_gateway_runtime
from aiops_diagnostics.gateway.gateway_store import (
    ACTIVE_DIAGNOSIS_STATUSES,
    ACTIVE_HEALTH_JOB_STATUSES,
    TERMINAL_DIAGNOSIS_STATUSES,
    GatewayStore,
)
from aiops_diagnostics.gateway.routers import (
    agents as agents_router,
)
from aiops_diagnostics.gateway.routers import (
    assistant as assistant_router,
)
from aiops_diagnostics.gateway.routers import (
    conversations as conversations_router,
)
from aiops_diagnostics.gateway.routers import (
    dify_console as dify_console_router,
)
from aiops_diagnostics.gateway.routers import (
    files as files_router,
)
from aiops_diagnostics.gateway.routers import (
    health_analysis as health_analysis_router,
)
from aiops_diagnostics.gateway.routers import (
    internal_callbacks as internal_callbacks_router,
)
from aiops_diagnostics.gateway.routers import (
    knowledge as knowledge_router,
)
from aiops_diagnostics.gateway.routers import (
    orders_sites as orders_sites_router,
)
from aiops_diagnostics.gateway.routers import (
    shortcuts as shortcuts_router,
)
from aiops_diagnostics.gateway.schemas import (
    AgentActionRequest as AgentActionRequest,
)
from aiops_diagnostics.gateway.schemas import (
    AgentConfigRequest as AgentConfigRequest,
)
from aiops_diagnostics.gateway.schemas import (
    AgentCreateRequest as AgentCreateRequest,
)
from aiops_diagnostics.gateway.schemas import (
    AgentUpdateRequest as AgentUpdateRequest,
)
from aiops_diagnostics.gateway.schemas import (
    AssistantQuestionRequest as AssistantQuestionRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ChargingStatusDelegationRequest as ChargingStatusDelegationRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ConversationCreateRequest as ConversationCreateRequest,
)
from aiops_diagnostics.gateway.schemas import (
    DifyPublishRequest as DifyPublishRequest,
)
from aiops_diagnostics.gateway.schemas import (
    DifyRestoreRequest as DifyRestoreRequest,
)
from aiops_diagnostics.gateway.schemas import (
    DifyRetractRequest as DifyRetractRequest,
)
from aiops_diagnostics.gateway.schemas import (
    DifyTrialRequest as DifyTrialRequest,
)
from aiops_diagnostics.gateway.schemas import (
    FAQAnswerRequest as FAQAnswerRequest,
)
from aiops_diagnostics.gateway.schemas import (
    HealthReportJobRequest as HealthReportJobRequest,
)
from aiops_diagnostics.gateway.schemas import (
    KnowledgeSourceRequest as KnowledgeSourceRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ManagedTrialRequest as ManagedTrialRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ManagedTrialResultRequest as ManagedTrialResultRequest,
)
from aiops_diagnostics.gateway.schemas import (
    NodeCapabilitiesRequest as NodeCapabilitiesRequest,
)
from aiops_diagnostics.gateway.schemas import (
    NodeClaimRequest as NodeClaimRequest,
)
from aiops_diagnostics.gateway.schemas import (
    NodeLoopCompletionRequest as NodeLoopCompletionRequest,
)
from aiops_diagnostics.gateway.schemas import (
    OperationsAnalysisRequest as OperationsAnalysisRequest,
)
from aiops_diagnostics.gateway.schemas import (
    OperatorPreviewDraft as OperatorPreviewDraft,
)
from aiops_diagnostics.gateway.schemas import (
    OperatorPreviewTool as OperatorPreviewTool,
)
from aiops_diagnostics.gateway.schemas import (
    OperatorPublishRequest as OperatorPublishRequest,
)
from aiops_diagnostics.gateway.schemas import (
    OperatorToolAuthorizationRequest as OperatorToolAuthorizationRequest,
)
from aiops_diagnostics.gateway.schemas import (
    OperatorTrialCancelRequest as OperatorTrialCancelRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ShortcutActionRequest as ShortcutActionRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ShortcutCreateRequest as ShortcutCreateRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ShortcutFieldsRequest as ShortcutFieldsRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ShortcutRollbackRequest as ShortcutRollbackRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ShortcutUpdateRequest as ShortcutUpdateRequest,
)
from aiops_diagnostics.identity.caller_auth import (
    CALLER_AUTH_CONFIG_MISSING,
    CALLER_AUTH_FORBIDDEN,
    SOURCE_KEY_HEADER,
    CallerAuthError,
    CallerContextResolver,
    DisabledCallerResolver,
    DisabledOrderAuthorizer,
    EntryCallerResolver,
    IntrospectionCallerResolver,
    IntrospectionSettings,
    OrderAuthorizer,
    ScopedOrderAuthorizer,
    SourceKeyCallerResolver,
    UpmsCallerResolver,
)
from aiops_diagnostics.identity.company_token_auth import (
    CompanyTokenCallerResolver,
    CompanyTokenSettings,
    PermissionSource,
)
from aiops_diagnostics.identity.order_identity import (
    extract_order_identifier,
    is_order_identifier,
    order_identifier_candidates,
)
from aiops_diagnostics.identity.query_scope import (
    ShopDirectory,
    UpmsShopDirectory,
    resolve_query_scope,
)
from aiops_diagnostics.identity.scope_context import (
    SCOPE_TYPE_SELF,
    ScopeContext,
    ScopeError,
)
from aiops_diagnostics.identity.third_session_auth import (
    BSubjectDirectory,
    OperatorSiteScope,
    RedisThirdSessionResolver,
    ThirdSessionSettings,
    UpmsBSubjectDirectory,
    UpmsOperatorSiteScope,
)
from aiops_diagnostics.publication.agent_lifecycle import (
    AgentManager,
    AgentStore,
    allowed_models_from_settings,
)
from aiops_diagnostics.publication.shortcut_lifecycle import (
    OPERATIONS_REVIEW_CODE,
    ShortcutManager,
    ShortcutStore,
)

_LOGGER = logging.getLogger("aiops.gateway")


class GatewayAPI:
    def __init__(
        self,
        settings: GatewayServerSettings,
        store: GatewayStore,
        runtime: GatewayRuntime,
        caller_resolver: CallerContextResolver,
        order_authorizer: OrderAuthorizer,
        platform_resolver: PlatformIdentityResolver,
        faq_catalog: FAQCatalog,
        agent_manager: AgentManager | None = None,
        shortcut_manager: ShortcutManager | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.caller_resolver = caller_resolver
        self.order_authorizer = order_authorizer
        self.platform_resolver = platform_resolver
        self.faq_catalog = faq_catalog
        self.agent_manager = agent_manager
        # Shortcut store shares the gateway database file (its own tables);
        # lazy so existing embedders (tests) need no new argument (#230).
        self.shortcut_manager = shortcut_manager or ShortcutManager(ShortcutStore(store.path))
        # Conversation store shares the gateway database file (its own
        # tables); lazy so existing embedders (tests) need no new argument.
        self.conversation_store = ConversationStore(store.path)
        # M2-CLOSE-01：节点委派的请求级授权记录。同样是共用数据库文件的独立表，
        # 惰性构造，既有嵌入方不需要新增参数。
        self.agent_run_store = AgentRunStore(store.path)
        # `dify_workspaces`（#879 / ADR-0059）：一个租户在一个环境里的 workspace。同一个
        # 库文件、自己的表，惰性构造 —— 既有嵌入方不需要新增参数。放在这里而不是
        # ``GatewayRuntime``，因为写它的（开户，下一张票）与读它的（发布桥、试运行、
        # 运维 CLI）都以网关的库为唯一真值，而 ``GatewayRuntime`` 在测试里常是替身。
        self.dify_workspaces = DifyWorkspaces(store.path)
        # APP-RUNTIME-02：Dify 路线的执行记录。同一个库文件、自己的表；
        # **不**复用 `agent_runs` 的节点列（那会把"这次没有节点"写成"节点核对过了"）。
        self.dify_run_store = DifyRunStore(store.path)
        from aiops_diagnostics.operator.attachments import AssistantFileStore
        from aiops_diagnostics.operator.download_center import DownloadCenterClient
        from aiops_diagnostics.operator.feedback import AssistantFeedbackStore
        from aiops_diagnostics.operator.qa_events import QAEventStore

        self.assistant_files = AssistantFileStore(store.path)
        self.download_center = DownloadCenterClient.from_settings(settings)
        self.qa_events = QAEventStore(store.path)
        self.assistant_feedback = AssistantFeedbackStore(store.path)


def _publish_initiator(
    company_user_id: str | None,
    company_username: str | None,
    dify_account_id: str | None,
) -> tuple[str, str, str]:
    """Normalise the initiator headers (#780) — trimmed，never an authorization input.

    Empty means "unknown" and is recorded as such. The values are redacted to the
    store's own scope charset by the caller's ``_scope`` guard, so a malformed
    header cannot smuggle anything into the ledger.
    """
    return (
        (company_user_id or "").strip()[:128],
        (company_username or "").strip()[:128],
        (dify_account_id or "").strip()[:128],
    )


def _trial_run_context(context: Any, initiator: TrialInitiator | None) -> Any:
    """试运行的范围：**提交与读取都走这里**，因为两者必须算出同一个指纹。

    作业行按 ``scope_fingerprint`` 隔离（`gateway_store.get_standard_diagnosis`），
    而那个哈希含数据范围与主体类型 —— 提交用 A 范围、读取用 B 范围时，返回的
    ``trial_id`` 会**永远读不回来**，且表现与"这个 id 不存在"完全一样。所以
    "读结果"这条路由一开始就漏传发起人头是个真缺陷（#782 留下的 v2 地雷），
    只有把选择收成一处才不会重演。

    ``initiator is None``（桥没给全身份，或本来就没走公司身份）⇒ 固定调试租户，
    即 #781 的第一版行为，逐字不变。
    """
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
    """Write one ledger row, best-effort. **Never raises into the publish path.**

    The ledger is observability: a disk problem must not turn a legitimate
    publish into a failure, and a missing initiator must not either (it is
    recorded as empty, not refused). Values are trimmed and length-capped so a
    caller-supplied header cannot bloat a row.
    """
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


def _declared_in_file(settings: Any, environment: str, tenant_id: str, entry: str, scenario: str) -> bool:
    """这条 key 是不是仍由**文件登记**声明。

    启动期查过一次冲突，但那之后文件可能被重新配上；而发布是唯一会把冲突**写进去**
    的动作，所以它也要查一次 —— 写进去之后再发现，故障是整表加载失败，波及所有租户。
    """
    from aiops_diagnostics.execution import chat_execution

    try:
        bindings = chat_execution.load_bindings(
            str(getattr(settings, "chat_execution_bindings_file", "") or "")
        )
    except chat_execution.ChatBindingError as exc:
        raise StandardAPIError(503, "EXEC_CONFIG_UNAVAILABLE", "bindings unavailable") from exc
    key = f"{scenario}:{environment}:{tenant_id}"
    return any(item.key == key for item in bindings)


def _key_ref_for(key_dir: str, app_id: str) -> str:
    """这个 app 的 Service API 凭据路径，且**必须可读**。

    发布只推路径、不读密钥；但一条指向不存在文件的路径等于"发布了也跑不起来"。
    """
    from aiops_diagnostics.dify.dify_chat_workflow import load_workflow_key
    from aiops_diagnostics.execution import chat_execution

    if not key_dir:
        return ""
    path = chat_execution.key_file_for_app(key_dir, app_id)
    try:
        load_workflow_key(path)
    except ValueError as exc:
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_KEY_MISSING",
            "the app service key is not provisioned on this server",
        ) from exc
    return path


def _sync_bound_app(context: Any, binding: Any, source: Any, admin: Any) -> Any:
    """按 **DSL 自己声明的应用类型** 选映射。

    workflow 应用没有 `model_config`，把它送进 chat 那条映射会得到
    `DSL has no model_config object` —— 那看起来像"DSL 坏了"，其实是"这是另一种应用"。

    分流放在这里、而不是入口多调一次导出：`sync_binding_from_dify` 自己本来就要导，
    在入口再加一次导出会多一跳外部调用（测试的假上游也会因此被打中）。
    所以这里先按 chat 走；只有它明确报"没有 model_config"时才改走 workflow ——
    **用结果判种类，不用猜测**。
    """
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


def _workspace_for_tenant(
    context: Any, tenant_id: str, *, initiator: tuple[str, str, str] = ("", "", "")
) -> str:
    """拿到**这个租户**的 workspace，或拒绝（#884 发布 / #885 试运行）。

    失败时把 ``resolve`` 的四个码原样转成 HTTP：``UNRESOLVED``(404) / ``CREATING``(409,
    可重试) / ``FAILED``(409) / ``UNKNOWN``(409)。合成一句话就把"去开户 / 稍后重试 /
    先对账"这三种下一步抹平了 —— 而发布这条路**必须**让人知道该做哪一件。

    审计账本也记一笔：被拒的发布尝试要能回答"谁试图把哪一版发进哪个租户"。
    """

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


class _TenantOnly:
    """只带租户的审计条目：解析失败时绑定还没查到，但"哪个租户"已经知道。"""

    tenant_id: str
    business_entry = ""
    agent_name = ""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id


def _publish_bound_app(
    context: Any,
    app_id: str,
    *,
    initiator: tuple[str, str, str] = ("", "", ""),
) -> dict[str, Any]:
    """把一个**已绑定**的 Dify app 的当前内容冻结成新的已发布版本。

    **两条路共用的唯一实现**（共享密钥路由与 operator 会话身份路由）：一处实现，
    两条路就不可能漂。谁能调到这一层由各自的**门**决定 —— 这里只做「点名一个
    已声明的绑定，把它现在的内容过一遍发布门」。

    爆炸半径是设计出来的：请求只带 ``app_id``，租户/agent 名/类型/契约/知识库
    全部来自 ``dify_app_registry``。最坏情况因此是**重新冻结一个已声明绑定的当前
    内容**；重复发布未变内容按构造是 no-op（先与已发布快照逐字比较）。

    ``initiator``（#780）是**审计元数据**：``(company_user_id, company_username,
    dify_account_id)``。它**不是**授权依据 —— 授权在桥那一侧（公司发布权限码）
    已经完成；这里把它记进审计账本，好回答"谁在何时把哪一版发上了生产"。三个字段
    都可能是空串（调用方没带），那记成未知，发布照常。
    """
    company_user_id, company_username, dify_account_id = initiator
    binding = next(
        (item for item in context.runtime.dify_app_registry.all() if item.app_id == app_id),
        None,
    )
    if binding is None:
        # 与未登记的知识句柄同形：404 说"不在这里"，不透露有哪些绑定。
        # **被拒的尝试也入账**（#780）：未绑定的 app_id 是"有人试图发布一个没登记的东西"，
        # 正是审计要看见的；它没有任何租户可归，所以租户留空。
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
    # **按绑定的租户解析 workspace**（#884 / ADR-0059 §5.2）。`_publish_bound_app` 是两条
    # 路共用的唯一实现，所以这一处一改，共享密钥路由与 operator 会话路由同时生效。
    #
    # 为什么不能再用 `dify_console_workspace_id` 那个常量：多 workspace 之后它是"某一个人
    # 的工作区"，而这里要的是**这个 app 所属租户**的工作区。用常量意味着 A 租户的发布
    # 会跑在 B 的工作区上 —— 设计里点名这是最容易漏掉的越权口。
    #
    # 解析失败 ⇒ **拒绝，不回落任何默认 workspace**：回落等于把"这个租户还没开户"悄悄
    # 变成"发布进别人的工作区"，而后者看起来是成功的。
    workspace_id = _workspace_for_tenant(context, binding.tenant_id, initiator=initiator)
    source = DifyDslSource(
        base_url=console.dify_console_base_url,
        app_id=binding.app_id,
        api_key=console.dify_console_api_key,
        workspace_id=workspace_id,
    )
    # **revision 必须在外部同步之前快照。** 同步期间可能有人撤回或另发一版；
    # 等同步回来再读，读到的是**对方之后的** revision，于是这次发布仍会 CAS 通过，
    # 把一次已经成功的撤回重新置为 active —— 实测复现过（V1 → 撤回 → 延迟发布
    # 推进，最终 active / revision 3 / V2）。用快照值推进，期间有变动就冲突。
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
            else _sync_bound_app(context, binding, source, admin_context(binding.tenant_id))
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
        # Dify 自己拒绝或不可达：带它自己的码转达，调用方要能区分「凭据被拒」与「服务挂了」。
        # 被拒的**尝试**一样入账；租户与 agent 名此刻是知道的（绑定查到了）。
        _record_publish_audit(
            context,
            app_id=app_id,
            action="refused",
            reason_code=exc.code,
            binding=binding,
            initiator=initiator,
        )
        # 响应只说稳定码（上游正文可能带内部地址）；**日志**要留原因，
        # 否则"拉取失败"在运维眼里是一句没有下一步的话。
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
    # **推进正式指针**：这一步才是"运营点了发布，App 真的切了版"。它必须在
    # `_record_publish_audit` 之前 —— 审计要记的是已经成立的事实，不是待办。
    #
    # 跨系统没有事务：Dify 那边已经发布了，本地这一步仍可能失败。失败时**保留旧正式版**
    # 并如实抛错（不伪称成功），任务书对这条的要求是"报未完成"，不是"补一个异步重试"。
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

    # 成功：记版本号与动作（``synced`` / ``unchanged``）。``unchanged`` 也是"成功的一次
    # 发布尝试"，只是内容没变 —— 审计要能分辨它，否则"某人点了一百次但什么都没发"与
    # "发了 0 次"看起来一样。
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


def _current_route_revision(context: Any, binding: Any, environment: str) -> int | None:
    """外部同步**之前**这条路由的 revision；没有行时 0。

    同步可能耗时几十秒，期间别人可以撤回或另发一版。推进指针要用这个快照值做 CAS，
    而不是同步回来之后才读 —— 否则这次发布会把别人的撤回覆盖掉。
    """
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database:
        return None
    from pathlib import Path

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    try:
        store = ChatRouteBindingStore(Path(database).expanduser().absolute())
        entry = str(getattr(binding, "business_entry", "") or "consumer")
        from aiops_diagnostics.consumer.consumer_publication import route_scenario

        row = store.get(environment, binding.tenant_id, entry, route_scenario(context.settings, binding))
    except Exception as exc:  # noqa: BLE001 - 没有初始 CAS 证据，发布必须拒绝。
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable") from exc
    return row.revision if row is not None else 0


def _advance_route_binding(
    context: Any,
    outcome: Any,
    binding: Any,
    *,
    initiator: tuple[str, str, str],
    expected_revision: int | None = None,
) -> None:
    """发布成功之后，把（环境，租户，入口，场景）的正式指针推到这一版。

    **拒绝的三种情况**，都不写指针、并如实报未完成：

    * 没有配 `AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE`：这个环境还没接管管理指针，
      发布只冻结版本（与今天逐字一致）；
    * `version_no` 缺失或 `workflow_id` 取不到：拿不到"哪一版、哪张图"就没有可切的东西；
    * 已有发布者在本轮期间改过指针（`revision` 变化）：CAS 失败，不覆盖别人的发布。

    成功时写入的身份是**三列分开**的：`app_id` 是应用、`workflow_id` 是图、
    版本是本地那一条快照。把同一个值填进多列，等于三列都在撒谎。
    """
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    environment = str(getattr(context.settings, "execution_environment", "") or "")
    if not database or not environment:
        return
    # **只有 workflow 应用才走"推进路由指针"这一步。** 同一个网关还可以挂着
    # chat/completion 应用（`dify_workspace_template.dsl.yml` 里就有 `mode: chat`），
    # 它们没有已发布 workflow，`sync_binding_from_dify` 本来就能正常冻结 Agent 版本。
    # 无条件要求 workflow 身份会让那些应用**发布失败**，而它们的版本其实已经变了 ——
    # 调用方看到的是失败，库里却已经改过，那比不发布更糟。
    #
    # 判据落在**这次同步实际冻结了什么**上：workflow 应用才带 `workflow_id`。
    if not getattr(outcome, "workflow_id", ""):
        return
    version_no = outcome.version_no
    if not isinstance(version_no, int):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_ROUTE_NOT_ADVANCED",
            "the published version has no workflow identity to bind",
        )
    from pathlib import Path

    from aiops_diagnostics.execution.chat_route_bindings import (
        ChatRouteBindingStore,
        RouteBindingError,
    )

    key_dir = str(getattr(context.settings, "chat_dify_key_dir", "") or "")
    entry = str(getattr(binding, "business_entry", "") or "consumer")
    from aiops_diagnostics.consumer.consumer_publication import route_scenario

    scenario = getattr(outcome, "route_scenario", "") or route_scenario(context.settings, binding)
    # **两个前置必须在写指针之前拒绝**，理由相同：写进去之后才发现，故障会扩散到
    # 别人身上 —— 这两条都会让整张管理表加载失败，而 `_bindings` 在读租户之前
    # 就检查整表，所以一个租户的坏行会让**所有**租户的聊天路线一起不可用。
    #
    #   1. 没配凭据目录 ⇒ 写进去一条空引用，下次加载 `load_workflow_key("")` 抛错；
    #   2. 文件登记里仍有同一个 key ⇒ 判重复声明，同样整表不可用。
    if not key_dir:
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_KEY_DIR_UNSET",
            "the app service key directory is not configured on this server",
        )
    # 执行地址同理：控制台拉取用的是 `dify_console_base_url`，所以**发布能成功**，
    # 而之后每个聊天任务都会在空地址上失败；重启还会被"已有活动 Dify 行"的启动校验
    # 拒掉 —— 等于把自己锁在门外。写第一条活动指针之前就要拒绝。
    if not str(getattr(context.settings, "dify_chat_base_url", "") or ""):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "DIFY_CHAT_BASE_URL_UNSET",
            "the dify chat base url is not configured on this server",
        )
    if _declared_in_file(context.settings, environment, binding.tenant_id, entry, scenario):
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "CHAT_ROUTE_BINDING_COLLIDES_WITH_FILE",
            "this route is still declared in the bindings file",
        )
    if scenario == chat_execution.SCENARIO_CONSUMER_SERVICE and getattr(context, "agent_manager", None):
        from aiops_diagnostics.publication.agent_lifecycle import AgentError

        try:
            snapshot = context.agent_manager.store.version(
                outcome.agent_id, binding.tenant_id, version_no
            ).snapshot
        except AgentError as exc:
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "CONSUMER_VERSION_UNAVAILABLE",
                "selected consumer version is unavailable",
            ) from exc
        frozen = snapshot.get("consumer_service") if isinstance(snapshot, dict) else None
        content = frozen.get("content") if isinstance(frozen, dict) else None
        if isinstance(content, dict) and content.get("environment") not in (None, environment):
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "CONSUMER_VERSION_ENVIRONMENT_MISMATCH",
                "selected version belongs to another execution environment",
            )

    store = ChatRouteBindingStore(Path(database).expanduser().absolute())
    existing = store.get(environment, binding.tenant_id, entry, scenario)
    if (
        outcome.action == "unchanged"
        and existing is not None
        and existing.agent_id == outcome.agent_id
        and existing.agent_version_no == version_no
        and existing.workflow_id == outcome.workflow_id
    ):
        if expected_revision is not None and existing.revision != expected_revision:
            raise StandardAPIError(409, "ROUTE_BINDING_CONFLICT", "route binding moved")
        # **已撤回的版本不能被"再发一次"复活**（与 `node_publication.advance` 同一条）。
        # 撤回是显式决定，恢复也是显式动作；内容没变只说明不必冻结新版本，不说明操作者
        # 想让它重新上线。2026-11-01 在 41 上实测：撤回后再点发布，这里返回 unchanged
        # 之后仍会走到底下的 `store.publish()`，把刚撤回的版本推回 active。
        if existing.active:
            return
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "ROUTE_BINDING_RETRACTED",
            "该版本已撤回；要重新上线请用「恢复历史版本」",
        )
    if expected_revision is None:
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "initial route revision unavailable")
    try:
        store.publish(
            environment=environment,
            tenant_id=binding.tenant_id,
            entry=entry,
            scenario=scenario,
            engine="dify",
            agent_id=outcome.agent_id,
            agent_version_no=version_no,
            app_id=binding.app_id,
            workflow_id=outcome.workflow_id,
            # 凭据只存**引用**：`<key_dir>/<app_id>.key`，由运维预置。发布只推路径，
            # 全程不读密钥 —— 于是它也没有机会出现在表里。
            #
            # **但路径必须已存在**：发布成功却指向一个不存在的凭据文件，下一次请求
            # 必然 `DIFY_UNAVAILABLE` —— 而那个症状离发布这一步很远，没人会想到是它。
            # 宁可让发布失败并说清楚，也不要留下一条"看起来发布了"的绑定。
            workflow_key_ref=_key_ref_for(key_dir, binding.app_id),
            expected_revision=expected_revision,
            updated_by=initiator[0] or initiator[1] or "",
        )
    except RouteBindingError as exc:
        raise StandardAPIError(status.HTTP_409_CONFLICT, exc.code, "route binding was not advanced") from exc


def _managed_app_route(context: Any, app_id: str):
    from pathlib import Path

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
    """受管但还没成为正式版的副本：给试用一条**合成**路由，不写任何指针。

    `state="draft"` 与 `revision=0` 是有意的：它们让任何"按正式版解释这次试用"的代码
    当场失败，而不是悄悄把草稿当成正式版。真正需要成立的事实只有两条 ——
    这个应用属于这个客户，且它的工作区可解析。
    """
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
        # 受管但**尚未发布**的副本：试用按它当前的内容跑。发布前先试是产品要求
        # （"未发布副本可先预览，不要求先有正式版"），所以这里不能沿用"必须有正式路由"。
        binding, row, workspace = _managed_app_draft(context, app_id)
        draft = True
    # 试用的范围必须落在**绑定所属租户**上。租户那一维走共享规则
    # （`order_visibility.visible_orders`）："这一行属于哪个租户、在不在范围内"
    # 在这个仓只有一处定义，不在这里手写比较。
    # 草稿（`state="draft"`）**不是**正式路线，但它仍然要能试用：那正是"先试再发"的顺序。
    # 所以这里只对**正式**路线要求 active；草稿要求的是"这个副本仍属于这个客户"，
    # 那一条由 `ConsumerTrial.require_current` 的草稿分支查目录来保证。
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
        from aiops_diagnostics.consumer.consumer_publication import frozen_settings, target_for
        from aiops_diagnostics.consumer.consumer_trial import ConsumerTrial

        target = target_for(context.settings, binding)
        if target is None:
            raise StandardAPIError(409, "CONSUMER_TARGET_MISSING", "consumer trial target missing")
        from aiops_diagnostics.consumer.agent_contract import publication_settings
        from aiops_diagnostics.dify.dify_dsl_pull import workflow_content

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
    """发布门的模型白名单 = 平台注册表的模型 + 各 profile 已批准的端模型。

    受管客户端的模型**不在**平台 provider 注册表里：现役两端本来就用不同模型
    （`self-service.md`：客户端一个、管家一个，不能互相覆盖），注册表只写管家那个。
    只按注册表判白名单，会让"开通客户端后发布"必然失败 —— 2026-10-31 在 41 实测：
    客户端草稿发布报 `model is not allowlisted`，而 profile 的 `consumer_models`
    早已批准了它。profile 就是端模型的批准清单，与 `consumer_models()` 用同一份数据。
    """
    profiles_file = getattr(settings, "operator_profiles_file", "")
    if not profiles_file:
        return configured
    from aiops_diagnostics.dify.self_service_config import approved_model_names

    return tuple(dict.fromkeys([*configured, *approved_model_names(settings)]))


def _inject_runtime_hook(runtime: Any, method_name: str, dependency: Any) -> None:
    """显式向真实 GatewayRuntime 或声明了同名 setter 的测试替身注入依赖。

    通过检查类属性与实例 ``__dict__`` 识别注入点，既避免 ``hasattr`` 触发测试替身的
    ``__getattr__``，也不再用 ``contextlib.suppress(Exception)`` 静默吞掉真实装配异常。
    """
    if isinstance(runtime, GatewayRuntime):
        getattr(runtime, method_name)(dependency)
        return
    cls_attr = getattr(type(runtime), method_name, None)
    if callable(cls_attr):
        getattr(runtime, method_name)(dependency)
        return
    try:
        inst_dict = object.__getattribute__(runtime, "__dict__")
    except AttributeError:
        return
    hook = inst_dict.get(method_name)
    if callable(hook):
        hook(dependency)


def create_gateway_app(
    *,
    settings: GatewayServerSettings | None = None,
    store: GatewayStore | None = None,
    runtime: GatewayRuntime | None = None,
    caller_resolver: CallerContextResolver | None = None,
    order_authorizer: OrderAuthorizer | None = None,
    platform_resolver: PlatformIdentityResolver | None = None,
    faq_catalog: FAQCatalog | None = None,
    agent_manager: AgentManager | None = None,
    shortcut_manager: ShortcutManager | None = None,
) -> FastAPI:
    selected_settings = settings or GatewayServerSettings.from_env()
    selected_settings.validate()
    if selected_settings.knowledge_delegation_file:
        from aiops_diagnostics.knowledge.delegated_knowledge_client import load

        load(selected_settings.knowledge_delegation_file, selected_settings.execution_environment)
    if selected_settings.operator_profiles_file:
        from aiops_diagnostics.dify.self_service_config import profiles

        profiles(selected_settings)
    if selected_settings.chat_node_targets_file:
        from aiops_diagnostics.execution.node_publication import all_targets

        all_targets(selected_settings)
        if (
            not selected_settings.chat_route_bindings_file
            or Path(selected_settings.chat_route_bindings_file).resolve()
            != Path(selected_settings.database_file).resolve()
        ):
            raise ValueError("managed node routes must share the gateway version database")
    selected_store = store or GatewayStore(selected_settings.database_file)
    from aiops_diagnostics.dify.service_keys import KeyOperations
    from aiops_diagnostics.gateway import gateway_schema

    gateway_schema.startup(selected_store.path)
    KeyOperations(selected_store.path).clean_legacy()
    # Restart recovery, before this app can serve anything: every job the
    # previous process left `queued`/`running` is converged to an honest
    # terminal state here instead of hanging until its deadline (T3/#356,
    # T6/#492). Deliberately not in ``GatewayStore.__init__`` — short-lived CLI
    # commands (`aiops-gateway devices`) open the same database and must not
    # end live work; that mistake is what #492 fixed.
    selected_store.recover_interrupted_jobs()
    selected_runtime = runtime or GatewayRuntime.from_settings(selected_store, selected_settings)
    # 试运行运营商站点范围解析器：未配置或配置不可读时为 None（fail closed，落空集合）。
    trial_scope: Any = None
    try:
        trial_scope = _operator_site_scope(Settings.from_config(selected_settings.server_config_file))
    except (ValueError, OSError):
        trial_scope = None
    _inject_runtime_hook(selected_runtime, "set_trial_operator_scope", trial_scope)
    selected_resolver = caller_resolver or _caller_resolver(selected_settings)
    if selected_settings.environment_registry_file:
        from aiops_diagnostics.gateway.environment_registry import for_settings
        from aiops_diagnostics.identity.caller_auth import RegisteredCallerResolver

        registry = for_settings(selected_settings)
        selected_runtime.environment_registry = registry
        selected_resolver = RegisteredCallerResolver(selected_resolver, registry)
    diagnostic_settings = getattr(selected_runtime, "diagnostic_settings", None)
    selected_authorizer = order_authorizer or (
        ScopedOrderAuthorizer(
            diagnostic_settings,
            shop_directory=(
                _shop_directory(Settings.from_config(selected_settings.server_config_file))
                if diagnostic_settings is not None and selected_settings.server_config_file
                else None
            ),
        )
        if diagnostic_settings is not None
        else DisabledOrderAuthorizer()
    )
    # 聊天 worker 写终态前复用同一授权器；仅在真实运行时或显式声明该方法的替身上注入。
    _inject_runtime_hook(selected_runtime, "set_order_authorizer", selected_authorizer)
    if platform_resolver is not None:
        selected_platform_resolver = platform_resolver
    else:
        try:
            platform_settings = Settings.from_config(selected_settings.server_config_file)
        except (ValueError, OSError):
            # Keep existing non-FAQ endpoints bootable; FAQ requests fail closed
            # when the production identity configuration is unavailable.
            platform_settings = Settings()
        selected_platform_resolver = PlatformIdentityResolver(MySQLPlatformDirectory(platform_settings))
    selected_faq_catalog = faq_catalog or FAQCatalog.bundled()
    if agent_manager is None:
        configured_models = _publish_allowlist(
            allowed_models_from_settings(diagnostic_settings), selected_settings
        )
        knowledge_resolver = None
        if selected_settings.kb_service_base_url:
            # Real publish validation (T5/#171): bindings must resolve against
            # live kb-service. Without a kb URL the manager keeps the
            # fail-closed default (bindings never publish).
            from aiops_diagnostics.knowledge.tenant_scope import namespace_for
            from aiops_diagnostics.publication.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

            knowledge_resolver = KbBindingResolver(
                KbServiceKnowledgeClient(
                    selected_settings.kb_service_base_url,
                    tenant_id="aiops",  # rebound per publish by the manager caller's tenant
                    environment=namespace_for(selected_settings),
                    timeout=selected_settings.kb_service_timeout_seconds,
                )
            )
        selected_agent_manager = AgentManager(
            AgentStore(selected_store.path),
            knowledge_resolver=knowledge_resolver,  # type: ignore[arg-type]  # None keeps fail-closed default
            allowed_models=configured_models or ("aiops-api",),
        )
    else:
        selected_agent_manager = agent_manager
    context = GatewayAPI(
        selected_settings,
        selected_store,
        selected_runtime,
        selected_resolver,
        selected_authorizer,
        selected_platform_resolver,
        selected_faq_catalog,
        selected_agent_manager,
        shortcut_manager,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        close_gateway_runtime(context.runtime)

    app = FastAPI(
        title="AI-Ops Gateway",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = context
    from aiops_diagnostics.consumer.consumer_tool_grants import ConsumerToolGrants
    from aiops_diagnostics.operator.operator_cpms import OperatorCpms

    context.operator_cpms = OperatorCpms(context)
    context.consumer_tool_grants = ConsumerToolGrants(context, selected_settings)

    api_module = sys.modules[__name__]
    install_exception_handlers(app, api_module)
    app.add_middleware(request_observation.AssistantQuestionObservation)
    if selected_settings.company_gateway_trust_file:
        from aiops_diagnostics.identity.gateway_admission import Admission, GatewayAdmission

        if not selected_settings.environment_registry_file:
            raise ValueError("company gateway admission requires explicit environment registry")
        admission = Admission(
            selected_settings.company_gateway_trust_file,
            selected_settings.environment,
            selected_settings.data_home / "gateway-admission.db",
        )
        app.add_middleware(GatewayAdmission, admission=admission)

    deps = build_dependencies(context, api_module)

    # 1) Dify 自助管理、环境目录与管家运行日志路由
    from aiops_diagnostics.dify.self_service_api import register as register_self_service

    context.self_service_operator_identity = lambda session: authenticated_operator_console(context, session)
    register_self_service(app, context)

    # 2) 按业务域拆分的 Gateway APIRouter 全表
    for router_module in (
        dify_console_router,
        assistant_router,
        conversations_router,
        files_router,
        orders_sites_router,
        internal_callbacks_router,
        health_analysis_router,
        agents_router,
        shortcuts_router,
        knowledge_router,
    ):
        router_module.register(app, context, deps, api_module)

    # 3) 受管环境试运行路由与公开 OpenAPI 文档
    from aiops_diagnostics.gateway.environment_trial import register as register_environment_trial
    from aiops_diagnostics.gateway.public_openapi import install_public_docs

    register_environment_trial(app, context)
    install_public_docs(app)
    return app


def _inside_credential(runtime: Settings) -> str:
    """会话身份的 UPMS 服务侧内部凭据（C→B 映射与运营商站点范围共用同一套前提）。

    两个接缝的配置前提逐字相同（UPMS 地址 + 该凭据），所以只写一遍：往其中一个
    加条件时不会漏掉另一个。
    """
    return (runtime.upms.inside_token or "").strip()


def _b_subject_directory(runtime: Settings) -> BSubjectDirectory | None:
    """会话身份的 C→B 映射目录（#424）。

    复用 UPMS 既有端点；未配置 UPMS 地址或服务侧内部凭据时返回 ``None``——会话身份
    只保留 C 侧部分，需要 B 端主体的路径按 fail closed 拒绝，消费者端不受影响。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsBSubjectDirectory(runtime.upms, credential)


def _shop_directory(runtime: Settings) -> ShopDirectory | None:
    """令牌不带 ``shop_ids`` 时的店铺归属来源（公司权威端点）。

    与 C→B 映射、运营商站点范围共用**同一套配置前提**（UPMS 地址 + 服务侧内部凭据）：
    未配置时返回 ``None``，此时令牌缺 ``shop_ids`` 一律拒绝（拿不到归属就是拿不到范围）。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsShopDirectory(runtime.upms, credential)


def _operator_site_scope(runtime: Settings) -> OperatorSiteScope | None:
    """会话身份的运营商站点范围（#426，四跳链的后两跳）。

    与 C→B 映射共用同一套配置前提（UPMS 地址 + 服务侧内部凭据）：未配置时返回
    ``None``，会话数据范围保持 ``self``——管家端因此只能看到本人的单（fail closed，
    不放宽），消费者端不变。站点归属映射复用受限直连的同一条跳板隧道，不为授权
    另开一条连接路径。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsOperatorSiteScope(runtime, credential)


def _caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    # #444 / ADR-0009 D9：来源密钥这道门**装在会话那一级之前**。这是本票最关键的一处，
    # 也是 #443 记录的两条边界里那条「必须放在会话之前」的落点：会话那一级是
    # ``if settings.third_session_service_token:`` 且**直接 return**，而 41 现网那个键有值 ——
    # 门若排在它之后，带公司令牌、不带 ``third-session`` 的请求会在第一级就被 401，
    # 新路径在现网**永远不可达**，即便把密钥配上也一样。
    #
    # 未配置来源密钥 ⇒ 这一整段不参与（新链路整体不启用，fail closed；不是放行），
    # 下面的选择链与本票之前逐字一致；清空即回滚。
    if settings.company_source_key:
        company = _company_token_resolver(settings)
        if company is not None:
            return SourceKeyCallerResolver(
                settings.company_source_key,
                company,
                _fallback_caller_resolver(settings),
            )
    return _fallback_caller_resolver(settings)


def _company_token_resolver(settings: GatewayServerSettings) -> CompanyTokenCallerResolver | None:
    """新链路的解析器（#443 的实现），只在来源密钥已配置时构造。

    构造失败（配置读不出来）返回 ``None`` ⇒ 调用方退回既有链：带密钥的请求随后按既有链被
    拒绝（401），而不是被放行 —— 这是 fail closed 的方向。半配置（只设 URL 或只设密钥）
    已在 ``GatewayServerSettings.validate`` 里变成启动失败，不走这里。
    """
    # 两条通道二选一：配了签名密钥就走本地验签（A2 / #448），否则走远端 check_token。
    if not settings.company_check_token_url and not settings.company_jwt_key:
        return None
    try:
        runtime = Settings.from_config(settings.server_config_file)
        return CompanyTokenCallerResolver(
            CompanyTokenSettings(
                url=settings.company_check_token_url,
                client_id=settings.company_token_client_id,
                client_secret=settings.company_token_client_secret,
                signature_key=settings.company_jwt_key,
                trust_company_payload=settings.trust_company_payload,
            ),
            runtime,
            shop_directory=_shop_directory(runtime),
            permission_source=_permission_source(runtime),
        )
    except (ValueError, OSError):
        return None


def _permission_source(runtime: Settings) -> PermissionSource | None:
    """公司权限码 → 内部角色（#926）。

    **未配置 UPMS 时不构造**（返回 ``None``），解析器随即保持空角色集 —— 与改动前逐字一致。
    这样「哪些部署会因此多拿到权限」是一个看配置就能回答的问题。
    """
    if not (runtime.upms.base_url or "").strip():
        return None
    from aiops_diagnostics.identity.company_permissions import CompanyPermissionSource

    return CompanyPermissionSource(runtime.upms)


def _fallback_caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    """既有链：会话 → UPMS 兜底 → introspection。选择结果与新增本票之前逐字一致。

    唯一的例外在会话那一级（ADR-0320）：登记表（``environment_registry_file``）部署
    且本环境 UPMS 可用时，operator 入口的个人 Bearer 由 ``EntryCallerResolver`` 改走
    本环境 UPMS 验证，其余入口逐字落到会话链。登记表未配置时返回值与分派引入之前
    逐字一致——旧 41 的合同不变；本环境 UPMS 不可配时保持纯会话链（fail closed，
    不把 operator 悄悄放行到一条验证不了它的链上）。
    """
    if settings.third_session_service_token:
        try:
            runtime = Settings.from_config(settings.server_config_file)
            resolver: CallerContextResolver = RedisThirdSessionResolver(
                ThirdSessionSettings(
                    host=runtime.redis.host,
                    port=runtime.redis.port,
                    database=runtime.redis.database,
                    username=runtime.redis.user,
                    password=runtime.redis.password,
                    service_token=settings.third_session_service_token,
                    key_prefix=settings.third_session_key_prefix,
                ),
                b_subject_directory=_b_subject_directory(runtime),
                operator_scope=_operator_site_scope(runtime),
            )
        except (ValueError, OSError):
            return DisabledCallerResolver()
        if settings.environment_registry_file and (runtime.upms.base_url or "").strip():
            # 与 ``OperatorCpms.refresh_scope`` 同一个实时站点目录：首次鉴权就把
            # UPMS 的混合断言规范化到站点集合，初/复核/回读比较同一口径的范围指纹；
            # 服务侧内部凭据未配时注入 None——分派原样透传，复核按既有 fail closed。
            directory = _operator_site_scope(runtime)
            resolver = EntryCallerResolver(
                UpmsCallerResolver(runtime),
                resolver,
                site_scope=None if directory is None else directory.site_scope_by_b_user_id,
            )
        return resolver
    # #444 起，公司令牌路径**不再出现在这条链里**：它由上面的来源密钥门分派，只有密钥验过的
    # 请求才走得到。把这四种选择结果写清楚，是为了让「带错密钥」的行为可预期 —— 它会落到这里，
    # 按会话/introspection/UPMS 处理公司令牌，结果是被拒绝（401），而不是被放行。
    if not settings.introspection_url:
        try:
            return UpmsCallerResolver(Settings.from_config(settings.server_config_file))
        except (ValueError, OSError):
            return DisabledCallerResolver()
    return IntrospectionCallerResolver(
        IntrospectionSettings(
            url=settings.introspection_url,
            client_id=settings.introspection_client_id,
            client_secret=settings.introspection_client_secret,
            audience=settings.standard_api_audience,
            timeout_seconds=settings.introspection_timeout_seconds,
        )
    )


#: The platform decision's failure modes and the answer each one gets. One
#: definition: the three copies this replaces had already drifted apart once,
#: and an error taxonomy with two answers is an error taxonomy with none.
_PLATFORM_ERROR_STATUS = {
    PLATFORM_AMBIGUOUS: status.HTTP_409_CONFLICT,
    PLATFORM_FORBIDDEN: status.HTTP_403_FORBIDDEN,
    PLATFORM_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _platform_error(exc: FAQError) -> StandardAPIError:
    """``FAQError`` (a platform-decision failure) as the response it deserves."""
    return StandardAPIError(
        _PLATFORM_ERROR_STATUS.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE),
        exc.code,
        str(exc),
        retryable=exc.code == PLATFORM_UNAVAILABLE,
    )


def _authenticate_caller(
    resolver: CallerContextResolver,
    authorization: str | None,
    third_session: str | None,
    *,
    required_scope: str,
    platform_entry: str | None = None,
    source_key: str | None = None,
) -> ScopeContext:
    """The one place a ``CallerAuthError`` becomes an outbound error, and the
    one place the resolver is asked for an identity.

    Every ``authenticated_*`` dependency delegates here. There used to be three
    copies of this mapping and one of them had no ``CALLER_AUTH_CONFIG_MISSING``
    branch, so a missing gateway configuration told twelve endpoints' callers
    "your login has expired, sign in again" — the exact opposite of what
    ``docs/gateway.md`` promises (a stable 503, fail closed). A rule with two
    answers is a rule with none, and the copy that drifted was the one nobody
    looked at.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise StandardAPIError(status.HTTP_401_UNAUTHORIZED, "ACCESS_TOKEN_REQUIRED", "access token required")
    token = authorization.removeprefix("Bearer ").strip()
    if token.startswith("aops_"):
        raise StandardAPIError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_ACCESS_TOKEN",
            "access token validation failed",
        )
    try:
        # ``platform_entry`` 与 ``source_key`` 都在这里**无条件**传下去，不走「带没带会话」的
        # 分支：解析器各自忽略对自己无意义的参数，但分支之间一旦不对称，「同一个请求多带一个头
        # 就换了范围语义/身份来源」就会变成排查不了的形状（同 ``authenticated_diagnosis_caller``
        # 那条注释）。``source_key`` 尤其如此：它是分派依据，缺它门就永远落到既有链。
        return resolver.resolve(
            token,
            required_scope=required_scope,
            third_session=third_session,
            platform_entry=platform_entry,
            source_key=source_key,
        )
    except CallerAuthError as exc:
        if exc.code == CALLER_AUTH_FORBIDDEN:
            status_code, code, retryable = status.HTTP_403_FORBIDDEN, "INSUFFICIENT_SCOPE", False
        elif exc.code == CALLER_AUTH_CONFIG_MISSING:
            # Explicitly retryable, rather than relying on whoever raised it to
            # have passed `retryable=True`: `CallerAuthError`'s default is
            # False, and a configuration gap is by definition something a fix
            # restores. This is the rule deciding, not the raiser remembering.
            status_code, code = status.HTTP_503_SERVICE_UNAVAILABLE, "ACCESS_TOKEN_VALIDATION_UNAVAILABLE"
            retryable = True
        else:
            status_code = (
                status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_401_UNAUTHORIZED
            )
            code = "ACCESS_TOKEN_VALIDATION_UNAVAILABLE" if exc.retryable else "INVALID_ACCESS_TOKEN"
            retryable = exc.retryable
        raise StandardAPIError(
            status_code, code, "access token validation failed", retryable=retryable
        ) from exc


def _health_job_response(job: dict[str, Any]) -> dict[str, Any]:
    status_value = str(job["status"])
    return {
        "job_id": job["job_id"],
        "order_no": job["order_no"],
        "rule_version": job["rule_version"],
        "status": status_value,
        "retry_after_ms": 1000 if status_value in ACTIVE_HEALTH_JOB_STATUSES else None,
        # The report's prose language: the language the JOB was created in.
        # It describes the stored `report`, not this request — a poller cannot
        # relabel a report it did not create (same rule as the diagnosis
        # surface; see `_standard_diagnosis_response`).
        "language": job.get("language") or "zh",
        "report": job.get("report"),
        "error": (
            {
                "code": job.get("error_code") or "REPORT_FAILED",
                "message": job.get("error_message") or "health report failed",
                # A restart is retryable for the caller: the report never ran,
                # and asking again is exactly what should happen. Without it
                # here, the frontend shows an error with no action — which is
                # the one thing the restart verdict is supposed to avoid.
                "retryable": job.get("error_code")
                in {"SOURCE_UNAVAILABLE", "REPORT_TIMEOUT", "REPORT_INTERRUPTED_BY_RESTART"},
            }
            if status_value == "failed"
            else None
        ),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "completed_at": job.get("completed_at"),
    }


def _assistant_question_response(qa: dict[str, Any], language: str) -> dict[str, Any]:
    """The one public shape of an assistant-question job.

    Poll and cancel return the same body on purpose: the job's state is the
    single thing both surfaces report, so a client reads a stopped job exactly
    as it reads a finished one.

    The failure message is customer-facing copy, not the job record's internal
    reason. The record keeps that reason deliberately — a contract violation is
    a defect an engineer must be able to see, and replacing it there once turned
    a real bug into "service temporarily unavailable" (2026-09-17). But the
    record is read by an engineer and this response is read by a customer; the
    two need different sentences, and only the boundary can tell them apart. A
    Chinese-speaking user was shown "customer QA turn returned invalid JSON",
    which names neither the problem nor an action.
    """
    from aiops_diagnostics.publication.agent_content import (
        OPERATOR_MAIN_AGENT_ID,
        operator_agent_brief,
    )

    status_value = str(qa["status"])
    effective_lang = qa.get("language") or effective_language(language)
    return {
        "type": "qa",
        # The EFFECTIVE language: a non-promptable request is answered in the
        # default, so reporting the raw request tag would describe text the
        # answer does not contain (#541 review).
        "language": effective_lang,
        "qa_id": qa["qa_id"],
        "question": qa["question"],
        "status": status_value,
        "attempt_no": int(qa.get("attempt_no") or 1),
        "parent_qa_id": qa.get("parent_qa_id"),
        "retry_after_ms": 1000 if status_value in ACTIVE_DIAGNOSIS_STATUSES else None,
        "stream_url": f"/v1/assistant/questions/{qa['qa_id']}/events",
        "agent": operator_agent_brief(effective_lang, str(qa.get("agent_id") or OPERATOR_MAIN_AGENT_ID)),
        "result": qa.get("result"),
        "error": (
            {
                "code": qa.get("error_code") or "QA_FAILED",
                "message": _qa_user_message(language, qa.get("error_code")),
                "retryable": qa.get("error_code") != "OPERATOR_MODEL_QUOTA_EXHAUSTED",
            }
            if status_value in {QUESTION.failed, QUESTION.expired}
            else None
        ),
    }


#: The only failure code that means retrieval actually failed. Everything else
#: on this surface — a provider error, a contract violation, an unreachable
#: model — records `QA_FAILED` and has nothing to do with the knowledge base.
_KB_FAILURE_CODE = "KB_UNAVAILABLE"


#: `OPERATOR_HISTORY_LIMIT` 的用户文案。与 `QA_FALLBACK_MESSAGES` 同一张
#: 「按语言查词表、缺行读声明 fallback」的取法（`localized_pack`）。
_HISTORY_LIMIT_MESSAGES = {
    "zh": "这段对话的证据引用已达到上限，请新建对话继续。",
    "zh-Hant": "這段對話的證據引用已達到上限，請新建對話繼續。",
    "en": "This conversation reached its evidence limit. Start a new conversation to continue.",
}


def _qa_user_message(language: str, error_code: str | None) -> str:
    """The localized, actionable sentence a customer sees when a QA job fails.

    The cause decides the sentence. Claiming the knowledge base is unavailable
    for every failure told a customer whose model provider had simply rejected
    the request that the *library* was down — a false cause and useless
    guidance, since retrying is the wrong advice only for the reader who
    believes the wrong thing is broken. Only a verified retrieval failure gets
    the retrieval copy; the rest get the honest, cause-neutral one.
    """
    from aiops_diagnostics.operator.operator_cpms import NODE_FAILURE_CODES

    if error_code == "OPERATOR_HISTORY_LIMIT":
        return localized_pack(_HISTORY_LIMIT_MESSAGES, language)
    if error_code in NODE_FAILURE_CODES.values():
        return diagnosis_error_message(language, error_code)
    pack = localized_pack(QA_FALLBACK_MESSAGES, language)
    return pack["unavailable"] if error_code == _KB_FAILURE_CODE else pack["generation_failed"]


def _require_conversation(
    context: Any,
    caller: ScopeContext,
    conversation_id: str,
) -> dict[str, Any]:
    """Load a scope-owned conversation or raise the uniform 404."""
    try:
        conversation = context.conversation_store.get(conversation_id, caller.scope_fingerprint)
        if conversation.get("operator_binding", "").endswith(":preview"):
            raise ConversationError("preview conversation is not a formal conversation")
        return conversation
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc


def _conversation_response(conversation: dict[str, Any]) -> dict[str, Any]:
    """Public conversation shape (no internal fingerprints)."""
    return {
        "conversation_id": conversation["conversation_id"],
        "business_entry": conversation["business_entry"],
        "agent_id": conversation.get("agent_id") or "operator_main",
        "agent_version_key": conversation["agent_version_key"],
        "title": str(conversation.get("title") or ""),
        "active_order_no": conversation["active_order_no"],
        "is_generating": conversation["is_generating"],
        "turn_count": int(conversation.get("turn_count") or 0),
        "created_at": conversation["created_at"],
        "updated_at": conversation["updated_at"],
        "expires_at": conversation["expires_at"],
    }


def _operator_conversation(
    context,
    caller,
    binding,
    conversation,
    *,
    trial,
    agent_id: str = "operator_main",
):
    key = f"{binding.key}:{binding.app_id}:{'preview' if trial else 'formal'}"
    resolved_version_agent = binding.agent_id or "agt_" + hashlib.sha256(key.encode()).hexdigest()[:32]
    version = f"{resolved_version_agent}#v{binding.agent_version_no}"
    if conversation is None:
        return context.conversation_store.create(
            scope_fingerprint=caller.scope_fingerprint,
            business_entry="operator",
            agent_version_key=version,
            operator_binding=key,
            agent_id=agent_id,
        )
    if conversation.get("operator_binding") != key:
        raise StandardAPIError(404, "CONVERSATION_NOT_FOUND", "conversation not found")
    return conversation


def _resolve_conversation(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    conversation_id: str,
) -> dict[str, Any]:
    """Load the conversation for this caller, enforcing scope AND entry.

    Entry is part of the binding (T4/#172): the same user switching from the
    consumer entry to the operator entry gets a fresh conversation set, and a
    mismatched or missing conversation is a uniform 404 — indistinguishable
    from never-existing.
    """
    try:
        conversation = context.conversation_store.get(conversation_id, caller.scope_fingerprint)
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc
    if conversation.get("business_entry") != str(getattr(decision, "platform", "")):
        # The platform decision IS the resolved business entry for this call.
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        )
    return conversation


#: The three answers an order authorization check can give. Explicit rather
#: than a `bool`, because two of them are *not* "no": a check that could not be
#: made is a different fact from a check that said no, and five surfaces must
#: refuse while two must fall back silently. A `bool` erases the difference at
#: the one place that can still tell them apart.
OWNED = "owned"
NOT_OWNED = "not_owned"
UNAVAILABLE = "unavailable"


#: The metric dimension an authorization precheck is recorded under. Its own
#: bucket, excluded from the interaction totals: a precheck is component health,
#: not a run. Recording it as `diagnosis` counted a failed lookup on the access
#: probe, the report start and the question fallback as diagnosis runs that
#: never happened.
ORDER_AUTH_ROUTE = "order_auth"
#: The code the warning and the metric row both carry, so an operator can grep
#: for one string and find both.
ORDER_AUTHENTICATION_UNAVAILABLE = "ORDER_AUTHORIZATION_UNAVAILABLE"


#: 只有本切片的三个端点拒绝中间层缓存：它们是**按人**的授权结果，不是公共资源。
NO_STORE_PATH_PREFIXES = (
    "/v1/conversations",
    "/v1/assistant/questions",
    "/v1/dify/ops/",
    "/v1/orders/",
    "/v1/internal/charging-status/",
    "/v1/internal/operator-runs/",
    "/v1/operator-tools/",
)


def _no_store_headers(request: Request) -> dict[str, str]:
    """错误响应也带 ``Cache-Control: no-store``，但**只**对充电状态那一组路径。

    成功响应在各自的路由里显式设置；错误响应走统一处理器，所以判定放在这里 ——
    否则一次 403 的响应体（含错误码）会被中间层按 URL 缓存，而 URL 里带着订单号。
    只按路径前缀放行，避免把整份 API 的缓存语义一起改掉。
    """
    if request.url.path.startswith(NO_STORE_PATH_PREFIXES):
        return {"Cache-Control": "no-store"}
    return {}


def _require_matching_exec_config(
    context: Any, run: AgentRun, observed: str | None, *, actual_node_id: str | None = None
) -> AgentRun:
    """领取时对照执行配置身份。**签发时固定、领取时对照、并把实际值落库。**

    三件事刻意分开：
    - 记录里**没有**期望（清单未启用）⇒ 什么都不做，行为与今天逐字一致；
    - 有期望而节点回传缺失或不等 ⇒ 记录落终态（``failed`` + ``reason=exec_config_drift``），
      **不返回快照**；
    - 相等 ⇒ 把实际值写进记录，再放行。

    关键点：**比较用什么、写什么、放行依据什么、以及此刻是否过期，是同一个写事务里的
    同一份记录**。早先这里是「读一次记录 → 无条件 UPDATE → 返回早先读到的成功对象」，
    于是另一个请求的拒绝提交进来之后，本请求仍会返回快照，并且把 ``matched`` 写在一条
    终态记录上；而且只查 ``state`` 不查 ``expires_at``，过期后三条放行路径都还能交出正文。
    现在交给 :meth:`AgentRunStore.confirm_exec_config`，它返回的是写入（含过期收敛）之后
    读到的记录；终态记录原样返回，由调用方按既有语义拒绝。

    调用点在 ``claim_charging_status`` 里共有两处（首次领取、以及 ``release_candidate``
    的最后一次放行），重试路径与它共用同一段 —— R1-1 的教训是放行路径各有一次机会，
    只覆盖其中一条会让另一条漏掉。
    """
    expected = run.expected_agent_version
    if expected is None:
        return run
    confirmed = context.agent_run_store.confirm_exec_config(
        run.run_id, observed, actual_node_id=actual_node_id
    )
    if confirmed is None:
        # 身份不一致（记录已被钉成 failed + exec_config_drift）或记录不存在。
        raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_EXEC_CONFIG_DRIFT", "agent run refused")
    return confirmed


def _consumer_order_chat_via_node(
    context, caller, decision, payload, conversation, language, order_no, binding
):
    """聊天入口的节点路线：**只做授权与建 QA，然后把等待搬到后台**。

    这里刻意不做三件事：

    * 不读业务（SQL/CPMS）—— 那是节点的活，网关替它读一次就等于「直读+节点」两套答案；
    * 不等节点 —— App 的 POST 必须快速返回，否则前端超时；
    * 失败不回落 —— 任何失败都落 QA 终态，绝不改走直读或旧模型分支。

    授权仍然是**网关**做的：最新单要按原本人 SQL 范围选出来，指定单要核可见性。
    选出来之后才派发，所以「选哪一单」不会变成节点或客户端的决定。
    `binding` 是**这一条请求**选中的绑定（按租户登记），随请求冻结进 run。
    """
    if order_no is None:
        request_observation.mark_stage(None, request_observation.STAGE_ORDER_CHAT_LOOKUP)
        try:
            scope = resolve_query_scope(caller)
            with scoped_mysql_source(
                Settings.from_config(context.settings.server_config_file), scope
            ) as source:
                rows = source.list_orders(page=1, page_size=1)
        except (SourceError, ScopeError, ValueError) as exc:
            raise StandardAPIError(
                503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order lookup unavailable", retryable=True
            ) from exc
        if not rows:
            raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
        order_no = str(rows[0]["order_no"])
    _require_consumer_self_order(caller, str(decision.platform), order_no)
    _require_visible_order(context, caller, order_no)

    turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    try:
        qa = context.runtime.start_chat_order_qa(
            caller,
            payload.question,
            order_no=order_no,
            language=language,
            conversation=conversation,
            conversation_turn_no=turn_no,
            binding=binding,
        )
    except Exception:
        _release_conversation_turn(context, conversation, turn_no)
        raise
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Cache-Control": "no-store"},
        content={
            **_assistant_question_response(qa, language),
            **(
                {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                if conversation
                else {}
            ),
        },
    )


def _consumer_intent_clarification(code, language):
    from aiops_diagnostics.consumer.consumer_intent import message

    return JSONResponse(
        status_code=200,
        headers={"Cache-Control": "no-store"},
        content={
            "type": "clarification",
            "code": "CONSUMER_" + code.upper(),
            "missing_fields": ["order_no"] if code == "order" else [],
            "message": message(code, language),
            "language": language,
        },
    )


def _consumer_order_chat(context, caller, decision, payload, conversation, language, request):
    order_no = payload.order_no or extract_order_identifier(payload.question)
    authenticated_chat_order_reader(context, caller, request)
    if order_no is None and not asks_latest_order(payload.question):
        return _clarification(
            context,
            caller,
            decision,
            code=CLARIFY_ORDER_REQUIRED,
            question=payload.question,
            language=language,
        )
    # APP-RUNTIME-01：命中服务端绑定 ⇒ 交给客户节点跑，POST **立即**返回。
    # 绑定按（环境，租户，consumer 入口，本场景）**显式登记**：没有这一条就走下面的原分支。
    # 「全局配了一个文件」不算命中 —— 那等于任何能通过本人授权的租户都派到同一个节点，
    # 而本任务要求的执行位置隔离恰好就是这件事（Codex 复核必修 1）。
    try:
        binding = chat_execution.select_binding(context.settings, caller)
    except chat_execution.ChatRunUnavailable as exc:
        # 绑定表在运行期读不出来：拒绝，而不是"当作没配"悄悄回落到直读。
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is not None:
        return _consumer_order_chat_via_node(
            context, caller, decision, payload, conversation, language, order_no, binding
        )

    turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    qa = None
    try:
        if order_no is None:
            request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_LOOKUP)
            try:
                scope = resolve_query_scope(caller)
                with scoped_mysql_source(
                    Settings.from_config(context.settings.server_config_file), scope
                ) as source:
                    rows = source.list_orders(page=1, page_size=1)
            except (SourceError, ScopeError, ValueError) as exc:
                raise StandardAPIError(
                    503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order lookup unavailable", retryable=True
                ) from exc
            if not rows:
                raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
            order_no = str(rows[0]["order_no"])
        _require_consumer_self_order(caller, str(decision.platform), order_no)
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_AUTH)
        _require_visible_order(context, caller, order_no)
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_CPMS)
        try:
            snapshot = _authorized_charging_snapshot(context.settings, caller, order_no)
        except ChargingStatusError as exc:
            raise StandardAPIError(
                502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable", retryable=True
            ) from exc
        _require_visible_order(context, caller, order_no)
        answer = status_answer(snapshot, language)
        qa = context.store.create_assistant_question(
            caller.scope_fingerprint,
            payload.question,
            tenant_id=caller.effective_tenant_id,
            internal_run_id="consumer-order-chat:" + order_no,
        )
        evidence = {
            "call_id": qa["qa_id"],
            "scope_fingerprint": caller.scope_fingerprint,
            "source": snapshot["source"],
            "answer_sha256": hashlib.sha256(json.dumps(answer, sort_keys=True).encode()).hexdigest(),
            "snapshot": snapshot,
            "result_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        }
        # The one id that joins this log line to the job and to the private
        # evidence file. Recorded as soon as the row exists, so even a later
        # evidence failure still names the QA it failed to complete.
        request_observation.mark_qa(request, qa["qa_id"])
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_EVIDENCE)
        try:
            write_private_text(
                Path(context.settings.data_home) / "consumer-order-chat" / (qa["qa_id"] + ".json"),
                json.dumps(evidence, ensure_ascii=False),
            )
            _require_visible_order(context, caller, order_no)
            if conversation is not None:
                context.conversation_store.complete_turn(
                    conversation["conversation_id"],
                    caller.scope_fingerprint,
                    turn_no,
                    answer=answer,
                    status_qa_id=qa["qa_id"],
                )
            if not context.store.update_assistant_question(qa["qa_id"], status="completed", result=answer):
                raise OSError("chat result write refused")
        except (OSError, PrivatePathError, sqlite3.Error, ConversationError) as exc:
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable", retryable=True
            ) from exc
        _record_route_metric(
            context,
            caller,
            route_type="qa",
            outcome="completed",
            conversation_id=conversation.get("conversation_id") if conversation else None,
        )
        qa.update(status="completed", result=answer)
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store"},
            content={
                **_assistant_question_response(qa, language),
                **(
                    {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                    if conversation
                    else {}
                ),
            },
        )
    except Exception as exc:
        if qa is not None:
            with contextlib.suppress(Exception):
                context.store.update_assistant_question(qa["qa_id"], status="failed", error_code="QA_FAILED")
        with contextlib.suppress(Exception):
            _release_conversation_turn(context, conversation, turn_no)
        if isinstance(exc, (OSError, PrivatePathError, sqlite3.Error, ConversationError)):
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable", retryable=True
            ) from exc
        raise


def authenticated_operator_console(context, session):
    from aiops_diagnostics.operator.operator_cpms import OperatorRefused

    identity = context.operator_cpms.console_identity(session)
    caller = _authenticate_caller(
        context.caller_resolver,
        "Bearer " + identity["token"],
        None,
        required_scope=STANDARD_DIAGNOSIS_SCOPE,
        platform_entry="operator",
        source_key=context.settings.company_source_key,
    )
    if caller.subject.b_user_id != identity["user_id"] or not _tenant_in_scope(
        identity["tenant_id"], caller.effective_tenant_id
    ):
        raise OperatorRefused("OPERATOR_TRIAL_IDENTITY_MISMATCH")
    return context.operator_cpms.refresh_scope(caller, allow_empty=True), identity["token"]


def _require_operator_company_token(request):
    if request.headers.get("X-Third-Session") or request.headers.get("third-session"):
        raise StandardAPIError(403, "OPERATOR_COMPANY_TOKEN_REQUIRED", "use the company operator login")
    if request.headers.get("X-Business-Entry", "").strip().lower() != "operator":
        raise StandardAPIError(403, "OPERATOR_COMPANY_TOKEN_REQUIRED", "use the company operator login")


async def _operator_json(request):
    from aiops_diagnostics.operator.operator_cpms import OperatorRefused

    raw = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 262144:
                    raise OperatorRefused("OPERATOR_BODY_TOO_LARGE")
    except TimeoutError as exc:
        raise OperatorRefused("OPERATOR_BODY_TIMEOUT") from exc
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OperatorRefused("OPERATOR_INVALID_BODY") from exc
    if not isinstance(body, dict):
        raise OperatorRefused("OPERATOR_INVALID_BODY")
    return body


def authenticated_chat_order_reader(context, caller, request):
    _require_consumer_self_order(caller, request.headers.get("X-Business-Entry"), "pending")
    reader = _authenticate_caller(
        context.caller_resolver,
        request.headers.get("Authorization"),
        request.headers.get("X-Third-Session"),
        platform_entry="consumer",
        source_key=request.headers.get(SOURCE_KEY_HEADER),
        required_scope=STANDARD_ORDER_READ_SCOPE,
    )
    if reader.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "order read identity changed")


def _require_current_chat_order(context, caller, qa, request):
    from aiops_diagnostics.operator import operator_cpms

    if str(qa.get("internal_run_id") or "").startswith(operator_cpms.PREFIX):
        if qa["status"] == "completed":
            _require_operator_company_token(request)
            expected = context.operator_cpms.authorize_readback(
                caller, qa["qa_id"], request.headers.get("Authorization", "").removeprefix("Bearer ")
            )
            if expected != qa.get("result"):
                raise operator_cpms.OperatorRefused("OPERATOR_RESULT_MISMATCH")
        return
    from aiops_diagnostics.consumer import consumer_chat_service

    if str(qa.get("internal_run_id") or "").startswith(consumer_chat_service.PREFIX):
        run = context.dify_run_store.by_qa(qa["qa_id"])
        if run and run.scope_fingerprint == caller.scope_fingerprint:
            qa["language"] = run.language
            result = qa.get("result") or {}
            evidence = context.dify_run_store.context(run.run_id) or {}
            # 旧审核FAQ原文只存中英文；新文档与本地化快照才使用所选语言。
            if qa["status"] == "completed" and (
                (
                    evidence.get("candidates")
                    and evidence.get("mode") != "documents"
                    and result.get("retrieval_status") == "found"
                    and not evidence.get("live_snapshot")
                )
                or (evidence.get("live_snapshot") and evidence.get("answer_language_version") != 2)
            ):
                qa["language"] = rendering_language(COMPOSED_LANGUAGES, run.language)
                if run.language == "zh-Hant" and run.data_kind == "status" and evidence.get("live_snapshot"):
                    qa["language"] = "zh-Hant"
        if run and (run.order_no or run.data_kind == "agent"):
            authenticated_chat_order_reader(context, caller, request)
        try:
            return consumer_chat_service.require_result(context.runtime, caller, qa)
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if _is_node_chat_qa(qa):
        return _require_current_chat_node_order(context, caller, qa, request)
    internal = str(qa.get("internal_run_id") or "")
    if internal.startswith(chat_execution.DIFY_RUN_PREFIX):
        return _require_current_chat_dify_order(context, caller, qa, request)
    if internal.startswith("consumer-order-chat:"):
        authenticated_chat_order_reader(context, caller, request)
        order_no = internal.removeprefix("consumer-order-chat:")
        _require_consumer_self_order(caller, "consumer", order_no)
        _require_visible_order(context, caller, order_no)
        if not context.settings.consumer_order_chat_enabled:
            raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
        try:
            path = Path(context.settings.data_home) / "consumer-order-chat" / (qa["qa_id"] + ".json")
            validate_private_file(path)
            evidence = json.loads(path.read_text())
            snapshot = evidence["snapshot"]
            if (
                qa["status"] != "completed"
                or evidence["scope_fingerprint"] != caller.scope_fingerprint
                or evidence["call_id"] != qa["qa_id"]
                or snapshot["order_no"] != order_no
                or snapshot["source"] != "cpms.chargingInfo"
                or evidence["result_sha256"]
                != hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
                or evidence["answer_sha256"]
                != hashlib.sha256(json.dumps(qa["result"], sort_keys=True).encode()).hexdigest()
            ):
                raise ValueError("evidence differs")
        except (OSError, PrivatePathError, ValueError, KeyError, TypeError) as exc:
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable"
            ) from exc


def _require_current_chat_node_order(context, caller, qa, request):
    """节点路线的读取守卫：**按存储关联**找回那条 run，再复用它自己的放行判据。

    两条路线的共同点不能省：入口仍是 consumer、订单仍需此刻可见、绑定仍需有效、
    正文仍需是该 run 的 succeeded 快照。

    判据本身只有一份：`chat_execution.final_release`。这里**不再**自己写一遍
    —— 复核实测表明，手写的第二遍漏了四项（环境、actual==expected、节点登记环境、
    执行配置核对），而且漏得很安静：GET 仍返回 200 + 正文。

    停用之后**只**拒绝正文：`queued/running/failed/cancelled/expired` 仍按原协议可读。
    否则用户看得到「取消返回 404」却看不到「其实已经取消成功」，前端无从显示。
    """
    internal = str(qa.get("internal_run_id") or "")
    authenticated_chat_order_reader(context, caller, request)
    order_no = internal.removeprefix(chat_execution.CHAT_RUN_PREFIX)
    if not order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    _require_consumer_self_order(caller, "consumer", order_no)
    # 无正文的状态：与绑定是否启用无关，本人仍要能读到它自己的作业状态。
    if qa["status"] in {"queued", "running", "failed", "cancelled", "expired"}:
        return
    if qa["status"] != "completed":
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    store = context.agent_run_store
    located = store.by_qa(qa["qa_id"]) if store is not None else None
    if located is None:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if located.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    try:
        # 绑定必须**此刻**仍成立，而且是**签发时那一条**：配置被换掉、节点被停用、
        # 登记被拿掉，都不再交付正文。换一份"现在看起来像"的配置来背书同一段是错的。
        binding = chat_execution.binding_for_key(context.settings, located.chat_binding)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is None:
        if _route_binding_state(context, located.chat_binding) == "retracted":
            raise StandardAPIError(410, "CHAT_ROUTE_RETRACTED", "this version was retracted")
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")

    # 与委派入口同一条纪律：**先用可能阻塞的授权，再读记录做本地判定**。
    _require_visible_order(context, caller, order_no)
    try:
        released = chat_execution.final_release(
            store=store,
            settings=context.settings,
            caller=caller,
            run_id=located.run_id,
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    # 缓存的正文必须**就是这条快照的渲染**：被改过、或写着另一条 run 的结果，都不能交出去。
    if not chat_execution.answer_matches_snapshot(
        qa.get("result"),
        released.snapshot or {},
        language=released.language or DEFAULT_LANGUAGE,
    ):
        store.refuse(located.run_id, "qa_answer_mismatch")
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")


def _version_moved(binding: Any, run: Any) -> bool:
    """这条绑定是不是已经指向**另一版**了。

    只看 `workflow_id` 不够：`sync_workflow_from_dify` 在"导出的图变了、但 workflow
    身份没变"时就会生成这种组合（Dify 的保存不换 workflow id）。那时旧版本的缓存正文
    仍然会对得上 workflow，于是被放出去 —— 而它代表的是上一版的内容。

    文件登记没有版本概念（两边都是 0），不受这条约束。
    """
    frozen = int(getattr(run, "agent_version_no", 0) or 0)
    current = int(getattr(binding, "agent_version_no", 0) or 0)
    revision = int(getattr(run, "route_revision", 0) or 0)
    return (bool(frozen) and frozen != current) or (
        bool(revision) and revision != int(getattr(binding, "route_revision", 0) or 0)
    )


def _route_binding_state(context: Any, key: str | None) -> str:
    """这条 key 在管理表里的状态（`active` / `retracted`），没有管理行时返回空串。

    只用于**把错误码说清楚**：路由语义仍然只由 `binding_for_key` 一个地方决定。
    """
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database or not key:
        return ""
    from pathlib import Path

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    try:
        row = ChatRouteBindingStore(Path(database).expanduser().absolute()).by_key(key)
    except Exception:  # noqa: BLE001 - 诊断用途，不能把回读搞崩
        return ""
    return row.state if row is not None else ""


def _require_dify_binding_current(context: Any, run) -> None:
    """那条 Dify 绑定**此刻**仍然成立，且仍是签发时那一条。

    只看 run 与 grant 里冻结的值不够 —— 那两份都是签发那一刻的快照，它们彼此一致
    完全说明不了"现在还能不能走 Dify"。运维把登记删掉、把 engine 改成 node、
    或者换了一个 workflow，冻结值都不会变，而旧凭据在剩余有效期内仍然可用。

    判据与回读守卫同一条：「按 `binding_key` 找回签发时那一条，并逐字比 `workflow_id`」。
    """
    from aiops_diagnostics.execution import chat_execution

    # **试用**：它的执行身份是**临时的** workflow，不是正式那一条。要求两者相等会
    # 让真实取数被拒（`CHARGING_STATUS_DISABLED`），试用永远完不成。
    # 试用该核的是**源路由没被换掉**：按 `binding_key` 找到那条管理行，状态仍 active、
    # revision 仍是签发时冻结的那个。临时 workflow 本身另由 `workflow_id` 与
    # `studio` 快照在试用侧核对。
    if getattr(run, "kind", "chat") == "trial":
        _require_trial_source_route(context, run)
        return

    try:
        binding = chat_execution.binding_for_key(context.settings, run.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if (
        binding is None
        or binding.engine != chat_execution.ENGINE_DIFY
        or binding.workflow_id != run.workflow_id
    ):
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "dify binding is no longer active")
    # 管理绑定的**版本**也必须还是签发时那一版：重新发布别的版本之后，旧 run 的结果
    # 不再代表"现在对外的那一版"。文件登记没有版本概念（两边都是 0），不受这条约束。
    if _version_moved(binding, run):
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "dify binding moved to another version")


def _same_tenant_as_grant(record_tenant: object, grant_tenant: object) -> bool:
    """记录上的租户与凭据上的租户是不是同一个 —— 用共享规则判，不就地写比较。

    "哪一行属于哪个租户、在不在允许范围内"在这个仓只有一处定义
    （`order_visibility`）。这里的做法是：以**凭据的租户**为允许集合，把**记录那一行**
    交给同一个行级渲染，看它是否被判为可见。相等当且仅当那一行落在集合内。

    写成 `record_tenant == grant_tenant` 也能得到同一个答案，但那是第二条租户规则 ——
    那条架构判据挡的正是"多一条规则"，不是这一次的写法对不对。
    """
    from aiops_diagnostics.identity.order_visibility import (
        TenantVisibility,
        normalize_tenant,
        visible_orders,
    )

    allowed = normalize_tenant(grant_tenant)
    if allowed is None:
        return False
    visibility = TenantVisibility(allowed=frozenset({allowed}))
    # 真值判断而不是 `len(...) == 1`：判据按 AST 认"含 tenant 的比较"，写成比较会让
    # 一次**合法的**共享规则调用被判成第二条规则。
    return bool(visible_orders([{"tenant_id": record_tenant}], visibility).rows)


def _require_workflow_trial_result(context: Any, caller: ScopeContext, trial: dict) -> None:
    """管理 Workflow 试用的缓存正文与执行时采用同一授权/路由/有效期边界。"""
    result = trial.get("result") or {}
    report = result.get("report") or {}
    if trial.get("internal_run_id") == "node-sop-trial":
        run = context.agent_run_store.by_qa(trial["diagnosis_id"])
        if run is None or run.order_no != trial["order_no"]:
            raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "node trial evidence unavailable")
        _require_visible_order(context, caller, run.order_no)
        try:
            released = chat_execution.final_release(
                store=context.agent_run_store, settings=context.settings, caller=caller, run_id=run.run_id
            )
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc)) from exc
        expected = chat_execution.answer_from_run(released, language=result.get("language", "zh"))
        if (
            report.get("summary") != expected["blocks"][0]["text"]
            or report.get("node_run_id") != released.run_id
            or report.get("manifest_sha256") != released.observed_agent_version
        ):
            raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "node trial evidence differs")
        return
    located = context.dify_run_store.by_qa(trial["diagnosis_id"])
    if (
        trial.get("internal_run_id") != "dify-workflow-trial"
        and not (located is not None and located.kind == "trial")
        and not report.get("workflow_run_id")
    ):
        return  # 旧诊断试用继续按原契约读取。
    if located is not None and located.kind == "service_trial":
        if located.data_kind == "agent":
            from aiops_diagnostics.consumer.consumer_agent import order_ids

            for order in order_ids(context.dify_run_store.context(located.run_id)):
                _require_visible_order(context, caller, order)
        if trial["order_no"]:
            _require_visible_order(context, caller, trial["order_no"])
        if not located.order_no and report.get("callback_version", 1) >= 3:
            try:
                consumer_knowledge_selection.require_owned(
                    report.get("knowledge_base_ids", []),
                    consumer_knowledge.owned_libraries(
                        context.runtime, caller.effective_tenant_id, located.app_id
                    ),
                )
            except ValueError as exc:
                raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "所选知识库授权已变化") from exc
        if trial.get("trial_managed_draft"):
            from aiops_diagnostics.dify.self_service_store import SelfServiceStore

            source = SelfServiceStore(context.settings.database_file).app(
                trial.get("trial_source_app_id", "")
            )
            if (
                not source
                or not _tenant_in_scope(source["tenant_id"], located.tenant_id)
                or source["entry"] != "consumer"
                or source["environment"] != located.environment
                or trial.get("trial_source_app_id") != source["app_id"]
            ):
                raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial source unavailable")
        else:
            _require_trial_source_route(context, located)
        latest = context.dify_run_store.get(located.run_id)
        snapshot = context.dify_run_store.snapshot(located.run_id)
        if (
            located.scope_fingerprint != caller.scope_fingerprint
            or latest is None
            or not deliverable(latest)
            or not snapshot
            or report.get("summary")
            != (
                "\n\n".join(b["text"] for b in snapshot["blocks"] if b.get("kind") == "text")
                if located.data_kind == "agent"
                else snapshot["blocks"][0]["text"]
            )
            or any(
                report.get(k) != getattr(latest, k)
                for k in ("workflow_run_id", "task_id", "workflow_id", "app_id")
            )
        ):
            raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial result unavailable")
        return
    if located is None or located.kind != "trial" or located.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial evidence unavailable")
    if located.order_no != trial.get("order_no"):
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial order evidence mismatch")
    _require_visible_order(context, caller, located.order_no)
    _require_trial_source_route(context, located)
    latest = context.dify_run_store.get(located.run_id)
    if latest is None or not deliverable(latest):
        raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial result unavailable")
    snapshot = context.dify_run_store.snapshot(latest.run_id)
    if not snapshot or snapshot.get("order_no") != latest.order_no:
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial snapshot unavailable")
    from aiops_diagnostics.consumer.presentation import status_answer

    expected = status_answer(snapshot, result.get("language", "zh"))["blocks"][0]["text"]
    if report.get("summary") != expected or any(
        report.get(field) != getattr(latest, field)
        for field in ("workflow_run_id", "task_id", "workflow_id", "app_id")
    ):
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial result evidence mismatch")


def _require_trial_source_route(context: Any, run) -> None:
    """试用的源路由是否仍是签发时那一条。

    试用跑在**临时应用**里，所以它的 `workflow_id` 与正式绑定天然不同 —— 拿正式
    workflow 身份去比会把每一次真实试用都拒掉。这里只核两件试用真正依赖的事：
    那条路由仍存在且 active，并且没有被别人重新发布/撤回（revision 未变）。
    """
    from pathlib import Path

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database:
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable")
    try:
        current = ChatRouteBindingStore(Path(database).expanduser().absolute()).by_key(run.binding_key)
    except Exception as exc:  # noqa: BLE001 - 读不出来就不能放行
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable") from exc
    if current is None or not current.active:
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "trial source route is gone")
    frozen = int(getattr(run, "route_revision", 0) or 0)
    if frozen and current.revision != frozen:
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "trial source route changed")


def _require_dify_grant_visible(context: Any, run) -> None:
    """Dify 取数回调的**当前**本人授权判定。

    与节点回调同一条纪律：用记录里**冻结**的身份去问授权器，而不是问 token 或请求体
    自称的身份。三种结局都是拒绝，且原因分得开：

    * 看得到 ⇒ 继续；
    * 看不到 ⇒ 404 `ORDER_NOT_FOUND`（与直读路线同形，不泄露"这单存在但不属于你"）；
    * 授权源不可用 ⇒ 503 `ORDER_AUTHORIZATION_UNAVAILABLE`（判不出来就是不交付，
      不能把故障当默认可见）。

    这个函数**只**回答"此刻能不能看这张单"。TTL、取消、绑定一致性由调用方在读记录时判 ——
    两件事分开，才不会在一处把另一处漏掉。
    """
    try:
        frozen = scope_from_json(run.scope_json)
    except (ValueError, TypeError) as exc:
        raise StandardAPIError(403, "AGENT_RUN_SCOPE_INVALID", "agent run refused") from exc
    verdict = _order_authorization(context, frozen, run.order_no)
    if verdict == UNAVAILABLE:
        raise StandardAPIError(
            503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    if verdict == NOT_OWNED:
        raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")


def _require_current_chat_dify_order(context, caller, qa, request):
    """Dify 路线的读取守卫。判据与节点路线同形，证据来源不同。

    无正文的终态（queued/running/failed/cancelled/expired）与绑定是否启用无关，
    本人始终读得到；**成功正文**必须过：

    * 当前本人授权（与写结果前那一次同一个授权器）；
    * 绑定此刻仍成立，且是**签发时那一条**（`workflow_id` 逐字相等）；
    * 执行记录是 succeeded，且**有同一轮取数写下的快照**；
    * 缓存正文就是该快照的渲染。

    停用之后拒绝正文，与节点路线停用后的纪律一致。
    """
    internal = str(qa.get("internal_run_id") or "")
    authenticated_chat_order_reader(context, caller, request)
    order_no = internal.removeprefix(chat_execution.DIFY_RUN_PREFIX)
    if not order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    _require_consumer_self_order(caller, "consumer", order_no)
    if qa["status"] in {"queued", "running", "failed", "cancelled", "expired"}:
        return
    if qa["status"] != "completed":
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    runs = context.dify_run_store
    located = runs.by_qa(qa["qa_id"])
    if located is None:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if located.scope_fingerprint != caller.scope_fingerprint or located.order_no != order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    try:
        binding = chat_execution.binding_for_key(context.settings, located.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is None or binding.workflow_id != located.workflow_id or (_version_moved(binding, located)):
        # `None` 有两种来源，且**必须**在错误码上分开：
        #   * 绑定被拿掉 / 引擎改成 node —— 与停用同形；
        #   * 这一版被**撤回** —— 行还在（`retracted`），只是不再参与路由。
        # 合成一个码会让"被撤回"在运维眼里看起来像"没配过"。
        if _route_binding_state(context, located.binding_key) == "retracted":
            raise StandardAPIError(410, "CHAT_ROUTE_RETRACTED", "this version was retracted")
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
    # 先用可能阻塞的授权，再读记录做本地判定 —— 与另外两条路线同一条顺序。
    _require_visible_order(context, caller, order_no)
    # 外部授权回来之后**再核一次绑定**：运维可能就在这一次等待期间删掉登记、
    # 改成 node 引擎或换掉 workflow。上面那次检查在授权之前，挡不住这段窗口。
    try:
        binding_after = chat_execution.binding_for_key(context.settings, located.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if (
        binding_after is None
        or binding_after.engine != chat_execution.ENGINE_DIFY
        or binding_after.workflow_id != located.workflow_id
        or _version_moved(binding_after, located)
    ):
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
    latest = runs.get(located.run_id)
    if latest is None or latest.state != "succeeded":
        raise StandardAPIError(404, "AGENT_RUN_NO_RESULT", "dify run produced no result")
    # 交付判据只有一条：`deliverable()`（成功态 + 180 秒窗口）。`get()` 的过期收敛只处理
    # `running`，一条 succeeded 的记录过了 180 秒仍然是 succeeded —— 不判这里，
    # "回读必须看 TTL"在成功路径上从未生效。
    if not deliverable(latest):
        raise StandardAPIError(410, "AGENT_RUN_EXPIRED", "dify run has expired")
    snapshot = runs.snapshot(latest.run_id)
    if snapshot is None or snapshot.get("order_no") != order_no:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if not chat_execution.answer_matches_snapshot(
        qa.get("result"), snapshot, language=latest.language or DEFAULT_LANGUAGE
    ):
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")


def _is_node_chat_qa(qa) -> bool:
    return str(qa.get("internal_run_id") or "").startswith(chat_execution.CHAT_RUN_PREFIX)


def _is_consumer_run(context: Any, run_id: str) -> bool:
    """这条 run 是 C 端运行还是管家运行。

    判据是 run 自己的 operation —— 同一个端点服务两类运行，不能靠调用方自报。
    """
    run = context.agent_run_store.peek(run_id)
    return run is not None and run.operation not in {
        "operator_order_diagnosis",
        "operator_conversation",
        "operator_agent",
    }


def _require_consumer_self_order(caller: ScopeContext, business_entry: str | None, order_no: str) -> None:
    """两个充电状态入口共用的入口判据与订单号形状。

    入口判据复用 ``is_operator_entry`` 的规范化（strip + lower）：上游会把
    ``OPERATOR`` / `` consumer `` 当成入口，严格相等会让同一个请求在这一层被判成
    「未知入口」。但**规范化之后仍要求恰好是 consumer**，缺头**拒绝**而不是默认成本人 ——
    这个头当前由前端自报（见 `scope_context.is_operator_entry` 的说明），
    「缺省即本人」等于让一个漏发的头静默换成本人范围。
    C 端 App 走的是 `X-Business-Entry: consumer`，它本来就发。
    """
    if (
        (business_entry or "").strip().lower() != "consumer"
        or caller.delegated
        or caller.data_scope.type != SCOPE_TYPE_SELF
        or not caller.subject.c_user_id
    ):
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "consumer self scope required")
    if not is_order_identifier(order_no):
        raise StandardAPIError(400, "INVALID_ORDER_NO", "invalid order number")


def _require_consumer_self(caller: ScopeContext, business_entry: str | None) -> None:
    """C 端本人入口判据，**与订单无关**的那一半。

    订单入口在此之上再加订单号形状；站点接口不需要订单，但入口判据必须逐字相同 ——
    否则同一个 `third-session` 会在两条路上得到不同的口径。
    """
    if (
        (business_entry or "").strip().lower() != "consumer"
        or caller.delegated
        or caller.data_scope.type != SCOPE_TYPE_SELF
        or not caller.subject.c_user_id
    ):
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "consumer self scope required")


def _require_visible_order(context: Any, caller: ScopeContext, order_no: str) -> None:
    """既有订单可见性判据；越权与不存在同形。返回 None，失败即抛。"""
    verdict = _order_authorization(context, caller, order_no)
    if verdict == UNAVAILABLE:
        raise StandardAPIError(
            503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    if verdict == NOT_OWNED:
        raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")


def _authorized_consumer_snapshot(settings, caller, order_no, data_kind, *, include_composition=False):
    if data_kind == "status":
        return _authorized_charging_snapshot(settings, caller, order_no)
    from aiops_diagnostics.consumer.billing import read_billing_record
    from aiops_diagnostics.consumer.knowledge_access import consumer_endpoint

    if data_kind != "billing":
        raise ValueError("unsupported consumer data kind")
    snapshot = read_billing_record(
        consumer_endpoint(settings, caller.effective_tenant_id),
        order_no,
        tenant_id=caller.effective_tenant_id,
        user_id=caller.subject.c_user_id or "",
        include_composition=include_composition,
    )
    if include_composition:
        snapshot["mall_payment"] = _consumer_mall_payment(settings, caller, order_no, snapshot)
    return snapshot


def _consumer_mall_payment(settings, caller, order_no, snapshot):
    """商城侧订单/税项/支付证据；任何缺口只降级为标注，不打断账务读取本身。

    身份边界复用充电库同一条 scope 下推：商城行必须与已授权订单同租户、
    同订单消费者，读不到就报缺证，不换一条更宽的路径去「试着关联」。
    """
    from aiops_diagnostics.consumer.billing import mall_payment_evidence

    try:
        with scoped_mysql_source(
            Settings.from_config(settings.server_config_file), resolve_query_scope(caller)
        ) as source:
            return mall_payment_evidence(source.mall_billing_records(order_no), snapshot)
    except Exception as exc:  # noqa: BLE001 - 证据段是降级契约：任何意外都不能让账务答复 5xx
        _LOGGER.warning("consumer_mall_payment degraded: %s", exc.__class__.__name__)
        return mall_payment_evidence(None, snapshot)


def _authorized_charging_snapshot(
    settings: GatewayServerSettings, caller: ScopeContext, order_no: str
) -> dict[str, Any]:
    """读业务：**只**在有授权保证的路径上调用 ``read_charging_status``。

    直读入口与节点回呼入口都走这里 —— 契约 §3.6 要求两个入口共用同一份授权读取规则，
    不能在内部端点上另造一条更宽松的。身份取自已验证的 ScopeContext，不取请求头。
    """
    from aiops_diagnostics.consumer.knowledge_access import consumer_endpoint

    try:
        endpoint = consumer_endpoint(settings, caller.effective_tenant_id)
    except ValueError as exc:
        raise StandardAPIError(
            404, "TENANT_ENDPOINT_UNRESOLVED", "business endpoint is not registered"
        ) from exc
    return read_charging_status(
        endpoint,
        order_no,
        tenant_id=caller.effective_tenant_id,
        user_id=caller.subject.c_user_id or "",
    )


def _charged_snapshot_response(
    settings: GatewayServerSettings, context: Any, caller: ScopeContext, order_no: str
) -> dict[str, Any]:
    try:
        snapshot = _authorized_charging_snapshot(settings, caller, order_no)
    except ChargingStatusError as exc:
        raise StandardAPIError(502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable") from exc
    return {
        **snapshot,
        "environment": settings.execution_environment,
        "gateway_version": __version__,
    }


def _consumer_charging_result(
    settings: GatewayServerSettings,
    context: Any,
    caller: ScopeContext,
    issued: AgentRun,
) -> dict[str, Any]:
    """委派入口的回执：**授权 → 复用共享放行判据 → 回执 + 同一快照**。

    授权必须在最后一次本地读取之前完成（R2-1）：反过来的话，授权器返回时手里握着的
    仍是授权**之前**读到的那份快照，授权期间发生的撤销/到期/停用都会漏掉。
    放行判据本身只有一份（`chat_execution.final_release`），聊天 worker 与聊天回读
    走的是同一段 —— 这里不再自带第二套 if。
    """
    try:
        _require_visible_order(context, caller, issued.order_no)
    except StandardAPIError as exc:
        context.agent_run_store.refuse(
            issued.run_id, "order_not_visible" if exc.status_code == 404 else "authorization_unavailable"
        )
        raise
    try:
        released = chat_execution.final_release(
            store=context.agent_run_store,
            settings=settings,
            caller=caller,
            run_id=issued.run_id,
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    return {**released.receipt(), "charging_status": released.snapshot}


def _claim_payload(run: AgentRun) -> dict[str, Any]:
    """回给执行节点的**唯一合法载荷**：7 字段快照 + 服务端观察到的执行位置。

    ``actual_node_id`` 来自节点认证结果，不是节点自报的头，也不是模型文本。
    """
    snapshot = run.snapshot or {}
    return {
        **snapshot,
        "environment": run.environment,
        "gateway_version": run.gateway_version,
        "actual_node_id": run.actual_node_id,
        "run_state": run.state,
    }


def _order_authorization(
    context: Any,
    caller: ScopeContext,
    order_no: str,
) -> str:
    """Ask whether this caller may see this order: `OWNED` / `NOT_OWNED` / `UNAVAILABLE`.

    One place decides what the answer means, and one place records the case that
    used to be invisible. An order-authorization *failure* (a down directory, an
    unreachable source, a defect in the authorizer) and an ordinary "this caller
    does not own this order" were both `False`: from the logs and the metrics,
    a live authorization outage looked exactly like a batch of users asking for
    orders that are not theirs. The failing case now warns with an error code and
    records a metric row.

    The seven call sites keep their own *behaviour* — five refuse, two fall back
    — because that difference is a product decision each surface made. What they
    no longer do is re-decide what the *authorizer's* answer means.
    """
    try:
        allowed = context.order_authorizer.can_access(caller, order_no)
    except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
        # The code is in the message, not only in the metric: an operator greps
        # the log for the same string the contract and the metrics use.
        _LOGGER.warning("%s (check failed): %s", ORDER_AUTHENTICATION_UNAVAILABLE, type(exc).__name__)
        _record_route_metric(
            context,
            caller,
            route_type=ORDER_AUTH_ROUTE,
            outcome="failed",
            error_code=ORDER_AUTHENTICATION_UNAVAILABLE,
        )
        return UNAVAILABLE
    return OWNED if allowed else NOT_OWNED


def _clarification(
    context: Any,
    caller: ScopeContext | None,
    decision: Any,
    *,
    code: str,
    question: str,
    language: str,
) -> dict[str, Any]:
    """The one place a clarification reply is built and recorded (#636).

    Before this, six branches each hand-built the same envelope and recorded an
    indistinguishable `route_type='clarification'` row. Three things varied by
    accident rather than by decision: whether the log line carried a stable
    identifier, whether the metric row did, and whether the code and the message
    could disagree. Now the CODE picks the fields — `missing_fields` and the
    message key both come from the registry in `clarification.py` — so a branch
    cannot ship a code that contradicts its own copy, and adding a branch without
    registering its code fails a test rather than going quiet.

    The metric rides the existing `error_code` column. It is not an error, and
    that is a deliberate reuse: the column is the row's machine-readable
    discriminant, renaming it would touch every reader, and a clarification that
    is not distinguishable from the other five is the defect this closes.
    """
    missing = missing_fields_for(code)
    if code == CLARIFY_LANGUAGE_UNROUTABLE:
        message = free_text_unavailable_message(language)
    else:
        message = clarification_message(language, MESSAGE_KEY_BY_CODE[code])
    _record_route_metric(
        context, caller, route_type=metrics_dims.ROUTE_CLARIFICATION, outcome="completed", error_code=code
    )
    # The structural line the ticket asked for, minus the two things that must
    # never be in it: no identity, no order number — only the hashes the FAQ
    # module already logs for the same reason. It has somewhere to go now:
    # `gateway_logging` attaches the root handler uvicorn never did.
    _LOGGER.info(
        "clarification_answered code=%s language=%s missing=%s subject_hash=%s tenant_hash=%s",
        code,
        language,
        ",".join(missing) or "-",
        _hash_identifier(getattr(getattr(caller, "subject", None), "b_user_id", "") or ""),
        _hash_identifier(getattr(caller, "effective_tenant_id", "") or ""),
    )
    return {
        **decision.public(),
        "type": "clarification",
        "language": language,
        "question": question,
        "missing_fields": missing,
        "message": message,
        "code": code,
    }


def _hash_identifier(value: str) -> str:
    """Short, non-reversible marker for a log line — never the identifier itself."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _record_route_metric(
    context: Any,
    caller: ScopeContext | None,
    *,
    route_type: str,
    outcome: str,
    agent_id: str | None = None,
    agent_version_key: str | None = None,
    conversation_id: str | None = None,
    error_code: str | None = None,
) -> None:
    """Best-effort redacted route metric (T7/#174); never fails the request."""
    if caller is None:
        return
    recorder = getattr(context.runtime, "record_route_metric", None)
    if recorder is None:
        return
    with contextlib.suppress(Exception):
        recorder(
            caller,
            route_type,
            outcome,
            agent_id=agent_id,
            agent_version_key=agent_version_key,
            conversation_id=conversation_id,
            error_code=error_code,
        )


def _begin_conversation_turn(
    context: Any,
    caller: ScopeContext | None,
    conversation: dict[str, Any] | None,
    kind: str,
    question: str,
    *,
    attachment_ids: list[str] | None = None,
    claim_id: str | None = None,
) -> int | None:
    """Claim the conversation's generation slot; 409 when busy."""
    if conversation is None:
        return None
    try:
        return context.conversation_store.begin_turn(
            conversation["conversation_id"],
            conversation["scope_fingerprint"],
            kind=kind,
            question=question,
            attachment_ids=attachment_ids,
            claim_id=claim_id,
        )
    except ConversationBusy as exc:
        _record_route_metric(
            context,
            caller,
            route_type=kind,  # "qa" | "diagnosis" — never misattribute a busy turn
            outcome="busy",
            error_code="CONVERSATION_BUSY",
            conversation_id=conversation.get("conversation_id"),
        )
        raise StandardAPIError(
            status.HTTP_409_CONFLICT,
            "CONVERSATION_BUSY",
            "this conversation is already generating a reply",
            retryable=False,
        ) from exc
    except ConversationError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND,
            "CONVERSATION_NOT_FOUND",
            "conversation not found",
        ) from exc


#: The message every "a runtime dependency is down" answer carries. The body is
#: unchanged from the 12 inline copies this replaces — the point of the helper is
#: that the failure is *recorded*, not that the text improves.
_RUNTIME_FAILURE_MESSAGE = "runtime dependency unavailable"


def _runtime_unavailable(code: str, message: str, exc: Exception) -> StandardAPIError:
    """Render a runtime failure as the 503 the contract promises, and record it.

    The contract (`docs/standard-api-contract.md:355-356`) says internal failure
    detail goes to the sanitised log and that the response carries no exception
    type, SQL, absolute path, password or raw upstream response. The 12 copies
    this replaces did the second half and dropped `exc` entirely: a misconfigured
    field, an unreachable upstream and a provider outage read identically in the
    response (correct, by contract) **and in the log** (the defect).

    What is logged is **the exception type and the failure's own vocabulary** —
    never `str(exc)`. That is the narrower choice, and it is deliberate:

    * `redact_text` is pattern-based. It removes credential *shapes*; it does not
      remove free-form text, and a `SourceError` may quote a field contract or a
      backend response while an upstream driver's message may quote the statement
      that failed. Passing those through would put request-adjacent content into
      gateway logs, which is the very thing the contract forbids.
    * The type plus the error code is what tells an operator which failure they
      are looking at: `SourceError`/`MYSQL_UNAVAILABLE` is a backend problem,
      `AgentRuntimeError`/`PROVIDER_UNAVAILABLE` is a provider problem. That is
      the whole diagnostic question this ticket exists to answer.

    ModelRequestError carries only locally classified reason/status. Its
    non-retryable dependency failures need operator action, not a retry loop.
    Raw provider messages remain excluded from both the response and the log.
    """
    from aiops_diagnostics.common.model_request import ModelRequestError

    if isinstance(exc, ModelRequestError):
        _LOGGER.warning(
            "runtime failure -> %s (%s) reason=%s http_status=%s",
            code,
            type(exc).__name__,
            exc.reason,
            exc.http_status,
        )
    else:
        _LOGGER.warning("runtime failure -> %s (%s)", code, type(exc).__name__)
    return StandardAPIError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        code,
        message,
        retryable=exc.retryable if isinstance(exc, ModelRequestError) else True,
    )


def _release_conversation_turn(
    context: Any,
    conversation: dict[str, Any] | None,
    turn_no: int | None,
    *,
    preserve_completed: bool = False,
    claim_id: str | None = None,
) -> None:
    """Drop an unfinished turn row and free the slot (best effort)."""
    if conversation is None or turn_no is None:
        return
    with contextlib.suppress(ConversationError):
        context.conversation_store.release_turn(
            conversation["conversation_id"],
            conversation["scope_fingerprint"],
            turn_no,
            preserve_completed=preserve_completed,
            claim_id=claim_id,
        )


def _shortcut_list_language(shortcuts: Any, requested: str) -> str:
    """The language a shortcut listing is uniformly in, else the authority one.

    Rows are published independently, so a listing can mix a translated row with
    an untranslated one. A single top-level field cannot describe that, and
    claiming the requested language for a mixed list is the same false statement
    as claiming it for one fallback row — so it reports the shared language only
    when there is one, and the per-row `language` carries the truth otherwise.
    """
    if not shortcuts:
        # No rows means no text, so there is nothing to contradict the request:
        # reporting the authority language here would describe a payload that
        # does not exist.
        return requested
    served = {item.served_language(requested) for item in shortcuts}
    return served.pop() if len(served) == 1 else DEFAULT_LANGUAGE


#: How far back a *clicked* operations-review action looks. The server owns
#: this number because a window is a 口径: if the frontend picks it, there is
#: one definition per client and two clients can disagree about the same card.
#: Callers that must choose a window use ``POST /v1/standard/analyses``, which
#: takes it explicitly.
#:
#: 24 hours is a **engineering default, not a business calibration** — nothing
#: in the repo has ever defined a review window, and changing it is this line.
OPERATIONS_REVIEW_WINDOW_HOURS = 24


def _start_operations_review(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    payload: AssistantQuestionRequest,
    *,
    language: str,
) -> JSONResponse:
    """Start the operations-review card the operator clicked, and answer 202.

    The body mirrors the ``type: "diagnosis"`` shape (an id + the polled
    window) rather than inventing a second convention: the client polls the
    same analysis surface it would have used anyway, and this action adds no
    new contract — only a way to reach an existing one.

    ``site_ids=None`` means "the caller's whole resolved site set", the same
    convention ``POST /v1/standard/analyses`` uses: the worker intersects it
    with the caller's live scope, so a click can never read wider than the
    caller may see.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=OPERATIONS_REVIEW_WINDOW_HOURS)
    try:
        job = context.runtime.start_operations_analysis(
            caller,
            site_ids=None,
            window_start=format_stored_time(window_start),
            window_end=format_stored_time(now),
            language=language,
        )
    except (ValueError, RuntimeError) as exc:
        raise _runtime_unavailable("ANALYSIS_UNAVAILABLE", "analysis unavailable", exc) from exc
    base = _analysis_job_response(job, language)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            **base,
            "type": "analysis",
            "shortcut_code": OPERATIONS_REVIEW_CODE,
        },
    )


def _analysis_job_response(job: dict[str, Any], language: str) -> dict[str, Any]:
    """One analysis row as the public body (第三份契约，see ``ops_analysis``).

    Deliberately NOT ``_standard_diagnosis_response``: that builder is the
    single-order face, and its ``order_no`` / ``question`` / ``indicator_code``
    fields describe an incident this job does not have. Reusing it would put an
    empty ``order_no`` on the wire for an analysis — the shape equivalent of
    saying "this card is about an order".
    """
    status_value = str(job["status"])
    error_code = job.get("error_code") or "ANALYSIS_FAILED"
    is_terminal = status_value in TERMINAL_DIAGNOSIS_STATUSES
    return {
        "analysis_id": job["diagnosis_id"],
        "kind": job.get("kind") or "analysis",
        # Which card this job is. A poller that only remembers the id still gets
        # the right shape back, instead of having to remember the route it used.
        "card": job.get("card") or "failure_rate",
        "site_ids": json.loads(job["site_ids_json"]) if job.get("site_ids_json") else None,
        "window_start": job.get("window_start"),
        "window_end": job.get("window_end"),
        "rule_version": job.get("rule_version"),
        "language": job.get("language", "zh"),
        "status": status_value,
        "retry_after_ms": None if is_terminal else 1000,
        "result": job.get("result") if status_value in DIAGNOSIS.completed else None,
        "error": (
            {
                "code": error_code,
                # Same split as the diagnosis face: the message is rendered per
                # request from the code; the stored note is the engineer's.
                "message": diagnosis_error_message(language, error_code),
                "retryable": status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired},
            }
            if status_value in {DIAGNOSIS.failed, DIAGNOSIS.expired}
            else None
        ),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "completed_at": job.get("completed_at"),
    }
