"""智能体生命周期 CRUD 与租户级运行指标路由。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, status

from aiops_diagnostics.gateway.dependencies import GatewayDeps
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.metrics_store import MetricsValidationError
from aiops_diagnostics.gateway.schemas import (
    AgentActionRequest,
    AgentCreateRequest,
    AgentUpdateRequest,
)
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.publication.agent_lifecycle import (
    VIEW_ROLES,
    AgentConflict,
    AgentError,
    AgentForbidden,
    AgentNotFound,
)


def _agent_error(exc: AgentError) -> StandardAPIError:
    if isinstance(exc, AgentNotFound):
        return StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_NOT_FOUND", "agent not found")
    if isinstance(exc, AgentForbidden):
        return StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted")
    if isinstance(exc, AgentConflict):
        return StandardAPIError(
            status.HTTP_409_CONFLICT, "AGENT_REVISION_CONFLICT", "agent revision conflict"
        )
    return StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, str(exc))


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    del api

    @app.post("/v1/agents", status_code=status.HTTP_201_CREATED)
    def create_agent(
        payload: AgentCreateRequest,
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            agents = context.agent_manager.list(caller)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return {"agents": [agent.to_dict() for agent in agents]}

    @app.get("/v1/agents/{agent_id}")
    def get_agent(
        agent_id: str,
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            version = context.agent_manager.version(caller, agent_id, version_no)
        except AgentError as exc:
            raise _agent_error(exc) from exc
        return version.to_dict()

    @app.get("/v1/agent-metrics/summary")
    def agent_metrics_summary(
        agent_id: Annotated[str | None, Query(max_length=128)] = None,
        window_hours: Annotated[int, Query(ge=1, le=720)] = 720,
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Tenant-scoped redacted run aggregate."""
        if not frozenset(getattr(caller, "roles", ())).intersection(VIEW_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            return context.runtime.get_agent_metrics_summary(
                caller, agent_id=agent_id, window_hours=window_hours
            )
        except RuntimeError as exc:
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
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """Recent redacted run rows for the caller's tenant only."""
        if not frozenset(getattr(caller, "roles", ())).intersection(VIEW_ROLES):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            )
        try:
            runs = context.runtime.list_agent_metrics_runs(
                caller, agent_id=agent_id, route_type=route_type, limit=limit
            )
        except (ValueError, MetricsValidationError) as exc:
            raise StandardAPIError(status.HTTP_422_UNPROCESSABLE_ENTITY, "METRICS_INVALID", str(exc)) from exc
        except RuntimeError as exc:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_FORBIDDEN", "agent access is not permitted"
            ) from exc
        return {"runs": runs}
