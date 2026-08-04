from __future__ import annotations

import asyncio
import json
import platform
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aiops_diagnostics import __version__
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
    fixture_name: str | None = Field(default=None, max_length=128)


class GatewayAPI:
    def __init__(
        self,
        settings: GatewayServerSettings,
        store: GatewayStore,
        runtime: GatewayRuntime,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime


def create_gateway_app(
    *,
    settings: GatewayServerSettings | None = None,
    store: GatewayStore | None = None,
    runtime: GatewayRuntime | None = None,
) -> FastAPI:
    selected_settings = settings or GatewayServerSettings.from_env()
    selected_settings.validate()
    selected_store = store or GatewayStore(selected_settings.database_file)
    selected_runtime = runtime or GatewayRuntime.from_settings(selected_store, selected_settings)
    context = GatewayAPI(selected_settings, selected_store, selected_runtime)

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
