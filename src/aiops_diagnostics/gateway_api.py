from __future__ import annotations

import asyncio
import json
import platform
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics import __version__
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
from aiops_diagnostics.config import Settings
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
    TERMINAL_RUN_STATUSES,
    AuthenticationError,
    EnrollmentError,
    GatewayDevice,
    GatewayStore,
    RunNotFoundError,
)
from aiops_diagnostics.scope_context import ScopeContext, ScopeError
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
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    order_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
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
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.caller_resolver = caller_resolver
        self.order_authorizer = order_authorizer
        self.platform_resolver = platform_resolver
        self.faq_catalog = faq_catalog


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


def create_gateway_app(
    *,
    settings: GatewayServerSettings | None = None,
    store: GatewayStore | None = None,
    runtime: GatewayRuntime | None = None,
    caller_resolver: CallerContextResolver | None = None,
    order_authorizer: OrderAuthorizer | None = None,
    platform_resolver: PlatformIdentityResolver | None = None,
    faq_catalog: FAQCatalog | None = None,
) -> FastAPI:
    selected_settings = settings or GatewayServerSettings.from_env()
    selected_settings.validate()
    selected_store = store or GatewayStore(selected_settings.database_file)
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
    context = GatewayAPI(
        selected_settings,
        selected_store,
        selected_runtime,
        selected_resolver,
        selected_authorizer,
        selected_platform_resolver,
        selected_faq_catalog,
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
    def faq_recommendations(identity: tuple[ScopeContext, Any] = Depends(faq_identity)):  # noqa: B008
        _, decision = identity
        return {
            **decision.public(),
            "faq_version": context.faq_catalog.version,
            "recommendations": context.faq_catalog.recommendations(decision.platform),
        }

    @app.get("/v1/faq/catalog")
    def faq_catalog(identity: tuple[ScopeContext, Any] = Depends(faq_identity)):  # noqa: B008
        _, decision = identity
        return {
            **decision.public(),
            "faq_version": context.faq_catalog.version,
            "entries": context.faq_catalog.catalog(decision.platform),
        }

    @app.post("/v1/faq/answer")
    def faq_answer(
        payload: FAQAnswerRequest,
        identity: tuple[ScopeContext, Any] = Depends(faq_identity),  # noqa: B008
    ):
        _, decision = identity
        try:
            answer = context.faq_catalog.answer(decision.platform, payload.question_id)
        except FAQError as exc:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, FAQ_NOT_FOUND, "FAQ question was not found"
            ) from exc
        return {
            **decision.public(),
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
                )
            except (ValueError, RuntimeError) as exc:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "DIAGNOSIS_UNAVAILABLE",
                    "diagnosis unavailable",
                    retryable=True,
                ) from exc
            base = _standard_diagnosis_response(diagnosis)
            return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={**base, "type": "diagnosis"})

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
                    try:
                        diagnosis = context.runtime.start_standard_diagnosis(
                            caller,
                            embedded,
                            payload.question,
                            None,
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
                        content={**base, "type": "diagnosis", "order_no_extracted": embedded},
                    )

        # Route 2: FAQ short-circuit (deterministic, zero-order, zero-model).
        faq_id = _faq_hit_by_keywords(decision.platform, context.faq_catalog, payload.question)
        if faq_id is not None:
            answer = context.faq_catalog.answer(decision.platform, faq_id)
            return {
                **decision.public(),
                "type": "faq",
                "faq_version": context.faq_catalog.version,
                "question_id": answer["question_id"],
                "question": answer["question"],
                "answer": answer["answer"],
                "format": answer["format"],
            }

        # Route 3: generic zero-order answer — start a real QA job (T3/#153).
        try:
            qa = context.runtime.start_assistant_qa(caller, payload.question)
        except (ValueError, RuntimeError) as exc:
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
                "qa_id": qa["qa_id"],
                "question": qa["question"],
                "status": qa["status"],
                "retry_after_ms": 1000,
                "result": qa.get("result"),
                "error": None,
            },
        )

    @app.get("/v1/assistant/questions/{qa_id}")
    def get_assistant_question(
        qa_id: str,
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
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
        return {
            "type": "qa",
            "qa_id": qa["qa_id"],
            "question": qa["question"],
            "status": qa["status"],
            "retry_after_ms": 1000 if qa["status"] in {"queued", "running"} else None,
            "result": qa.get("result"),
            "error": None,
        }

    @app.get("/v1/assistant/questions")
    def list_assistant_questions(
        identity: tuple[ScopeContext, Any] = Depends(assistant_identity),  # noqa: B008
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
            "count": len(questions),
            "questions": questions,
        }

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
        if device.tenant_id and payload.tenant_id and payload.tenant_id != device.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="requested tenant does not match the enrolled device scope",
            )
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


