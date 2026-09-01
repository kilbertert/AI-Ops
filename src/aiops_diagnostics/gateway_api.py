from __future__ import annotations

import asyncio
import json
import platform
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
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

STANDARD_ORDER_READ_SCOPE = "aiops:orders:read"
STANDARD_DIAGNOSIS_SCOPE = "aiops:diagnoses:write"
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


class StandardDiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    question: str = Field(min_length=1, max_length=4000)
    indicator_code: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )


class GatewayAPI:
    def __init__(
        self,
        settings: GatewayServerSettings,
        store: GatewayStore,
        runtime: GatewayRuntime,
        caller_resolver: CallerContextResolver,
        order_authorizer: OrderAuthorizer,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.caller_resolver = caller_resolver
        self.order_authorizer = order_authorizer


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
    context = GatewayAPI(
        selected_settings,
        selected_store,
        selected_runtime,
        selected_resolver,
        selected_authorizer,
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
            return context.caller_resolver.resolve(token, required_scope=STANDARD_ORDER_READ_SCOPE)
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

    @app.post("/v1/standard/diagnoses", status_code=status.HTTP_202_ACCEPTED)
    def create_standard_diagnosis(
        payload: StandardDiagnosisRequest,
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
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(authenticated_caller),  # noqa: B008
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


def _standard_diagnosis_response(diagnosis: dict[str, Any]) -> dict[str, Any]:
    """Render a stored standard diagnosis as the documented public contract.

    Internal fields (``scope_fingerprint``, ``workspace_id``, ``tenant_id``,
    ``provider``, ``fixture``, ``evidence``) are deliberately kept off the wire.
    """
    status_value = str(diagnosis["status"])
    is_terminal = status_value in {"completed", "inconclusive", "failed", "expired"}
    payload: dict[str, Any] = {
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
    return payload


def _caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    if not settings.introspection_url:
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
