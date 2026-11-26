"""Gateway 应用装配与核心兼容导出门面。

身份解析器装配、HTTP 响应视图与会话/订单辅助已下沉至同包单职责模块：
- ``gateway/caller_auth_wiring.py``（身份链构建与认证错误映射）
- ``gateway/response_views.py``（响应序列化、澄清构造与统一 503 日志）
- ``gateway/conversation_helpers.py``（会话轮次、订单归属守卫与运维复盘）
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from aiops_diagnostics import __version__
from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.consumer import consumer_knowledge as consumer_knowledge
from aiops_diagnostics.conversation.conversation_store import ConversationStore
from aiops_diagnostics.conversation.faq import (
    FAQCatalog,
    MySQLPlatformDirectory,
    PlatformIdentityResolver,
)
from aiops_diagnostics.data.charging_status import read_charging_status
from aiops_diagnostics.data.sources import scoped_mysql_source
from aiops_diagnostics.dify.dify_run_store import DifyRunStore
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.execution.agent_runs import (
    AgentRun,
    AgentRunStore,
)
from aiops_diagnostics.execution.agent_runs import (
    SnailDispatcher as SnailDispatcher,
)
from aiops_diagnostics.gateway import caller_auth_wiring as _auth_wiring
from aiops_diagnostics.gateway import consumer_chat_dispatch as _chat_dispatch
from aiops_diagnostics.gateway import conversation_helpers as _conv_helpers
from aiops_diagnostics.gateway import dify_publish_ops as _publish_ops
from aiops_diagnostics.gateway import request_observation
from aiops_diagnostics.gateway import response_views as _views
from aiops_diagnostics.gateway.caller_auth_wiring import (
    _authenticate_caller as _authenticate_caller,
)
from aiops_diagnostics.gateway.caller_auth_wiring import (
    _caller_resolver,
    _operator_site_scope,
    _shop_directory,
)
from aiops_diagnostics.gateway.caller_auth_wiring import (
    _platform_error as _platform_error,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    NOT_OWNED as NOT_OWNED,
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
    _same_tenant_as_grant as _same_tenant_as_grant,
)
from aiops_diagnostics.gateway.consumer_chat_dispatch import (
    _version_moved as _version_moved,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    OPERATIONS_REVIEW_WINDOW_HOURS as OPERATIONS_REVIEW_WINDOW_HOURS,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _begin_conversation_turn as _begin_conversation_turn,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _operator_conversation as _operator_conversation,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _operator_json as _operator_json,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _order_authorization as _order_authorization,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _release_conversation_turn as _release_conversation_turn,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _require_conversation as _require_conversation,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _require_operator_company_token as _require_operator_company_token,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _require_visible_order as _require_visible_order,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _resolve_conversation as _resolve_conversation,
)
from aiops_diagnostics.gateway.conversation_helpers import (
    _start_operations_review as _start_operations_review,
)
from aiops_diagnostics.gateway.dependencies import (
    STANDARD_DIAGNOSIS_SCOPE as STANDARD_DIAGNOSIS_SCOPE,
)
from aiops_diagnostics.gateway.dependencies import (
    build_dependencies,
    install_exception_handlers,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _advance_route_binding as _advance_route_binding,
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
    _sync_bound_app,
)
from aiops_diagnostics.gateway.dify_publish_ops import (
    _tenant_in_scope as _tenant_in_scope,
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
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime, close_gateway_runtime
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.gateway.response_views import (
    NO_STORE_PATH_PREFIXES as NO_STORE_PATH_PREFIXES,
)
from aiops_diagnostics.gateway.response_views import (
    _analysis_job_response as _analysis_job_response,
)
from aiops_diagnostics.gateway.response_views import (
    _assistant_question_response as _assistant_question_response,
)
from aiops_diagnostics.gateway.response_views import (
    _clarification as _clarification,
)
from aiops_diagnostics.gateway.response_views import (
    _conversation_response as _conversation_response,
)
from aiops_diagnostics.gateway.response_views import (
    _hash_identifier as _hash_identifier,
)
from aiops_diagnostics.gateway.response_views import (
    _health_job_response as _health_job_response,
)
from aiops_diagnostics.gateway.response_views import (
    _no_store_headers as _no_store_headers,
)
from aiops_diagnostics.gateway.response_views import (
    _qa_user_message as _qa_user_message,
)
from aiops_diagnostics.gateway.response_views import (
    _record_route_metric as _record_route_metric,
)
from aiops_diagnostics.gateway.response_views import (
    _runtime_unavailable as _runtime_unavailable,
)
from aiops_diagnostics.gateway.response_views import (
    _shortcut_list_language as _shortcut_list_language,
)
from aiops_diagnostics.gateway.routers import (
    agents as agents_router,
)
from aiops_diagnostics.gateway.routers import (
    assistant as assistant_router,
)
from aiops_diagnostics.gateway.routers import (
    assistant_actions as assistant_actions_router,
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
    health_faq as health_faq_router,
)
from aiops_diagnostics.gateway.routers import (
    internal_callbacks as internal_callbacks_router,
)
from aiops_diagnostics.gateway.routers import (
    knowledge as knowledge_router,
)
from aiops_diagnostics.gateway.routers import (
    media as media_router,
)
from aiops_diagnostics.gateway.routers import (
    orders_sites as orders_sites_router,
)
from aiops_diagnostics.gateway.routers import (
    shortcuts as shortcuts_router,
)
from aiops_diagnostics.gateway.schemas import (
    AssistantQuestionRequest as AssistantQuestionRequest,
)
from aiops_diagnostics.gateway.schemas import (
    DifyTrialRequest as DifyTrialRequest,
)
from aiops_diagnostics.gateway.schemas import (
    HealthReportJobRequest as HealthReportJobRequest,
)
from aiops_diagnostics.gateway.schemas import (
    ManagedTrialRequest as ManagedTrialRequest,
)
from aiops_diagnostics.gateway.schemas import (
    OperatorToolAuthorizationRequest as OperatorToolAuthorizationRequest,
)
from aiops_diagnostics.identity.caller_auth import (
    CallerContextResolver,
    DisabledOrderAuthorizer,
    OrderAuthorizer,
    ScopedOrderAuthorizer,
)
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.publication.agent_lifecycle import (
    AgentManager,
    AgentStore,
    allowed_models_from_settings,
)
from aiops_diagnostics.publication.shortcut_lifecycle import (
    ShortcutManager,
    ShortcutStore,
)

# 供外部单测通过 ``gateway_api._auth_wiring`` / ``_views`` / ``_conv_helpers`` 无声保留接口兼容。
del _auth_wiring, _conv_helpers, _views

_LOGGER = logging.getLogger("aiops.gateway")


class GatewayAPI:
    """运行中 Gateway 应用的装配上下文容器。"""

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
    """装配 FastAPI Gateway 应用、校验环境配置并挂载全部分域路由。"""
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
        health_faq_router,
        media_router,
        assistant_router,
        assistant_actions_router,
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


def _chat_dispatch_hooks() -> ChatDispatchHooks:
    """保留 ``gateway_api`` 模组级 hook 点，确保测试 monkeypatch 对下游派发链生效。"""
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
    """校验控制台侧签发的管家公司会话并刷新站点范围。"""
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
