"""Gateway FastAPI 依赖注入、统一异常处理器与知识源解析装配。"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aiops_diagnostics.common.http_auth import (
    INTERNAL_TOKEN_HEADER,
    TIMESTAMP_HEADER,
    validate_internal_token,
)
from aiops_diagnostics.common.i18n import resolve_language
from aiops_diagnostics.conversation.faq import (
    PLATFORM_UNAVAILABLE,
    FAQError,
    PlatformDecision,
    PlatformDirectoryError,
)
from aiops_diagnostics.dify.dify_debug_identity import (
    PRODUCTION_IDENTITY,
    DifyIdentity,
    debug_dify_identity,
)
from aiops_diagnostics.gateway import request_observation
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.caller_auth import SOURCE_KEY_HEADER
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.knowledge.knowledge_sources import KnowledgeSourceResolver
from aiops_diagnostics.publication.agent_lifecycle import AGENT_MANAGE_SCOPE
from aiops_diagnostics.publication.shortcut_lifecycle import SHORTCUT_MANAGE_SCOPE

_LOGGER = logging.getLogger("aiops.gateway")

STANDARD_ORDER_READ_SCOPE = "aiops:orders:read"
STANDARD_DIAGNOSIS_SCOPE = "aiops:diagnoses:write"
STANDARD_FAQ_SCOPE = "aiops:faq:read"


def request_language(
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> str:
    """Resolve the presentation language for one request (presentation only)."""
    return resolve_language(accept_language)


def localized_copy_language(
    request: Request,
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> str:
    """``request_language`` with request-path logging for deterministic copy endpoints."""
    language = resolve_language(accept_language)
    _LOGGER.info(
        "localized_copy_language path=%s accept_language=%r resolved=%s",
        request.url.path,
        accept_language[:160] if accept_language is not None else None,
        language,
    )
    return language


@dataclass(frozen=True, slots=True)
class GatewayDeps:
    """一次 Gateway 应用装配生成的全部 FastAPI 依赖项与知识源解析器。"""

    authenticated_caller: Callable[..., ScopeContext]
    authenticated_diagnosis_caller: Callable[..., ScopeContext]
    authenticated_faq_caller: Callable[..., ScopeContext]
    authenticated_agent_caller: Callable[..., ScopeContext]
    authenticated_shortcut_caller: Callable[..., ScopeContext]
    authenticated_shortcut_viewer: Callable[..., ScopeContext]
    faq_identity: Callable[..., tuple[ScopeContext, PlatformDecision]]
    assistant_identity: Callable[..., tuple[ScopeContext, PlatformDecision]]
    operator_diagnosis_identity: Callable[..., tuple[ScopeContext, PlatformDecision]]
    shortcut_identity: Callable[..., tuple[ScopeContext, PlatformDecision]]
    dify_shared_credential: Callable[..., DifyIdentity]
    dify_publish_credential: Callable[..., None]
    dify_workspace_credential: Callable[..., None]
    dify_trial_credential: Callable[..., None]
    libraries_for_tenant: Callable[[GatewayServerSettings, GatewayRuntime], Any]
    knowledge_sources: Callable[[GatewayServerSettings, GatewayStore], KnowledgeSourceResolver]


def install_exception_handlers(app: FastAPI, api: Any) -> None:
    """注册 Gateway 全局异常处理器，保持对外错误信封与缓存头一致。"""
    from aiops_diagnostics.consumer.consumer_tool_grants import ConsumerToolRefused
    from aiops_diagnostics.operator.operator_cpms import FEEDBACK_REFUSALS, OperatorRefused

    @app.exception_handler(ConsumerToolRefused)
    async def consumer_tool_refused_handler(request: Request, exc: ConsumerToolRefused):
        del request
        return JSONResponse(
            status_code=403,
            content={
                "error": {"code": exc.code, "message": "consumer tool request refused", "retryable": False}
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(OperatorRefused)
    async def operator_refused_handler(request: Request, exc: OperatorRefused):
        code = str(exc)
        if request.url.path.endswith("/completions"):
            safe_code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", code) else "OPERATOR_REFUSED"
            logging.getLogger("aiops.operator").warning("operator_completion_refused code=%s", safe_code)
        error: dict[str, Any] = {
            "code": code,
            "message": (
                "当前账号未分配可诊断场站，请联系公司管理员分配或切换账号"
                if code == "OPERATOR_SITE_SCOPE_REQUIRED"
                else "operator request unavailable"
            ),
            "retryable": False,
        }
        if request.url.path.startswith("/v1/internal/") and code in FEEDBACK_REFUSALS:
            error["feedback"] = True
            error["hint"] = FEEDBACK_REFUSALS[code]
        return JSONResponse(
            status_code=410 if code == "OPERATOR_RESULT_RETRACTED" else 403,
            content={"error": error},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(StandardAPIError)
    async def standard_api_error_handler(request: Request, exc: StandardAPIError) -> JSONResponse:
        request_observation.mark_refused(request, exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                }
            },
            headers=api._no_store_headers(request),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        del exc
        request_observation.mark_refused(request, request_observation.INVALID_REQUEST)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "request validation failed",
                    "retryable": False,
                }
            },
            headers=api._no_store_headers(request),
        )


def build_dependencies(context: Any, api: Any) -> GatewayDeps:
    """按 GatewayContext 构造全部鉴权、平台判定与 Dify 凭据依赖。"""

    def authenticated_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return api._authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_ORDER_READ_SCOPE,
        )

    def authenticated_diagnosis_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return api._authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_DIAGNOSIS_SCOPE,
        )

    def authenticated_faq_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return api._authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_FAQ_SCOPE,
        )

    def authenticated_agent_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return api._authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            source_key=source_key,
            required_scope=AGENT_MANAGE_SCOPE,
        )

    def authenticated_shortcut_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return api._authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            source_key=source_key,
            required_scope=SHORTCUT_MANAGE_SCOPE,
        )

    def authenticated_shortcut_viewer(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        source_key: Annotated[str | None, Header(alias=SOURCE_KEY_HEADER)] = None,
    ) -> ScopeContext:
        return api._authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            platform_entry=business_entry,
            source_key=source_key,
            required_scope=STANDARD_FAQ_SCOPE,
        )

    def platform_identity(caller_dependency: Any) -> Callable[..., tuple[ScopeContext, PlatformDecision]]:
        def dependency(
            caller: ScopeContext = Depends(caller_dependency),  # noqa: B008
            business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
        ) -> tuple[ScopeContext, PlatformDecision]:
            try:
                decision = context.platform_resolver.resolve(caller, business_entry)
            except FAQError as exc:
                raise api._platform_error(exc) from exc
            except PlatformDirectoryError as exc:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    PLATFORM_UNAVAILABLE,
                    "platform identity unavailable",
                    retryable=True,
                ) from exc
            return caller, decision

        return dependency

    faq_identity = platform_identity(authenticated_faq_caller)
    assistant_identity = platform_identity(authenticated_diagnosis_caller)
    operator_diagnosis_identity = platform_identity(authenticated_diagnosis_caller)
    shortcut_identity = platform_identity(authenticated_shortcut_viewer)

    def dify_shared_credential(
        authorization: Annotated[str | None, Header()] = None,
    ) -> DifyIdentity:
        configured = context.settings.dify_knowledge_api_key
        presented = (authorization or "").removeprefix("Bearer ").strip()
        bearer = authorization and authorization.startswith("Bearer ")
        matches_production = (
            bool(configured) and bearer and secrets.compare_digest(configured.encode(), presented.encode())
        )
        matches_debug = (
            bool(context.settings.dify_debug_api_key)
            and bearer
            and secrets.compare_digest(context.settings.dify_debug_api_key.encode(), presented.encode())
        )
        if matches_debug:
            return debug_dify_identity(context.settings)
        if matches_production:
            return PRODUCTION_IDENTITY
        from aiops_diagnostics.knowledge import knowledge_delegation

        if bearer and presented.startswith(knowledge_delegation.PREFIX):
            try:
                scope = knowledge_delegation.authorize(context.settings, presented)
            except ValueError as exc:
                raise StandardAPIError(403, "DIFY_CREDENTIAL_REJECTED", "credential rejected") from exc
            return DifyIdentity(name="delegated", delegated_scope=scope)
        if not configured:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_RETRIEVAL_UNAVAILABLE", "route not found")
        if not bearer:
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "DIFY_CREDENTIAL_REQUIRED", "bearer credential required"
            )
        from aiops_diagnostics.dify import candidate_knowledge

        if presented.startswith(candidate_knowledge.PREFIX):
            try:
                scope = candidate_knowledge.parse_token(context.settings.internal_token_secret, presented)
            except ValueError as exc:
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected"
                ) from exc
            return DifyIdentity(name="candidate", candidate_scope=scope)
        raise StandardAPIError(status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected")

    def dify_publish_credential(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        configured = context.settings.dify_publish_api_key
        if not configured:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_PUBLISH_UNAVAILABLE", "route not found")
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "DIFY_CREDENTIAL_REQUIRED", "bearer credential required"
            )
        presented = authorization.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(configured.encode("utf-8"), presented.encode("utf-8")):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected"
            )

    def dify_workspace_credential(
        token: Annotated[str | None, Header(alias=INTERNAL_TOKEN_HEADER)] = None,
        timestamp: Annotated[str | None, Header(alias=TIMESTAMP_HEADER)] = None,
    ) -> None:
        configured = context.settings.internal_token_secret
        if not configured or not context.settings.environment:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_WORKSPACE_API_UNAVAILABLE", "route not found"
            )
        if not validate_internal_token(
            token,
            timestamp,
            configured,
            expire_seconds=context.settings.internal_token_expire_seconds,
        ):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "INTERNAL_TOKEN_REQUIRED", "internal token required"
            )

    def dify_trial_credential(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        configured = context.settings.dify_trial_api_key
        if not configured:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "DIFY_TRIAL_UNAVAILABLE", "route not found")
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "DIFY_CREDENTIAL_REQUIRED", "bearer credential required"
            )
        presented = authorization.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(configured.encode("utf-8"), presented.encode("utf-8")):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "DIFY_CREDENTIAL_REJECTED", "credential rejected"
            )

    def libraries_for_tenant(settings: GatewayServerSettings, runtime: GatewayRuntime) -> Any:
        del settings

        def fetch(tenant_id: str) -> tuple[str, ...] | None:
            client = runtime.kb_search_client
            if client is None:
                return None
            try:
                owned = client.for_tenant(tenant_id).list_knowledge_bases()
            except Exception:  # noqa: BLE001 - upstream down: no auto source this request
                return None
            return tuple(str(item["knowledge_base_id"]) for item in owned if item.get("knowledge_base_id"))

        return fetch

    def knowledge_sources(settings: GatewayServerSettings, store: GatewayStore) -> KnowledgeSourceResolver:
        client = context.runtime.kb_search_client
        auto_tenants: list[str] = []
        if settings.dify_knowledge_autoregister and client is not None:
            try:
                auto_tenants = [
                    str(row["tenant_id"]) for row in store.list_ragflow_tenants(settings.environment)
                ]
            except Exception:  # noqa: BLE001 - an unreadable table is "none onboarded"
                auto_tenants = []
        return KnowledgeSourceResolver.from_parts(
            env_bindings=settings.dify_knowledge_bindings,
            rows=store.list_knowledge_id_bindings(),
            auto_tenants=auto_tenants,
        )

    return GatewayDeps(
        authenticated_caller=authenticated_caller,
        authenticated_diagnosis_caller=authenticated_diagnosis_caller,
        authenticated_faq_caller=authenticated_faq_caller,
        authenticated_agent_caller=authenticated_agent_caller,
        authenticated_shortcut_caller=authenticated_shortcut_caller,
        authenticated_shortcut_viewer=authenticated_shortcut_viewer,
        faq_identity=faq_identity,
        assistant_identity=assistant_identity,
        operator_diagnosis_identity=operator_diagnosis_identity,
        shortcut_identity=shortcut_identity,
        dify_shared_credential=dify_shared_credential,
        dify_publish_credential=dify_publish_credential,
        dify_workspace_credential=dify_workspace_credential,
        dify_trial_credential=dify_trial_credential,
        libraries_for_tenant=libraries_for_tenant,
        knowledge_sources=knowledge_sources,
    )