def _faq_hit_by_keywords(platform: str, faq_catalog: FAQCatalog, question: str) -> str | None:
    """Return a question_id whose title best matches the free question, or None.

    Char-set containment: score = |question_sig ∩ title_sig| / |question_sig|,
    requiring the question's significant chars to be mostly present in the
    title. Deterministic, zero-model; a later ticket adds model disambiguation
    for ambiguous text.
    """
    qsigs = _normalize_keywords(question)
    if not qsigs:
        return None
    best_qid: str | None = None
    best_overlap = 0
    for entry in faq_catalog.catalog(platform):
        title_sigs = _normalize_keywords(str(entry.get("question") or ""))
        if not title_sigs:
            continue
        overlap = len(qsigs & title_sigs)
        if overlap > best_overlap:
            best_overlap = overlap
            best_qid = entry["question_id"]
    # Require at least MIN_OVERLAP distinct chars AND a strong containment,
    # so single-char ties ("枪") or generic overlap ("充/电/程") never fire.
    if best_overlap < _FAQ_MIN_OVERLAP or best_qid is None:
        return None
    title_sigs = _normalize_keywords(str(faq_catalog.catalog(platform)[0]["question"]))
    for entry in faq_catalog.catalog(platform):
        if entry["question_id"] == best_qid:
            title_sigs = _normalize_keywords(str(entry.get("question") or ""))
            break
    containment = len(qsigs & title_sigs) / len(qsigs)
    return best_qid if containment >= _FAQ_MIN_CONTAINMENT else None


_ORDER_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_.:-]{6,64}(?![A-Za-z0-9])")


def _extract_order_no(text: str) -> str | None:
    """Extract the first plausible order number from free text, or None.

    Order numbers look like long alphanumeric/token runs (e.g. 19-digit
    IDs or dashed codes). We match standalone tokens of 6-64 safe chars
    (letters/digits/_.:-), which is a much stronger signal than bare digits
    (which would also hit phone numbers). This is a cheap deterministic first
    pass; a model disambiguation layer can refine it later.
    """
    if not text:
        return None
    for match in _ORDER_TOKEN.finditer(text):
        candidate = match.group(0)
        # Skip pure-digit tokens that are too short to be order ids and could
        # be phone numbers (<6 digits already excluded by length, but a long
        # pure-digit run like a phone would be 11 digits — still ambiguous;
        # order numbers in this system are >=15 chars). To be conservative,
        # only treat pure-digit runs >=15 as order candidates.
        if candidate.isdigit() and len(candidate) < 15:
            continue
        return candidate
    return None


def _standard_diagnosis_response(diagnosis: dict[str, Any]) -> dict[str, Any]:
    status_value = str(diagnosis["status"])
    is_terminal = status_value in {"completed", "inconclusive", "failed", "expired"}
    return {
        "diagnosis_id": diagnosis["diagnosis_id"],
        "order_no": diagnosis["order_no"],
        "question": diagnosis.get("question"),
        "indicator_code": diagnosis.get("indicator_code"),
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
