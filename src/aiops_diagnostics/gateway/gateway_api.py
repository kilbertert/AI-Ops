from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
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
)
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.common.scalar_rules import format_stored_time
from aiops_diagnostics.consumer import consumer_knowledge as consumer_knowledge
from aiops_diagnostics.conversation.clarification import (
    CLARIFY_LANGUAGE_UNROUTABLE,
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
from aiops_diagnostics.data.charging_status import read_charging_status
from aiops_diagnostics.data.sources import SourceError, scoped_mysql_source
from aiops_diagnostics.dify.dify_run_store import DifyRunStore
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.execution.agent_runs import (
    AgentRun,
    AgentRunStore,
)
from aiops_diagnostics.execution.agent_runs import (
    SnailDispatcher as SnailDispatcher,
)
from aiops_diagnostics.gateway import consumer_chat_dispatch as _chat_dispatch
from aiops_diagnostics.gateway import dify_publish_ops as _publish_ops
from aiops_diagnostics.gateway import metrics_store as metrics_dims
from aiops_diagnostics.gateway import request_observation
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    NOT_OWNED as NOT_OWNED,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    ORDER_AUTH_ROUTE as ORDER_AUTH_ROUTE,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    ORDER_AUTHENTICATION_UNAVAILABLE as ORDER_AUTHENTICATION_UNAVAILABLE,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    OWNED as OWNED,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    UNAVAILABLE as UNAVAILABLE,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    ChatDispatchHooks,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _claim_payload as _claim_payload,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _consumer_intent_clarification as _consumer_intent_clarification,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _is_consumer_run as _is_consumer_run,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _is_node_chat_qa as _is_node_chat_qa,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _require_consumer_self as _require_consumer_self,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _require_consumer_self_order as _require_consumer_self_order,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _require_dify_binding_current as _require_dify_binding_current,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _require_matching_exec_config as _require_matching_exec_config,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _require_trial_source_route as _require_trial_source_route,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _route_binding_state as _route_binding_state,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _same_tenant_as_grant as _same_tenant_as_grant,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _version_moved as _version_moved,
)
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
from aiops_diagnostics.gateway.dify_publish_ops import (
    _advance_route_binding as _advance_route_binding,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _current_route_revision as _current_route_revision,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _declared_in_file as _declared_in_file,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _key_ref_for as _key_ref_for,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _managed_app_draft as _managed_app_draft,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _managed_app_route as _managed_app_route,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _publish_allowlist as _publish_allowlist,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _publish_initiator as _publish_initiator,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _record_publish_audit as _record_publish_audit,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _restore_bound_app as _restore_bound_app,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _sync_bound_app as _sync_bound_app,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _tenant_in_scope as _tenant_in_scope,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _TenantOnly as _TenantOnly,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _trial_run_context as _trial_run_context,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _workflow_trial_draft as _workflow_trial_draft,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _workspace_for_tenant as _workspace_for_tenant,
)
from aiops_diagnostics.gateway.errors import StandardAPIError as StandardAPIError
from aiops_diagnostics.gateway.gateway_config import (
    GatewayServerSettings,
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
from aiops_diagnostics.identity.query_scope import (
    ShopDirectory,
    UpmsShopDirectory,
)
from aiops_diagnostics.identity.scope_context import (
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


def _publish_bound_app(
    context: Any,
    app_id: str,
    *,
    initiator: tuple[str, str, str] = ("", "", ""),
) -> dict[str, Any]:
    return _publish_ops._publish_bound_app(
        context,
        app_id,
        initiator=initiator,
        sync_bound_app_fn=_sync_bound_app,
    )


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
        from aiops_diagnostics.identity.caller_auth import RegisteredCallerResolver
        from aiops_diagnostics.onboarding.registry import for_settings

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
    from aiops_diagnostics.gateway.public_openapi import install_public_docs
    from aiops_diagnostics.onboarding.trial import register as register_environment_trial

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
    """错误响应也带 ``Cache-Control: no-store``，但**只**对充电状态那一组路径。"""
    if request.url.path.startswith(NO_STORE_PATH_PREFIXES):
        return {"Cache-Control": "no-store"}
    return {}


def _chat_dispatch_hooks() -> ChatDispatchHooks:
    return ChatDispatchHooks(
        authenticate_caller=_authenticate_caller,
        order_authorization=_order_authorization,
        begin_conversation_turn=_begin_conversation_turn,
        release_conversation_turn=_release_conversation_turn,
        assistant_question_response=_assistant_question_response,
        clarification=_clarification,
        record_route_metric=_record_route_metric,
        require_operator_company_token=_require_operator_company_token,
        scoped_mysql_source_fn=scoped_mysql_source,
        write_private_text_fn=write_private_text,
        read_charging_status_fn=read_charging_status,
        authorized_charging_snapshot_fn=_authorized_charging_snapshot,
        consumer_mall_payment_fn=_consumer_mall_payment,
    )


def _consumer_order_chat_via_node(
    context, caller, decision, payload, conversation, language, order_no, binding
):
    return _chat_dispatch._consumer_order_chat_via_node(
        context,
        caller,
        decision,
        payload,
        conversation,
        language,
        order_no,
        binding,
        hooks=_chat_dispatch_hooks(),
    )


def _consumer_order_chat(context, caller, decision, payload, conversation, language, request):
    return _chat_dispatch._consumer_order_chat(
        context,
        caller,
        decision,
        payload,
        conversation,
        language,
        request,
        hooks=_chat_dispatch_hooks(),
    )


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
    return _chat_dispatch.authenticated_chat_order_reader(
        context, caller, request, authenticate_caller_fn=_authenticate_caller
    )


def _require_current_chat_order(context, caller, qa, request):
    return _chat_dispatch._require_current_chat_order(
        context, caller, qa, request, hooks=_chat_dispatch_hooks()
    )


def _require_current_chat_node_order(context, caller, qa, request):
    return _chat_dispatch._require_current_chat_node_order(
        context, caller, qa, request, hooks=_chat_dispatch_hooks()
    )


def _require_workflow_trial_result(context: Any, caller: ScopeContext, trial: dict) -> None:
    _chat_dispatch._require_workflow_trial_result(
        context, caller, trial, order_authorization_fn=_order_authorization
    )


def _require_dify_grant_visible(context: Any, run) -> None:
    _chat_dispatch._require_dify_grant_visible(context, run, order_authorization_fn=_order_authorization)


def _require_current_chat_dify_order(context, caller, qa, request):
    return _chat_dispatch._require_current_chat_dify_order(
        context, caller, qa, request, hooks=_chat_dispatch_hooks()
    )


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
    return _chat_dispatch._authorized_consumer_snapshot(
        settings,
        caller,
        order_no,
        data_kind,
        include_composition=include_composition,
        authorized_charging_snapshot_fn=_authorized_charging_snapshot,
        consumer_mall_payment_fn=_consumer_mall_payment,
    )


def _consumer_mall_payment(settings, caller, order_no, snapshot):
    return _chat_dispatch._consumer_mall_payment(
        settings, caller, order_no, snapshot, scoped_mysql_source_fn=scoped_mysql_source
    )


def _authorized_charging_snapshot(
    settings: GatewayServerSettings, caller: ScopeContext, order_no: str
) -> dict[str, Any]:
    return _chat_dispatch._authorized_charging_snapshot(
        settings, caller, order_no, read_charging_status_fn=read_charging_status
    )


def _charged_snapshot_response(
    settings: GatewayServerSettings, context: Any, caller: ScopeContext, order_no: str
) -> dict[str, Any]:
    return _chat_dispatch._charged_snapshot_response(
        settings,
        context,
        caller,
        order_no,
        authorized_charging_snapshot_fn=_authorized_charging_snapshot,
    )


def _consumer_charging_result(
    settings: GatewayServerSettings,
    context: Any,
    caller: ScopeContext,
    issued: AgentRun,
) -> dict[str, Any]:
    return _chat_dispatch._consumer_charging_result(
        settings, context, caller, issued, order_authorization_fn=_order_authorization
    )


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
