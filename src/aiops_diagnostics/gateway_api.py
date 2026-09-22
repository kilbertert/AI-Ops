from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics import __version__
from aiops_diagnostics.agent_lifecycle import (
    AGENT_MANAGE_SCOPE,
    AgentConfig,
    AgentConflict,
    AgentError,
    AgentForbidden,
    AgentManager,
    AgentNotFound,
    AgentStore,
    allowed_models_from_settings,
)
from aiops_diagnostics.answer_language import (
    answer_chinese_leak,
    record_answer_language_fallback,
)
from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_CONFIG_MISSING,
    CALLER_AUTH_FORBIDDEN,
    CallerAuthError,
    CallerContextResolver,
    DisabledCallerResolver,
    DisabledOrderAuthorizer,
    IntrospectionCallerResolver,
    IntrospectionSettings,
    OrderAuthorizer,
    ScopedOrderAuthorizer,
    UpmsCallerResolver,
)
from aiops_diagnostics.codex_runtime import AgentRuntimeError
from aiops_diagnostics.config import Settings
from aiops_diagnostics.conversation_store import (
    ConversationBusy,
    ConversationError,
    ConversationStore,
)
from aiops_diagnostics.faq import (
    FAQ_NOT_FOUND,
    PLATFORM_AMBIGUOUS,
    PLATFORM_FORBIDDEN,
    PLATFORM_UNAVAILABLE,
    FAQCatalog,
    FAQError,
    MySQLPlatformDirectory,
    PlatformDirectoryError,
    PlatformIdentityResolver,
)
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime, close_gateway_runtime
from aiops_diagnostics.gateway_store import (
    ACTIVE_DIAGNOSIS_STATUSES,
    TERMINAL_DIAGNOSIS_STATUSES,
    TERMINAL_RUN_STATUSES,
    AuthenticationError,
    EnrollmentError,
    GatewayDevice,
    GatewayStore,
    RunNotFoundError,
)
from aiops_diagnostics.i18n import (
    DEFAULT_LANGUAGE,
    QA_FALLBACK_MESSAGES,
    clarification_message,
    resolve_language,
)
from aiops_diagnostics.metrics_store import MetricsValidationError
from aiops_diagnostics.order_visibility import DeviceTenantError
from aiops_diagnostics.scope_context import ScopeContext, ScopeError
from aiops_diagnostics.shortcut_lifecycle import (
    SHORTCUT_MANAGE_SCOPE,
    TENANT_SCOPE,
    ShortcutConflict,
    ShortcutError,
    ShortcutForbidden,
    ShortcutManager,
    ShortcutNotFound,
    ShortcutStore,
)
from aiops_diagnostics.sources import SourceError
from aiops_diagnostics.third_session_auth import RedisThirdSessionResolver, ThirdSessionSettings

STANDARD_ORDER_READ_SCOPE = "aiops:orders:read"
STANDARD_DIAGNOSIS_SCOPE = "aiops:diagnoses:write"
STANDARD_FAQ_SCOPE = "aiops:faq:read"
SAFE_ORDER_NO = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=20, max_length=256)
    device_name: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=128)


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem: str = Field(min_length=1, max_length=4000)
    order_no: str | None = Field(default=None, max_length=128)
    # Deliberately no character pattern: the tenant a request names is judged by
    # the shared entry rule (``resolve_device_tenant``), which normalizes both
    # sides, treats a blank one as absent, and refuses a genuinely different one.
    # A pattern here would be a second tenant decision at the transport layer —
    # the duplication this boundary exists to remove — and would turn a padded
    # identifier that normalizes onto the enrolled tenant into a 422 instead of
    # the run it is entitled to. Only the length bound stays, as for every other
    # bounded identifier on this surface (#331).
    tenant_id: str | None = Field(default=None, max_length=128)
    key_slot: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=64)
    fixture_name: str | None = Field(default=None, max_length=128)


class HealthReportJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class AssistantQuestionRequest(BaseModel):
    """Free-text question for the unified assistant entry point.

    ``order_no`` is OPTIONAL here — unlike the order diagnosis endpoint. When
    absent, the classifier routes to a generic (zero-order) answer or a FAQ
    short-circuit. When present, it is honored as an explicit order diagnosis.
    ``conversation_id`` (T4/#172) optionally binds the question to a
    conversation: its confirmed ``active_order`` lets follow-up order
    questions omit the order number (ownership re-verified every turn), and
    the turn is recorded in the conversation history.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    order_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    conversation_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=64,
        pattern=r"^conv_[A-Za-z0-9]{1,57}$",
    )
    shortcut_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$",
    )


class StandardDiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    question: str = Field(min_length=1, max_length=4000)
    indicator_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )


class FAQAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z]+\.[a-z0-9-]+\.q[0-9]{3}$")


class AgentConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_type: str = Field(min_length=1, max_length=32)
    prompt: str = Field(min_length=1, max_length=8000)
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=20)
    model: str = Field(min_length=1, max_length=128)
    output_contract: str = Field(min_length=1, max_length=64)
    opening_questions: list[str] = Field(default_factory=list, max_length=20)
    quick_commands: list[str] = Field(default_factory=list, max_length=20)

    def to_config(self) -> AgentConfig:
        return AgentConfig(
            agent_type=self.agent_type,
            prompt=self.prompt,
            knowledge_base_ids=tuple(self.knowledge_base_ids),
            model=self.model,
            output_contract=self.output_contract,
            opening_questions=tuple(self.opening_questions),
            quick_commands=tuple(self.quick_commands),
        )


class AgentCreateRequest(AgentConfigRequest):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)


class AgentUpdateRequest(AgentCreateRequest):
    expected_revision: int = Field(ge=1)


class AgentActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class AgentDebugRunRequest(BaseModel):
    """One draft debug turn (T5/#171): question in, blocks preview out."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)


class ConversationCreateRequest(BaseModel):
    """Create a conversation bound to caller, entry, and agent version (T4/#172).

    Defined at module level (not inside create_gateway_app) so FastAPI sees a
    real BaseModel annotation under `from __future__ import annotations`.
    """

    model_config = ConfigDict(extra="forbid")

    agent_version_key: str = Field(
        min_length=6,
        max_length=80,
        pattern=r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$",
    )


class ShortcutFieldsRequest(BaseModel):
    """Shortcut draft fields shared by create and update (#230)."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=32)
    requires_order: bool = False
    sort_order: int = Field(default=100, ge=0, le=9999)
    labels: dict[str, str] = Field(min_length=1, max_length=6)
    descriptions: dict[str, str] = Field(default_factory=dict, max_length=6)
    question_templates: dict[str, str] = Field(default_factory=dict, max_length=6)
    target_agent_version: str | None = Field(
        default=None,
        min_length=6,
        max_length=80,
        pattern=r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$",
    )
    jump_path: str | None = Field(
        default=None,
        max_length=512,
        # Must be a path-absolute route, not a network-path reference. Per RFC
        # 3986 §4.2 a leading "//" starts an authority, so "//evil.com" is a
        # cross-host reference — a classic open-redirect vector once a client
        # hands it to its navigator. This form is used instead of a "(?!/)"
        # lookahead because Pydantic v2 compiles patterns with the Rust regex
        # crate, which does not support lookaround at all (it fails at import).
        # It accepts exactly "/" plus a non-"/" start plus a non-space tail.
        #
        # Raw whitespace is also rejected: it cannot appear in a URL path (it
        # is "%20" once encoded), and without \S a trailing space or newline
        # would pass here and then be silently normalized away by the
        # lifecycle validator, leaving the two layers disagreeing.
        pattern=r"^/(?:[^/]\S*)?$",
    )


class ShortcutCreateRequest(ShortcutFieldsRequest):
    business_entry: str = Field(min_length=1, max_length=32, pattern=r"^(consumer|operator)$")
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class ShortcutUpdateRequest(ShortcutFieldsRequest):
    expected_revision: int = Field(ge=1)


class ShortcutActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ShortcutRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    version_no: int = Field(ge=1)


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


class StandardAPIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


def request_language(
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
) -> str:
    """Resolve the presentation language for one request (L1/#201).

    A separate dependency instead of widening the identity tuple: language is
    orthogonal to authentication and platform resolution, and conversation
    endpoints share ``assistant_identity`` without needing it. Presentation
    only — never part of auth, routing, or the incident manifest.
    """
    return resolve_language(accept_language)


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
    selected_store = store or GatewayStore(selected_settings.database_file)
    # Restart recovery, before this app can serve anything: a question the
    # previous process left `queued`/`running` is converged to a terminal state
    # here instead of hanging until its deadline (T3/#356). Deliberately not in
    # ``GatewayStore.__init__`` — short-lived CLI commands (`aiops-gateway
    # devices`) open the same database and must not end live work.
    selected_store.recover_assistant_questions()
    selected_runtime = runtime or GatewayRuntime.from_settings(selected_store, selected_settings)
    selected_resolver = caller_resolver or _caller_resolver(selected_settings)
    diagnostic_settings = getattr(selected_runtime, "diagnostic_settings", None)
    selected_authorizer = order_authorizer or (
        ScopedOrderAuthorizer(diagnostic_settings)
        if diagnostic_settings is not None
        else DisabledOrderAuthorizer()
    )
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
        configured_models = allowed_models_from_settings(diagnostic_settings)
        knowledge_resolver = None
        if selected_settings.kb_service_base_url:
            # Real publish validation (T5/#171): bindings must resolve against
            # live kb-service. Without a kb URL the manager keeps the
            # fail-closed default (bindings never publish).
            from aiops_diagnostics.agent_debug import KbBindingResolver, KbServiceKnowledgeClient

            knowledge_resolver = KbBindingResolver(
                KbServiceKnowledgeClient(
                    selected_settings.kb_service_base_url,
                    tenant_id="aiops",  # rebound per publish by the manager caller's tenant
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

    @app.exception_handler(StandardAPIError)
    async def standard_api_error_handler(_: Request, exc: StandardAPIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        del exc
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "request validation failed",
                    "retryable": False,
                }
            },
        )

    def authenticated_device(
        authorization: Annotated[str | None, Header()] = None,
    ) -> GatewayDevice:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="device token required")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            return context.store.authenticate_device(token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    def authenticated_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
    ) -> ScopeContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "ACCESS_TOKEN_REQUIRED",
                "access token required",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if token.startswith("aops_"):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "INVALID_ACCESS_TOKEN",
                "access token validation failed",
            )
        try:
            if x_third_session is None:
                return context.caller_resolver.resolve(token, required_scope=STANDARD_ORDER_READ_SCOPE)
            return context.caller_resolver.resolve(
                token, required_scope=STANDARD_ORDER_READ_SCOPE, third_session=x_third_session
            )
        except CallerAuthError as exc:
            if exc.code == CALLER_AUTH_FORBIDDEN:
                status_code = status.HTTP_403_FORBIDDEN
                code = "INSUFFICIENT_SCOPE"
            elif exc.code == CALLER_AUTH_CONFIG_MISSING:
                status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                code = "ACCESS_TOKEN_VALIDATION_UNAVAILABLE"
            else:
                status_code = (
                    status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_401_UNAUTHORIZED
                )
                code = "ACCESS_TOKEN_VALIDATION_UNAVAILABLE" if exc.retryable else "INVALID_ACCESS_TOKEN"
            raise StandardAPIError(
                status_code,
                code,
                "access token validation failed",
                retryable=exc.retryable,
            ) from exc

    def authenticated_diagnosis_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
    ) -> ScopeContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "ACCESS_TOKEN_REQUIRED",
                "access token required",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if token.startswith("aops_"):
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED,
                "INVALID_ACCESS_TOKEN",
                "access token validation failed",
            )
        try:
            if x_third_session is None:
                return context.caller_resolver.resolve(token, required_scope=STANDARD_DIAGNOSIS_SCOPE)
            return context.caller_resolver.resolve(
                token, required_scope=STANDARD_DIAGNOSIS_SCOPE, third_session=x_third_session
            )
        except CallerAuthError as exc:
            if exc.code == CALLER_AUTH_FORBIDDEN:
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN,
                    "INSUFFICIENT_SCOPE",
                    "access token validation failed",
                ) from exc
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_401_UNAUTHORIZED,
                "ACCESS_TOKEN_VALIDATION_UNAVAILABLE" if exc.retryable else "INVALID_ACCESS_TOKEN",
                "access token validation failed",
                retryable=exc.retryable,
            ) from exc

    def authenticated_faq_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            required_scope=STANDARD_FAQ_SCOPE,
        )

    def authenticated_agent_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            required_scope=AGENT_MANAGE_SCOPE,
        )

    def authenticated_shortcut_caller(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            required_scope=SHORTCUT_MANAGE_SCOPE,
        )

    def authenticated_shortcut_viewer(
        authorization: Annotated[str | None, Header()] = None,
        x_third_session: Annotated[str | None, Header()] = None,
    ) -> ScopeContext:
        return _authenticate_caller(
            context.caller_resolver,
            authorization,
            x_third_session,
            required_scope=STANDARD_FAQ_SCOPE,
        )

    def faq_identity(
        caller: ScopeContext = Depends(authenticated_faq_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> tuple[ScopeContext, Any]:
        try:
            decision = context.platform_resolver.resolve(caller, business_entry)
        except FAQError as exc:
            status_code = {
                PLATFORM_AMBIGUOUS: status.HTTP_409_CONFLICT,
                PLATFORM_FORBIDDEN: status.HTTP_403_FORBIDDEN,
                PLATFORM_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE)
            raise StandardAPIError(
                status_code,
                exc.code,
                str(exc),
                retryable=exc.code == PLATFORM_UNAVAILABLE,
            ) from exc
        except PlatformDirectoryError as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                PLATFORM_UNAVAILABLE,
                "platform identity unavailable",
                retryable=True,
            ) from exc
        return caller, decision

    def assistant_identity(
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> tuple[ScopeContext, Any]:
        """Assistant endpoint identity: diagnoses:write callers + platform decision.

        The assistant endpoint can trigger a full order diagnosis (a write
        action) on its order branch, so it must authenticate with the same
        scope as /v1/standard/diagnoses (aiops:diagnoses:write) — NOT the
        weaker faq:read used by the pure-FAQ endpoints. The authenticated
        caller then resolves the platform content domain for the FAQ branch.
        """
        try:
            decision = context.platform_resolver.resolve(caller, business_entry)
        except FAQError as exc:
            status_code = {
                PLATFORM_AMBIGUOUS: status.HTTP_409_CONFLICT,
                PLATFORM_FORBIDDEN: status.HTTP_403_FORBIDDEN,
                PLATFORM_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE)
            raise StandardAPIError(
                status_code,
                exc.code,
                str(exc),
                retryable=exc.code == PLATFORM_UNAVAILABLE,
            ) from exc
        except PlatformDirectoryError as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                PLATFORM_UNAVAILABLE,
                "platform identity unavailable",
                retryable=True,
            ) from exc
        return caller, decision

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "aiops-gateway",
            "version": __version__,
            "api_version": "v1",
            "platform": platform.system().lower(),
            "business_mutations": "disabled",
        }

    @app.get("/v1/faq/recommendations")
    def faq_recommendations(
        identity: tuple[ScopeContext, Any] = Depends(faq_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ):
        _, decision = identity
        return {
            **decision.public(),
            "language": language,
            "faq_version": context.faq_catalog.version,
            "recommendations": context.faq_catalog.recommendations(decision.platform, language),
        }

    @app.get("/v1/faq/catalog")
    def faq_catalog(
        identity: tuple[ScopeContext, Any] = Depends(faq_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ):
        _, decision = identity
        return {
            **decision.public(),
            "language": language,
            "faq_version": context.faq_catalog.version,
            "entries": context.faq_catalog.catalog(decision.platform, language),
        }

    @app.post("/v1/faq/answer")
    def faq_answer(
        payload: FAQAnswerRequest,
        identity: tuple[ScopeContext, Any] = Depends(faq_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ):
        _, decision = identity
        try:
            answer = context.faq_catalog.answer(decision.platform, payload.question_id, language)
        except FAQError as exc:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, FAQ_NOT_FOUND, "FAQ question was not found"
            ) from exc
        return {
            **decision.public(),
            "language": language,
            "faq_version": context.faq_catalog.version,
            "question_id": answer["question_id"],
            "question": answer["question"],
            "answer": answer["answer"],
            "format": answer["format"],
        }

    @app.post("/v1/assistant/questions")
    def assistant_questions(
        payload: AssistantQuestionRequest,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Unified assistant entry point with optional order_no (T2/#152).

        Classifier routes:
          - explicit order_no        → type=diagnosis (defer to the order
            diagnosis semantics: authorize then start_standard_diagnosis)
          - FAQ keyword short-circuit → type=faq (sync, deterministic)
          - else                     → type=qa (generic zero-order answer);
            this ticket returns a queued placeholder that the T3 ticket fills
            with a real zero-order Agent job.
        """
        caller, decision = identity

        # Conversation binding (T4/#172): resolve BEFORE routing so every
        # branch can consult the confirmed active_order. A conversation that
        # is missing, expired, or belongs to another user/tenant/entry is a
        # uniform 404 — no existence leak.
        conversation: dict[str, Any] | None = None
        if payload.conversation_id:
            conversation = _resolve_conversation(context, caller, decision, payload.conversation_id)

        # Two server-side guards over the clicked shortcut. The listing
        # metadata is a UI hint; these guards are the boundary that keeps a
        # clicked action on its own path instead of leaking into generic QA.
        if payload.shortcut_code:
            try:
                shortcut = context.shortcut_manager.store.find_effective_by_code(
                    caller.effective_tenant_id, str(decision.platform), payload.shortcut_code
                )
            except ShortcutError:
                shortcut = None
            if shortcut is not None and shortcut.status == "published":
                # (a) A jump action is not the assistant entry's business: the
                # client was supposed to navigate. If it arrives here anyway,
                # answer plainly and create NO job. Checked before the order
                # guard and regardless of order context, because a jump action
                # must never produce an answer or a diagnosis.
                if shortcut.jump_path:
                    _record_route_metric(context, caller, route_type="clarification", outcome="completed")
                    return {
                        **decision.public(),
                        "type": "clarification",
                        "language": language,
                        "question": payload.question,
                        "missing_fields": [],
                        "message": clarification_message(language, "wrong_entry"),
                    }

                # (b) A clicked order-bound shortcut must not silently fall
                # through to generic QA when the frontend omitted the order
                # context.
                #
                # The order may arrive either as the explicit ``order_no``
                # field OR embedded in the question text (the frontend sends
                # the order picker result inside the sentence, e.g. "帮我检测
                # （2099…）这个订单的充电异常"). Only when NEITHER carries a
                # candidate do we ask for one — otherwise this guard would
                # shadow Route 1b and reject a perfectly diagnosable request.
                if (
                    shortcut.requires_order
                    and not payload.order_no
                    and _extract_order_no(payload.question) is None
                ):
                    _record_route_metric(context, caller, route_type="clarification", outcome="completed")
                    return {
                        **decision.public(),
                        "type": "clarification",
                        "language": language,
                        "question": payload.question,
                        "missing_fields": ["order_no"],
                        "message": clarification_message(language, "order_no"),
                    }

        # Route 1: explicit order → diagnosis semantics.
        if payload.order_no:
            try:
                allowed = context.order_authorizer.can_access(caller, payload.order_no)
            except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "ORDER_AUTHORIZATION_UNAVAILABLE",
                    "order authorization unavailable",
                    retryable=True,
                ) from exc
            if not allowed:
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND,
                    "ORDER_NOT_FOUND",
                    "order not found",
                )
            try:
                diagnosis = context.runtime.start_standard_diagnosis(
                    caller,
                    payload.order_no,
                    payload.question,
                    None,
                    language=language,
                )
            except (ValueError, RuntimeError) as exc:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "DIAGNOSIS_UNAVAILABLE",
                    "diagnosis unavailable",
                    retryable=True,
                ) from exc
            base = _standard_diagnosis_response(diagnosis)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={**base, "type": "diagnosis", "language": language},
            )

        # Route 1b: text-embedded order number → diagnosis if authorizable.
        # The caller did not pass order_no explicitly, but the question text
        # contains a plausible order id. Verify ownership first; only then
        # route to order diagnosis. If ownership fails, fall through to the
        # generic answer — this is NOT a hard 404, because the caller did not
        # assert ownership of a specific order (T4/#154).
        if not payload.order_no:
            embedded = _extract_order_no(payload.question)
            if embedded:
                try:
                    owns = context.order_authorizer.can_access(caller, embedded)
                except (CallerAuthError, ScopeError, SourceError, ValueError):
                    owns = False
                if owns:
                    turn_no = _begin_conversation_turn(
                        context, caller, conversation, "diagnosis", payload.question
                    )
                    try:
                        diagnosis = context.runtime.start_standard_diagnosis(
                            caller,
                            embedded,
                            payload.question,
                            None,
                            language=language,
                        )
                    except (ValueError, RuntimeError) as exc:
                        _release_conversation_turn(context, conversation, turn_no)
                        raise StandardAPIError(
                            status.HTTP_503_SERVICE_UNAVAILABLE,
                            "DIAGNOSIS_UNAVAILABLE",
                            "diagnosis unavailable",
                            retryable=True,
                        ) from exc
                    if conversation is not None and embedded == conversation.get("active_order_no"):
                        _keep_conversation_turn(context, conversation, turn_no)
                    else:
                        _release_conversation_turn(context, conversation, turn_no)
                    base = _standard_diagnosis_response(diagnosis)
                    return JSONResponse(
                        status_code=status.HTTP_202_ACCEPTED,
                        content={
                            **base,
                            "type": "diagnosis",
                            "language": language,
                            "order_no_extracted": embedded,
                            **({"conversation_id": conversation["conversation_id"]} if conversation else {}),
                        },
                    )

        # Route 1c (T4/#172): order question against the conversation's
        # CONFIRMED active order — the follow-up that may omit the order
        # number. Only fires when the question clearly involves that order
        # (the same explicitness bar as Route 1b); ownership is re-verified
        # EVERY turn because permissions change between requests. On any
        # failure the question falls through to qa+RAG — never a hard 404,
        # since the caller did not name an order this turn.
        if not payload.order_no and conversation is not None:
            active_order = conversation.get("active_order_no")
            if active_order and _question_involves_active_order(payload.question):
                try:
                    still_owned = context.order_authorizer.can_access(caller, active_order)
                except (CallerAuthError, ScopeError, SourceError, ValueError):
                    still_owned = False
                if still_owned:
                    turn_no = _begin_conversation_turn(
                        context, caller, conversation, "diagnosis", payload.question
                    )
                    try:
                        diagnosis = context.runtime.start_standard_diagnosis(
                            caller,
                            active_order,
                            payload.question,
                            None,
                            language=language,
                        )
                    except (ValueError, RuntimeError) as exc:
                        _release_conversation_turn(context, conversation, turn_no)
                        raise StandardAPIError(
                            status.HTTP_503_SERVICE_UNAVAILABLE,
                            "DIAGNOSIS_UNAVAILABLE",
                            "diagnosis unavailable",
                            retryable=True,
                        ) from exc
                    _keep_conversation_turn(context, conversation, turn_no)
                    base = _standard_diagnosis_response(diagnosis)
                    return JSONResponse(
                        status_code=status.HTTP_202_ACCEPTED,
                        content={
                            **base,
                            "type": "diagnosis",
                            "language": language,
                            "order_no_from_context": active_order,
                            "conversation_id": conversation["conversation_id"],
                        },
                    )
                # Ownership lost since confirmation: clear the stale binding
                # and answer as a plain knowledge question (qa+RAG).
                with contextlib.suppress(ConversationError):
                    context.conversation_store.set_active_order(
                        conversation["conversation_id"], caller.scope_fingerprint, None
                    )
                conversation = {**conversation, "active_order_no": None}

        # Route 2: promotional routing (#231) — BEFORE the FAQ short-circuit.
        # PRD #227 orders FAQ above a passive shortcut context, but a user who
        # CLICKED a product-entry button or explicitly asks for cases/solutions
        # must never land in the customer-service FAQ (#231 acceptance:
        # 案例不进入 FAQ). Only explicit promotional signals take this branch.
        promo_target, promo_intent = _promo_route(context, caller, decision, payload)
        if promo_intent is not None:
            return _start_promo_qa(
                context, caller, conversation, payload, language, promo_target, promo_intent
            )

        # Route 2b: FAQ short-circuit (deterministic, zero-order, zero-model).
        faq_id = _faq_hit_by_keywords(decision.platform, context.faq_catalog, payload.question)
        if faq_id is not None:
            answer = context.faq_catalog.answer(decision.platform, faq_id, language)
            _record_route_metric(context, caller, route_type="faq", outcome="completed")
            return {
                **decision.public(),
                "type": "faq",
                "language": language,
                "faq_version": context.faq_catalog.version,
                "question_id": answer["question_id"],
                "question": answer["question"],
                "answer": answer["answer"],
                "format": answer["format"],
            }

        # Do not send an order/billing dispute without an order context into
        # generic QA; ask for the missing business identifier synchronously.
        risk_key = (
            _missing_order_context_key(payload.question)
            if conversation is None and _extract_order_no(payload.question) is None
            else None
        )
        if risk_key is not None:
            _record_route_metric(context, caller, route_type="clarification", outcome="completed")
            return {
                **decision.public(),
                "type": "clarification",
                "language": language,
                "question": payload.question,
                "missing_fields": ["order_no"],
                "message": clarification_message(language, risk_key),
            }

        classifier = getattr(context.runtime, "classify_lightweight", None)
        if classifier is not None:
            try:
                classified = classifier(payload.question, language=language)
            except (ValueError, RuntimeError):
                classified = None
            if classified and classified.get("intent") == "casual" and classified.get("answer"):
                casual = str(classified["answer"])
                leak = answer_chinese_leak(casual, language)
                if leak:
                    # Same contract as the blocks surfaces: an answer that
                    # leaked Chinese is withheld, not delivered, and the
                    # fallback is written in the requested language.
                    record_answer_language_fallback(language=language, leaked=leak, surface="casual")
                    casual = QA_FALLBACK_MESSAGES.get(language, QA_FALLBACK_MESSAGES["zh"])["unavailable"]
                return {
                    **decision.public(),
                    "type": "qa",
                    "language": language,
                    "question": payload.question,
                    "status": "completed",
                    "result": {"text": casual, "reminder": True},
                    "error": None,
                }
            if classified and classified.get("risk") == "high" and classified.get("confidence") != "high":
                return {
                    **decision.public(),
                    "type": "clarification",
                    "language": language,
                    "question": payload.question,
                    "missing_fields": ["context"],
                    "message": clarification_message(language, "context"),
                }
            # Classifier-returned promotional intent (#229 protocol): same
            # promotional route as the explicit cues, resolved against the
            # caller's own published shortcut rows.
            if classified and classified.get("intent") in {"case_exploration", "solution_discovery"}:
                promo_target, promo_intent = _promo_route(
                    context,
                    caller,
                    decision,
                    payload,
                    forced_intent=str(classified.get("intent")),
                )
                return _start_promo_qa(
                    context, caller, conversation, payload, language, promo_target, promo_intent
                )

        # Route 3: generic zero-order answer — start a real QA job (T3/#153).
        # With a conversation: claim its generation slot first (409 busy),
        # and record the turn so follow-ups see it in context.
        turn_no: int | None = None
        if conversation is not None:
            turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
        try:
            qa = context.runtime.start_assistant_qa(
                caller,
                payload.question,
                conversation=conversation if turn_no is not None else None,
                conversation_turn_no=turn_no,
                language=language,
            )
        except (ValueError, RuntimeError) as exc:
            _release_conversation_turn(context, conversation, turn_no)
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "QA_UNAVAILABLE",
                "general answer unavailable",
                retryable=True,
            ) from exc
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "type": "qa",
                "language": language,
                "qa_id": qa["qa_id"],
                "question": qa["question"],
                "status": qa["status"],
                "retry_after_ms": 1000,
                "result": qa.get("result"),
                "error": None,
                **(
                    {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                    if conversation
                    else {}
                ),
            },
        )

    @app.get("/v1/assistant/questions/{qa_id}")
    def get_assistant_question(
        qa_id: str,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, _ = identity
        try:
            qa = context.runtime.get_assistant_qa(caller, qa_id)
        except (ValueError, RuntimeError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "QA_UNAVAILABLE",
                "general answer unavailable",
                retryable=True,
            ) from exc
        if qa is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "QA_NOT_FOUND",
                "assistant question not found",
            )
        return _assistant_question_response(qa, language)

    @app.post("/v1/assistant/questions/{qa_id}/cancel")
    def cancel_assistant_question(
        qa_id: str,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Stop one in-flight general question (#357).

        POST rather than DELETE: the job row survives as `cancelled` (audit and
        history), and DELETE would promise the resource is gone. The response
        carries the job's terminal state, so the caller does not have to poll
        once more to learn the outcome — and a repeated cancel, or a cancel of
        a job that just finished, answers that job's own current state instead
        of an error, because the stop button is pressed under flaky networks.
        """
        caller, _ = identity
        try:
            qa = context.runtime.cancel_assistant_qa(caller, qa_id)
        except (ValueError, RuntimeError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "QA_UNAVAILABLE",
                "general answer unavailable",
                retryable=True,
            ) from exc
        if qa is None:
            # Missing and out-of-scope are the same answer: cancelling must not
            # become a way to probe which qa_ids exist.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "QA_NOT_FOUND",
                "assistant question not found",
            )
        return _assistant_question_response(qa, language)

    @app.get("/v1/assistant/questions")
    def list_assistant_questions(
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List this caller's general-question history (T5/#155).

        Kept separate from /v1/standard/diagnoses (order diagnostics): the
        QA history contains only zero-order answers, each typed `qa`.
        """
        caller, _ = identity
        try:
            questions = context.runtime.list_assistant_qa(caller, limit=limit)
        except (ValueError, RuntimeError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "QA_UNAVAILABLE",
                "general answer unavailable",
                retryable=True,
            ) from exc
        return {
            "type": "qa_list",
            "language": language,
            "count": len(questions),
            "questions": questions,
        }

    # ------------------------------------------------------------------
    # Conversations (T4/#172)
    # ------------------------------------------------------------------

    @app.get("/v1/media/{signed_id}")
    def get_media(
        signed_id: str,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> Response:
        """Serve a signed media resource (#168 media protocol, T3/#170 data plane).

        No Authorization dependency: browsers load media through <img>/<video>
        tags that cannot attach Bearer headers, so the short-lived HMAC in the
        URL is the credential. The runtime re-validates the grant's tenant /
        agent-version / KB-binding scope and its liveness on every hit. Media
        plane unconfigured or any scope mismatch is a uniform 404 with no body
        detail — no existence leak (#170 acceptance).
        """
        response = context.runtime.serve_media(signed_id, range_header=range_header)
        if response is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "MEDIA_NOT_FOUND",
                "media resource not found",
            )
        return Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.body,
        )

    @app.post("/v1/conversations")
    def create_conversation(
        payload: ConversationCreateRequest,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Create a conversation bound to this caller, entry, and agent."""
        caller, decision = identity
        conversation = context.conversation_store.create(
            scope_fingerprint=caller.scope_fingerprint,
            business_entry=str(decision.platform),
            agent_version_key=payload.agent_version_key,
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=_conversation_response(conversation),
        )

    @app.get("/v1/conversations")
    def list_conversations(
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        caller, _ = identity
        conversations = context.conversation_store.list(caller.scope_fingerprint, limit=limit)
        return {
            "type": "conversation_list",
            "count": len(conversations),
            "conversations": [_conversation_response(item) for item in conversations],
        }

    @app.get("/v1/conversations/{conversation_id}")
    def get_conversation(
        conversation_id: str,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Conversation detail including the full turn history (answers
        included only for finished turns — the caller re-polls job ids for
        in-flight ones)."""
        caller, decision = identity
        conversation = _require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            # Entry is part of the conversation binding (T4/#172): reading
            # from a different entry is a uniform 404.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        history = context.conversation_store.turns(conversation["conversation_id"], caller.scope_fingerprint)
        return {
            **_conversation_response(conversation),
            "turns": [
                {
                    "turn_no": item["turn_no"],
                    "kind": item["kind"],
                    "question": item["question"],
                    "answer": item["answer"],
                    "created_at": item["created_at"],
                }
                for item in history
            ],
        }

    @app.delete("/v1/conversations/{conversation_id}")
    def delete_conversation(
        conversation_id: str,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        caller, decision = identity
        conversation = _require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        context.conversation_store.delete(conversation_id, caller.scope_fingerprint)
        return {"type": "conversation_deleted", "conversation_id": conversation_id}

    @app.post("/v1/conversations/{conversation_id}/active-order")
    def set_conversation_active_order(
        conversation_id: str,
        order_no: str | None = None,
        body: dict[str, Any] | None = None,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """Bind (or clear) the conversation's confirmed active order.

        The order must be owned by the caller — ownership is re-verified HERE,
        every bind; chat text can never replace this check (#172).
        """
        caller, decision = identity
        conversation = _require_conversation(context, caller, conversation_id)
        if conversation.get("business_entry") != str(decision.platform):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CONVERSATION_NOT_FOUND",
                "conversation not found",
            )
        if body is None:
            body = {}
        candidate = body.get("order_no", order_no)
        if candidate is None:
            conversation = context.conversation_store.set_active_order(
                conversation_id, caller.scope_fingerprint, None
            )
            return _conversation_response(conversation)
        candidate = str(candidate)
        if not SAFE_ORDER_NO.fullmatch(candidate):
            raise StandardAPIError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "INVALID_REQUEST",
                "order_no is invalid",
            )
        try:
            allowed = context.order_authorizer.can_access(caller, candidate)
        except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            ) from exc
        if not allowed:
            # Unowned order: uniform 404, never an ownership oracle.
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        conversation = context.conversation_store.set_active_order(
            conversation_id, caller.scope_fingerprint, candidate
        )
        return _conversation_response(conversation)

    @app.get("/v1/orders/{order_no}/access")
    def order_access(
        order_no: str,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
    ) -> dict[str, Any]:
        if not SAFE_ORDER_NO.fullmatch(order_no):
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_ORDER_NO",
                "invalid order number",
            )
        try:
            allowed = context.order_authorizer.can_access(caller, order_no)
        except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            ) from exc
        if not allowed:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        return {
            "order_no": order_no,
            "accessible": True,
            "scope_fingerprint": caller.scope_fingerprint,
        }

    @app.post("/v1/health-report-jobs", status_code=status.HTTP_202_ACCEPTED)
    def create_health_report_job(
        payload: HealthReportJobRequest,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            allowed = context.order_authorizer.can_access(caller, payload.order_no)
        except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            ) from exc
        if not allowed:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        try:
            job = context.runtime.start_health_report(caller, payload.order_no)
        except (ValueError, RuntimeError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "REPORT_JOB_UNAVAILABLE",
                "health report job unavailable",
                retryable=True,
            ) from exc
        return _health_job_response(job)

    @app.get("/v1/health-report-jobs/{job_id}")
    def get_health_report_job(
        job_id: str,
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            job = context.runtime.get_health_report(caller, job_id)
        except (ValueError, RuntimeError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "REPORT_JOB_UNAVAILABLE",
                "health report job unavailable",
                retryable=True,
            ) from exc
        if job is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "REPORT_JOB_NOT_FOUND",
                "health report job not found",
            )
        return _health_job_response(job)

    @app.post("/v1/standard/diagnoses", status_code=status.HTTP_202_ACCEPTED)
    def create_standard_diagnosis(
        payload: StandardDiagnosisRequest,
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            allowed = context.order_authorizer.can_access(caller, payload.order_no)
        except (CallerAuthError, ScopeError, SourceError, ValueError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            ) from exc
        if not allowed:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "ORDER_NOT_FOUND",
                "order not found",
            )
        try:
            diagnosis = context.runtime.start_standard_diagnosis(
                caller,
                payload.order_no,
                payload.question,
                payload.indicator_code,
                language=language,
            )
        except (ValueError, RuntimeError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "DIAGNOSIS_UNAVAILABLE",
                "diagnosis unavailable",
                retryable=True,
            ) from exc
        return _standard_diagnosis_response(diagnosis)

    @app.get("/v1/standard/diagnoses/{diagnosis_id}")
    def get_standard_diagnosis(
        diagnosis_id: str,
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            diagnosis = context.runtime.get_standard_diagnosis(caller, diagnosis_id)
        except (ValueError, RuntimeError) as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "DIAGNOSIS_UNAVAILABLE",
                "diagnosis unavailable",
                retryable=True,
            ) from exc
        if diagnosis is None:
            # A qa_ id here means the caller created a general-question job
            # (POST /v1/assistant/questions returned type=qa) but is polling
            # the order-diagnosis endpoint. Point them at the right poll path
            # instead of a bare "not found", so the wrong-endpoint mistake is
            # self-explanatory (observed in real frontend integration).
            if diagnosis_id.startswith("qa_"):
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND,
                    "DIAGNOSIS_NOT_FOUND",
                    "this is a general-question job (type=qa); poll GET /v1/assistant/questions/"
                    + diagnosis_id
                    + " instead",
                )
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "DIAGNOSIS_NOT_FOUND",
                "diagnosis not found",
            )
        return _standard_diagnosis_response(diagnosis)

    @app.get("/v1/standard/diagnoses")
    def list_standard_diagnoses(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        caller: ScopeContext = Depends(authenticated_diagnosis_caller),  # noqa: B008
    ) -> dict[str, Any]:
        diagnoses = context.runtime.list_standard_diagnoses(caller, limit=limit)
        return {"diagnoses": diagnoses}

    def _agent_error(exc: AgentError) -> StandardAPIError:
        if isinstance(exc, AgentNotFound):
            return StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_NOT_FOUND", "agent not found")
        if isinstance(exc, AgentForbidden):
            return StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        if isinstance(exc, AgentConflict):
            return StandardAPIError(
                status.HTTP_409_CONFLICT, "AGENT_REVISION_CONFLICT", "agent revision conflict"
            )
        return StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, str(exc))

    @app.post("/v1/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(
        payload: AgentCreateRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.create(
                caller,
                name=payload.name,
                description=payload.description,
                config=payload.to_config(),
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.get("/v1/agents")
    def list_agents(
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agents = context.agent_manager.list(caller)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return {"agents": [agent.to_dict() for agent in agents]}

    @app.get("/v1/agents/{agent_id}")
    def get_agent(
        agent_id: str,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.get(caller, agent_id)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.put("/v1/agents/{agent_id}")
    def update_agent(
        agent_id: str,
        payload: AgentUpdateRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.update(
                caller,
                agent_id,
                expected_revision=payload.expected_revision,
                name=payload.name,
                description=payload.description,
                config=payload.to_config(),
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.post("/v1/agents/{agent_id}/publish")
    def publish_agent(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.agent_manager.publish(
                caller, agent_id, expected_revision=payload.expected_revision
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/agents/{agent_id}/draft")
    def fork_agent_draft(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.fork_draft(
                caller, agent_id, expected_revision=payload.expected_revision
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.post("/v1/agents/{agent_id}/disable")
    def disable_agent(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agent = context.agent_manager.disable(
                caller, agent_id, expected_revision=payload.expected_revision
            )
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return agent.to_dict()

    @app.delete("/v1/agents/{agent_id}")
    def delete_agent(
        agent_id: str,
        payload: AgentActionRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, bool]:
        try:
            context.agent_manager.delete(caller, agent_id, expected_revision=payload.expected_revision)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return {"deleted": True}

    @app.get("/v1/agents/{agent_id}/versions/{version_no}")
    def get_agent_version(
        agent_id: str,
        version_no: int,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.agent_manager.version(caller, agent_id, version_no)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/agents/{agent_id}/debug-run")
    def debug_run_agent(
        agent_id: str,
        payload: AgentDebugRunRequest,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Isolated draft preview (T5/#171): runs the draft config through the
        production customer-QA harness without touching conversations or
        orders. Only draft-config customer agents are debuggable."""
        from aiops_diagnostics.agent_lifecycle import EDIT_ROLES

        if not frozenset(getattr(caller, "roles", ())).intersection(EDIT_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            agent = context.agent_manager.get(caller, agent_id)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        if agent.status != "draft" or agent.config.agent_type != "customer":
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_DEBUG_STATE_INVALID",
                "only a draft customer agent can be debug-run",
            )
        runner = getattr(context.runtime, "run_agent_debug", None)
        if runner is None:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_DEBUG_UNAVAILABLE",
                "agent debug-run is not configured",
                retryable=True,
            )
        try:
            return runner(caller, agent_id, payload.question, language=language)
        except AgentRuntimeError as exc:
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "AGENT_DEBUG_FAILED",
                _debug_public_error(exc),
                retryable=True,
            ) from exc

    # ------------------------------------------------------------------
    # Product-entry shortcuts (#230)
    # ------------------------------------------------------------------

    def _shortcut_error(exc: ShortcutError) -> StandardAPIError:
        if isinstance(exc, ShortcutNotFound):
            return StandardAPIError(status.HTTP_404_NOT_FOUND, "SHORTCUT_NOT_FOUND", "shortcut not found")
        if isinstance(exc, ShortcutForbidden):
            return StandardAPIError(
                status.HTTP_403_FORBIDDEN, "SHORTCUT_FORBIDDEN", "shortcut access is not permitted"
            )
        if isinstance(exc, ShortcutConflict):
            return StandardAPIError(
                status.HTTP_409_CONFLICT, "SHORTCUT_REVISION_CONFLICT", "shortcut revision conflict"
            )
        return StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, str(exc))

    def shortcut_identity(
        caller: ScopeContext = Depends(authenticated_shortcut_viewer),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> tuple[ScopeContext, str]:
        """Shortcut listing identity: any assistant-scope caller + resolved entry.

        The listing renders the product home for every authenticated user, so
        it requires no admin role — but the entry comes from the SAME platform
        decision the assistant uses (never a client self-report), and tenant
        isolation is enforced inside the store by the caller's effective
        tenant.
        """
        try:
            decision = context.platform_resolver.resolve(caller, business_entry)
        except FAQError as exc:
            status_code = {
                PLATFORM_AMBIGUOUS: status.HTTP_409_CONFLICT,
                PLATFORM_FORBIDDEN: status.HTTP_403_FORBIDDEN,
                PLATFORM_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
            }.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE)
            raise StandardAPIError(
                status_code,
                exc.code,
                str(exc),
                retryable=exc.code == PLATFORM_UNAVAILABLE,
            ) from exc
        except PlatformDirectoryError as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                PLATFORM_UNAVAILABLE,
                "platform identity unavailable",
                retryable=True,
            ) from exc
        return caller, str(decision.platform)

    @app.get("/v1/shortcuts")
    def list_shortcuts(
        identity: tuple[ScopeContext, str] = Depends(shortcut_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """Published product-entry shortcuts for this caller's tenant+entry.

        Drafts are invisible, disabled rows are gone, other tenants/entries
        are unreachable. Each item carries the stable ``code`` the client
        wires behavior to (e.g. order picker for requires_order=True), plus
        copy localized in the request language (zh fallback).
        """
        caller, entry = identity
        try:
            shortcuts = context.shortcut_manager.list_effective(caller, business_entry=entry)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "shortcut_list",
            "language": language,
            "count": len(shortcuts),
            "shortcuts": [item.public(language) for item in shortcuts],
        }

    @app.post("/v1/shortcuts", status_code=status.HTTP_201_CREATED)
    def create_shortcut(
        payload: ShortcutCreateRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.create(caller, payload.model_dump(), scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.get("/v1/shortcut-admin/{business_entry}")
    def list_shortcut_admin(
        business_entry: str,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Management listing (all statuses, full multilingual fields)."""
        try:
            shortcuts = context.shortcut_manager.list(caller, business_entry=business_entry, scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {
            "type": "shortcut_admin_list",
            "count": len(shortcuts),
            "shortcuts": [s.to_dict() for s in shortcuts],
        }

    @app.get("/v1/shortcuts/{shortcut_id}/versions/{version_no}")
    def get_shortcut_version(
        shortcut_id: str,
        version_no: int,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.version(caller, shortcut_id, version_no, scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.put("/v1/shortcuts/{shortcut_id}")
    def update_shortcut(
        shortcut_id: str,
        payload: ShortcutUpdateRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.update(caller, shortcut_id, payload.model_dump(), scope=scope)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/publish")
    def publish_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.publish(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/draft")
    def fork_shortcut_draft(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.fork_draft(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/rollback")
    def rollback_shortcut(
        shortcut_id: str,
        payload: ShortcutRollbackRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.shortcut_manager.rollback(
                caller,
                shortcut_id,
                version_no=payload.version_no,
                expected_revision=payload.expected_revision,
                scope=scope,
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return version.to_dict()

    @app.post("/v1/shortcut-admin/{business_entry}/{code}/suppress")
    def suppress_shortcut(
        business_entry: str,
        code: str,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.suppress(caller, business_entry=business_entry, code=code)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcut-admin/{business_entry}/{code}/restore")
    def restore_shortcut(
        business_entry: str,
        code: str,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.restore(caller, business_entry=business_entry, code=code)
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.post("/v1/shortcuts/{shortcut_id}/disable")
    def disable_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            shortcut = context.shortcut_manager.disable(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return shortcut.to_dict()

    @app.delete("/v1/shortcuts/{shortcut_id}")
    def delete_shortcut(
        shortcut_id: str,
        payload: ShortcutActionRequest,
        scope: Annotated[str, Query()] = TENANT_SCOPE,
        caller: ScopeContext = Depends(authenticated_shortcut_caller),  # noqa: B008
    ) -> dict[str, bool]:
        try:
            context.shortcut_manager.delete(
                caller, shortcut_id, expected_revision=payload.expected_revision, scope=scope
            )
        except ShortcutError as exc:
            raise _shortcut_error(exc) from exc
        return {"deleted": True}

    @app.get("/v1/agent-metrics/summary")
    def agent_metrics_summary(
        agent_id: Annotated[str | None, Query(max_length=128)] = None,
        window_hours: Annotated[int, Query(ge=1, le=720)] = 720,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Tenant-scoped redacted run aggregate (T7/#174).

        The tenant always comes from the authenticated scope; agent_id
        optionally narrows to one agent. Cross-tenant data is unreachable by
        construction, and rows never carry question/answer/prompt text.
        """
        from aiops_diagnostics.agent_lifecycle import VIEW_ROLES

        if not frozenset(getattr(caller, "roles", ())).intersection(VIEW_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            return context.runtime.get_agent_metrics_summary(
                caller, agent_id=agent_id, window_hours=window_hours
            )
        except AgentRuntimeError as exc:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            ) from exc
        except (ValueError, MetricsValidationError) as exc:
            raise StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, "METRICS_INVALID", str(exc)) from exc

    @app.get("/v1/agent-metrics/runs")
    def agent_metrics_runs(
        agent_id: Annotated[str | None, Query(max_length=128)] = None,
        route_type: Annotated[str | None, Query(max_length=16)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        caller: ScopeContext = Depends(authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Recent redacted run rows (T7/#174) for the caller's tenant only."""
        from aiops_diagnostics.agent_lifecycle import VIEW_ROLES

        if not frozenset(getattr(caller, "roles", ())).intersection(VIEW_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            runs = context.runtime.list_agent_metrics_runs(
                caller, agent_id=agent_id, route_type=route_type, limit=limit
            )
        except AgentRuntimeError as exc:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            ) from exc
        except (ValueError, MetricsValidationError) as exc:
            raise StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, "METRICS_INVALID", str(exc)) from exc
        return {"runs": runs}

    @app.post("/v1/enroll", status_code=status.HTTP_201_CREATED)
    def enroll(payload: EnrollRequest) -> dict[str, Any]:
        try:
            result = context.store.redeem_enrollment(
                payload.code,
                device_name=payload.device_name,
                platform=payload.platform,
            )
        except (EnrollmentError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {
            "device_id": result.device.device_id,
            "workspace_id": result.device.workspace_id,
            "tenant_id": result.device.tenant_id,
            "token": result.token,
        }

    @app.post("/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(
        payload: RunCreateRequest,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            return context.runtime.start_run(
                device,
                problem=payload.problem,
                order_no=payload.order_no,
                tenant_id=payload.tenant_id,
                key_slot=payload.key_slot,
                provider=payload.provider,
                fixture_name=payload.fixture_name,
            )
        except DeviceTenantError as exc:
            # The entry guard is one definition inside the runtime (#331); this
            # layer only maps it. A request naming a tenant outside the enrolled
            # device scope is therefore refused exactly once, with one status
            # code and one error, instead of 403 here and 400 from the runtime.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.get("/v1/runs")
    def list_runs(
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        return {"runs": context.store.list_runs(device.workspace_id, limit=limit)}

    @app.get("/v1/runs/{run_id}")
    def get_run(
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            return context.store.get_run(run_id, device.workspace_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc

    @app.get("/v1/runs/{run_id}/events")
    def list_events(
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> dict[str, Any]:
        try:
            events = context.store.list_events(
                run_id,
                device.workspace_id,
                after=after,
                limit=limit,
            )
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc
        return {
            "events": events,
            "next_after": events[-1]["sequence"] if events else after,
        }

    @app.get("/v1/runs/{run_id}/evidence")
    def list_run_evidence(
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            context.store.get_run(run_id, device.workspace_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc
        return {"evidence": context.runtime.list_evidence(run_id)}

    @app.get("/v1/runs/{run_id}/events/stream")
    async def stream_events(
        request: Request,
        run_id: str,
        device: GatewayDevice = Depends(authenticated_device),  # noqa: B008
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        try:
            context.store.get_run(run_id, device.workspace_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found") from exc

        async def event_stream():
            sequence = after
            while not await request.is_disconnected():
                events = context.store.list_events(
                    run_id,
                    device.workspace_id,
                    after=sequence,
                    limit=200,
                )
                for event in events:
                    sequence = int(event["sequence"])
                    yield _sse(event)
                run = context.store.get_run(run_id, device.workspace_id)
                if run["status"] in TERMINAL_RUN_STATUSES and not events:
                    yield _sse({"sequence": sequence, "type": "stream_end", "status": run["status"]})
                    return
                await asyncio.sleep(context.settings.event_poll_interval_seconds)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(event: dict[str, Any]) -> str:
    sequence = int(event.get("sequence", 0))
    event_type = str(event.get("type", "message"))
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"


def _caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    if settings.third_session_service_token:
        try:
            runtime = Settings.from_config(settings.server_config_file)
            return RedisThirdSessionResolver(
                ThirdSessionSettings(
                    host=runtime.redis.host,
                    port=runtime.redis.port,
                    database=runtime.redis.database,
                    username=runtime.redis.user,
                    password=runtime.redis.password,
                    service_token=settings.third_session_service_token,
                    key_prefix=settings.third_session_key_prefix,
                )
            )
        except (ValueError, OSError):
            return DisabledCallerResolver()
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


def _debug_public_error(error: Exception) -> str:
    """Bounded, redacted reason for a failed debug-run (server stays opaque)."""
    from aiops_diagnostics.redaction import redact_text

    message = redact_text(str(error))
    return message[:500] if message else error.__class__.__name__


def _authenticate_caller(
    resolver: CallerContextResolver,
    authorization: str | None,
    third_session: str | None,
    *,
    required_scope: str,
) -> ScopeContext:
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
        if third_session is None:
            return resolver.resolve(token, required_scope=required_scope)
        return resolver.resolve(token, required_scope=required_scope, third_session=third_session)
    except CallerAuthError as exc:
        if exc.code == CALLER_AUTH_FORBIDDEN:
            status_code, code = status.HTTP_403_FORBIDDEN, "INSUFFICIENT_SCOPE"
        elif exc.code == CALLER_AUTH_CONFIG_MISSING:
            status_code, code = status.HTTP_503_SERVICE_UNAVAILABLE, "ACCESS_TOKEN_VALIDATION_UNAVAILABLE"
        else:
            status_code = (
                status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_401_UNAUTHORIZED
            )
            code = "ACCESS_TOKEN_VALIDATION_UNAVAILABLE" if exc.retryable else "INVALID_ACCESS_TOKEN"
        raise StandardAPIError(
            status_code, code, "access token validation failed", retryable=exc.retryable
        ) from exc


def _health_job_response(job: dict[str, Any]) -> dict[str, Any]:
    status_value = str(job["status"])
    return {
        "job_id": job["job_id"],
        "order_no": job["order_no"],
        "rule_version": job["rule_version"],
        "status": status_value,
        "retry_after_ms": 1000 if status_value in {"queued", "running"} else None,
        "report": job.get("report"),
        "error": (
            {
                "code": job.get("error_code") or "REPORT_FAILED",
                "message": job.get("error_message") or "health report failed",
                "retryable": job.get("error_code") in {"SOURCE_UNAVAILABLE", "REPORT_TIMEOUT"},
            }
            if status_value == "failed"
            else None
        ),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "completed_at": job.get("completed_at"),
    }


_FAQ_GENERIC_CHARS = frozenset(
    {
        # Sentence-final / filler CJK that carries almost no discriminative
        # signal in this catalog. Without dropping these, "怎么办" (which ends
        # nearly every consumer title) swamps the matcher and every question
        # collapses onto the first title.
        "的",
        "了",
        "吗",
        "呢",
        "呀",
        "啊",
        "在",
        "有",
        "可",
        "以",
        "能",
        "会",
        "是",
        "怎",
        "么",
        "办",
        "如",
        "何",
        "为",
        "什",
        "一",
        "下",
        "不",
        "没",
        "很",
        "请",
    }
)
# Fraction of the question's significant chars that must appear in the title.
# Containment (not raw overlap) prevents a generic question ("续航里程...")
# from matching a title merely by sharing common chars (充/电/程) across many
# entries. A query that shares only a single char (e.g. just "枪") must not
# be enough; require at least MIN_OVERLAP distinct chars too.
_FAQ_MIN_CONTAINMENT = 0.5
_FAQ_MIN_OVERLAP = 2


def _normalize_keywords(text: str) -> set[str]:
    """Deterministic CJK char set for FAQ short-circuiting.

    Keeps significant single CJK chars (generic filler/sentence-tail dropped)
    plus whole Latin runs. Used for char-set containment against a candidate
    title: the question's distinctive chars must mostly be present in the
    title, which isolates genuinely related titles without a real tokenizer
    while resisting the "怎么办" tail and shared single-char noise.
    """
    lowered = text.lower()
    significant: set[str] = set()
    latin = ""
    for ch in lowered:
        if "一" <= ch <= "鿿":
            if latin:
                significant.add(latin)
                latin = ""
            if ch not in _FAQ_GENERIC_CHARS:
                significant.add(ch)
        elif ch.isalnum():
            latin += ch
        else:
            if latin:
                significant.add(latin)
                latin = ""
    if latin:
        significant.add(latin)
    return significant


def _faq_title_union_sigs(faq_catalog: FAQCatalog, platform: str, question_id: str) -> set[str]:
    """Signatures over ALL language variants of one entry's title (L3/#203).

    zh full question first, then the en/de/fr/es/pt compressed titles from the
    wide table — a question in any supported language matches the entry whose
    variant union it overlaps, without weakening per-entry determinism.
    """
    sigs: set[str] = set()
    for variant in faq_catalog.title_variants(platform, question_id):
        sigs |= _normalize_keywords(variant)
    return sigs


def _faq_hit_by_keywords(platform: str, faq_catalog: FAQCatalog, question: str) -> str | None:
    """Return a question_id whose multilingual titles best match the question.

    Set-containment over the union of a title's language variants: score =
    |question_sig ∩ title_union|, with a containment bar on the same union, so
    the question's significant tokens must sit mostly inside ONE entry across
    its languages. Deterministic, zero-model; a later ticket adds model
    disambiguation for ambiguous text.
    """
    qsigs = _normalize_keywords(question)
    if not qsigs:
        return None
    unions = {
        entry["question_id"]: _faq_title_union_sigs(faq_catalog, platform, entry["question_id"])
        for entry in faq_catalog.catalog(platform)
    }
    best_qid: str | None = None
    best_overlap = 0
    for question_id, title_sigs in unions.items():
        overlap = len(qsigs & title_sigs)
        if overlap > best_overlap:
            best_overlap = overlap
            best_qid = question_id
    # Require at least MIN_OVERLAP distinct tokens AND a strong containment,
    # so single-token ties ("枪") or generic overlap ("充/电/程") never fire.
    if best_overlap < _FAQ_MIN_OVERLAP or best_qid is None:
        return None
    containment = len(qsigs & unions[best_qid]) / len(qsigs)
    return best_qid if containment >= _FAQ_MIN_CONTAINMENT else None


# A candidate order token: starts AND ends on an alphanumeric, so a trailing
# sentence period or a label colon is not swallowed into the id, and carries
# 6-64 safe characters total.
_ORDER_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9_.:-]{4,62}[A-Za-z0-9](?![A-Za-z0-9])")


def _extract_order_no(text: str) -> str | None:
    """Extract the first plausible order number from free text, or None.

    Order numbers look like long alphanumeric/token runs (e.g. 19-digit
    IDs or dashed codes). We match standalone tokens of 6-64 safe chars
    (letters/digits/_.:-), which is a much stronger signal than bare digits
    (which would also hit phone numbers). This is a cheap deterministic first
    pass; a model disambiguation layer can refine it later.

    A candidate MUST carry at least one digit. Without that rule the scan
    returns the first ordinary WORD of a Latin-script question — `Please
    check charging anomalies for order (2098…)` yielded ``Please``, so the
    order was never found and the request silently degraded to a zero-order
    answer (41 live, 2026-09-20). Chinese hid the defect: CJK characters are
    outside the token class, so the scan skipped them and landed on the bare
    order number. Requiring a digit is what tells an identifier apart from a
    word; a real order number that carries none does not exist in this
    system.
    """
    if not text:
        return None
    for match in _ORDER_TOKEN.finditer(text):
        candidate = match.group(0)
        if not any(char.isdigit() for char in candidate):
            continue
        # Skip pure-digit tokens that are too short to be order ids and could
        # be phone numbers (<6 digits already excluded by length, but a long
        # pure-digit run like a phone would be 11 digits — still ambiguous;
        # order numbers in this system are >=15 chars). To be conservative,
        # only treat pure-digit runs >=15 as order candidates.
        if candidate.isdigit() and len(candidate) < 15:
            continue
        return candidate
    return None


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
    status_value = str(qa["status"])
    return {
        "type": "qa",
        "language": language,
        "qa_id": qa["qa_id"],
        "question": qa["question"],
        "status": status_value,
        "retry_after_ms": 1000 if status_value in ACTIVE_DIAGNOSIS_STATUSES else None,
        "result": qa.get("result"),
        "error": (
            {
                "code": qa.get("error_code") or "QA_FAILED",
                "message": _qa_user_message(language, qa.get("error_code")),
                "retryable": True,
            }
            if status_value in {"failed", "expired"}
            else None
        ),
    }


#: The only failure code that means retrieval actually failed. Everything else
#: on this surface — a provider error, a contract violation, an unreachable
#: model — records `QA_FAILED` and has nothing to do with the knowledge base.
_KB_FAILURE_CODE = "KB_UNAVAILABLE"


def _qa_user_message(language: str, error_code: str | None) -> str:
    """The localized, actionable sentence a customer sees when a QA job fails.

    The cause decides the sentence. Claiming the knowledge base is unavailable
    for every failure told a customer whose model provider had simply rejected
    the request that the *library* was down — a false cause and useless
    guidance, since retrying is the wrong advice only for the reader who
    believes the wrong thing is broken. Only a verified retrieval failure gets
    the retrieval copy; the rest get the honest, cause-neutral one.
    """
    pack = QA_FALLBACK_MESSAGES.get(language) or QA_FALLBACK_MESSAGES[DEFAULT_LANGUAGE]
    return pack["unavailable"] if error_code == _KB_FAILURE_CODE else pack["generation_failed"]


def _require_conversation(
    context: Any,
    caller: ScopeContext,
    conversation_id: str,
) -> dict[str, Any]:
    """Load a scope-owned conversation or raise the uniform 404."""
    try:
        return context.conversation_store.get(conversation_id, caller.scope_fingerprint)
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
        "agent_version_key": conversation["agent_version_key"],
        "active_order_no": conversation["active_order_no"],
        "is_generating": conversation["is_generating"],
        "created_at": conversation["created_at"],
        "updated_at": conversation["updated_at"],
        "expires_at": conversation["expires_at"],
    }


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


def _release_conversation_turn(
    context: Any,
    conversation: dict[str, Any] | None,
    turn_no: int | None,
) -> None:
    """Drop an unfinished turn row and free the slot (best effort)."""
    if conversation is None or turn_no is None:
        return
    with contextlib.suppress(ConversationError):
        context.conversation_store.release_turn(
            conversation["conversation_id"], conversation["scope_fingerprint"], turn_no
        )


def _keep_conversation_turn(
    context: Any,
    conversation: dict[str, Any] | None,
    turn_no: int | None,
) -> None:
    """Mark the claimed turn as persisted-without-answer (pending fill).

    The turn's answer arrives asynchronously (job worker); the row keeps the
    question and slot state. Job completion later writes the answer through
    the same store; for now the row marks the turn as asked.
    """
    if conversation is None or turn_no is None:
        return
    with contextlib.suppress(ConversationError):
        context.conversation_store.complete_turn(
            conversation["conversation_id"],
            conversation["scope_fingerprint"],
            turn_no,
            answer=None,
            token_count=0,
        )


_ACTIVE_ORDER_CUES = re.compile(r"订单|充值|充电|退款|押金|金额|费用|订单号|为什么.*停|怎么还没")
# Cues that mark a BILLING DISPUTE — a user ASSERTING their money is wrong.
#
# Covers every supported language. The Chinese-only list let every
# non-Chinese user past the guard into generic customer service: an English
# "was I overcharged" produced a plain QA answer with no order prompt at all
# (41 live, 2026-09-20). Chinese got away with bare nouns because 扣费 /
# 账单 / 金额不对 ARE claims about a specific charge.
#
# The other languages use PHRASES, not bare topic nouns, for the same reason
# the guard exists: `refund` / `remboursement` / `reembolso` alone names a
# subject, and the FAQ carries entries on exactly those subjects (q020/q021).
# Treating a topic word as a dispute would swallow the questions the FAQ
# short-circuit exists to serve — and it is not hypothetical: the first draft
# of this list included bare `refund` and broke
# test_assistant_faq_shortcircuit_ignores_generic_single_tokens.
_HIGH_RISK_ORDER_CUES = re.compile(
    # zh
    r"扣费|扣款|扣错|费用异常|金额不对|退款|退费|订单异常|订单问题|账单"
    # en
    r"|overcharg\w*|double[-\s]?charg\w*"
    r"|(?:bill|amount|charge)\w*\b[^.]{0,24}?\bwrong\b|\bwrong\b[^.]{0,24}?\b(?:bill|amount|charge)\w*"
    r"|charged\s+(?:but|however|twice|two)"
    r"|refund\s+(?:not|never|has\s+not|hasn't|still)"
    # de
    r"|überladen|überteuert|zu\s+viel\s+berechnet|doppelt\s+berechnet"
    r"|(?:rechnung|betrag|abrechnung)\w*\b[^.]{0,24}?\bfalsch\b|\bfalsch\b[^.]{0,24}?\b(?:rechnung|abrechnung)\w*"
    # fr
    r"|surfactur\w+|factur\w+\s+(?:en\s+trop|deux\s+fois)"
    r"|(?:facture|montant|facturation)\w*\b[^.]{0,24}?\b(?:incorrect|erroné|faux)\w*"
    # es
    r"|cobrad\w+\s+de\s+m[áa]s|cobrad\w+\s+dos\s+veces"
    r"|(?:factura|importe|cobro)\w*\b[^.]{0,24}?\b(?:incorrect|erróne|equivocad)\w*"
    # pt
    r"|cobrad\w+\s+a\s+mais|cobrad\w+\s+duas\s+vezes"
    r"|(?:fatura|valor|cobrança)\w*\b[^.]{0,24}?\b(?:incorret|errad|equivocad)\w*",
    re.IGNORECASE,
)


def _question_involves_active_order(question: str) -> bool:
    """Heuristic: does this follow-up clearly involve the active order?

    Deliberately conservative — only order-business words trigger the
    active_order shortcut. Plain knowledge questions ("怎么申请会员") fall
    through to qa+RAG even with an active order bound (#172 acceptance).
    """
    return bool(_ACTIVE_ORDER_CUES.search(question or ""))


def _missing_order_context_key(question: str) -> str | None:
    """The clarification key for a high-risk question, or None.

    Returns the key rather than the rendered text so the reply is localized
    by the shared ``clarification_message`` catalog. The previous form
    returned a Chinese literal regardless of Accept-Language — the same
    hardcoded-copy defect fixed on the other two clarification branches
    (#262, #282).
    """
    if _HIGH_RISK_ORDER_CUES.search(question or ""):
        return "order_no"
    return None


def _promo_route(
    context: Any,
    caller: ScopeContext,
    decision: Any,
    payload: AssistantQuestionRequest,
    *,
    forced_intent: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the promotional route for this request, or (None, None).

    Two entry signals (#231): an explicit ``shortcut_code`` from a clicked
    product-entry button, or free text that deterministically NAMES case
    exploration / solution discovery. ``forced_intent`` is the classifier's
    promotional intent when the cue matcher stayed silent. The promotional
    target always comes from the caller's own published shortcut row — the
    frontend can never self-report an agent. A missing target still routes
    to the promotional intent so the runtime can serve the honest empty card.
    """
    from aiops_diagnostics.promo_agents import PROMO_INTENTS, promo_intent_from_text
    from aiops_diagnostics.shortcut_lifecycle import ShortcutError

    intent: str | None = None
    shortcut = None
    try:
        if payload.shortcut_code:
            shortcut = context.shortcut_manager.store.find_effective_by_code(
                caller.effective_tenant_id, str(decision.platform), payload.shortcut_code
            )
            if shortcut is not None and shortcut.status == "published":
                intent = shortcut.intent if shortcut.intent in PROMO_INTENTS else None
        if intent is None:
            intent = (
                forced_intent if forced_intent in PROMO_INTENTS else promo_intent_from_text(payload.question)
            )
    except ShortcutError:
        return None, None
    if intent is None:
        return None, None
    target = getattr(shortcut, "target_agent_version", None) if shortcut is not None else None
    if not target:
        target = _published_promo_target(context, caller, str(decision.platform), intent)
    return target, intent


def _published_promo_target(context: Any, caller: ScopeContext, platform: str, intent: str) -> str | None:
    """Pin the first published shortcut of this promotional intent, if any."""
    from aiops_diagnostics.shortcut_lifecycle import ShortcutError

    try:
        rows = context.shortcut_manager.list_effective(caller, business_entry=platform)
    except ShortcutError:
        return None
    for row in rows:
        if row.intent == intent and row.target_agent_version:
            return row.target_agent_version
    return None


def _start_promo_qa(
    context: Any,
    caller: ScopeContext,
    conversation: dict[str, Any] | None,
    payload: AssistantQuestionRequest,
    language: str,
    promo_target: str | None,
    promo_intent: str,
) -> JSONResponse:
    """Start a promotional card QA job (#231): the public `qa` contract
    unchanged (202 + poll), served by the pinned promotional agent."""
    turn_no: int | None = None
    if conversation is not None:
        turn_no = _begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    try:
        qa = context.runtime.start_assistant_qa(
            caller,
            payload.question,
            conversation=conversation if turn_no is not None else None,
            conversation_turn_no=turn_no,
            language=language,
            promo_target=promo_target,
            promo_intent=promo_intent,
        )
    except (ValueError, RuntimeError) as exc:
        _release_conversation_turn(context, conversation, turn_no)
        raise StandardAPIError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "QA_UNAVAILABLE",
            "general answer unavailable",
            retryable=True,
        ) from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "type": "qa",
            "language": language,
            "qa_id": qa["qa_id"],
            "question": qa["question"],
            "status": qa["status"],
            "retry_after_ms": 1000,
            "result": qa.get("result"),
            "error": None,
            **(
                {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                if conversation
                else {}
            ),
        },
    )


def _standard_diagnosis_response(diagnosis: dict[str, Any]) -> dict[str, Any]:
    status_value = str(diagnosis["status"])
    # The shared status set decides what is terminal, exactly as it decides what
    # the store accepts: a literal copy here is how a status the store now takes
    # (a stopped diagnosis) would render `retry_after_ms=1000` and keep a client
    # polling a row the claim-guard can never let change again.
    is_terminal = status_value in TERMINAL_DIAGNOSIS_STATUSES
    return {
        "diagnosis_id": diagnosis["diagnosis_id"],
        "order_no": diagnosis["order_no"],
        "question": diagnosis.get("question"),
        "indicator_code": diagnosis.get("indicator_code"),
        "language": diagnosis.get("language", "zh"),
        "status": status_value,
        "retry_after_ms": None if is_terminal else 1000,
        "result": diagnosis.get("result") if status_value in {"completed", "inconclusive"} else None,
        "error": (
            {
                "code": diagnosis.get("error_code") or "DIAGNOSIS_FAILED",
                "message": diagnosis.get("error_message") or "diagnosis failed",
                "retryable": status_value in {"failed", "expired"},
            }
            if status_value in {"failed", "expired"}
            else None
        ),
        "created_at": diagnosis.get("created_at"),
        "updated_at": diagnosis.get("updated_at"),
        "completed_at": diagnosis.get("completed_at"),
    }
