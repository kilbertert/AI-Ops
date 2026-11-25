"""订单授权查询、充电状态读取（直读/委派）与 C 端站点列表路由。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Response, status

from aiops_diagnostics import __version__
from aiops_diagnostics.execution import execution_manifest
from aiops_diagnostics.execution.agent_runs import (
    SUCCEEDED,
    AgentRunConfig,
    AgentRunConfigError,
    AgentRunDispatchError,
)
from aiops_diagnostics.gateway.dependencies import GatewayDeps
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import ChargingStatusDelegationRequest
from aiops_diagnostics.identity.order_identity import is_order_identifier
from aiops_diagnostics.identity.scope_context import ScopeContext


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    selected_settings = context.settings

    @app.get("/v1/orders/{order_no}/access")
    def order_access(
        order_no: str,
        caller: ScopeContext = Depends(deps.authenticated_caller),  # noqa: B008
    ) -> dict[str, Any]:
        if not is_order_identifier(order_no):
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_ORDER_NO",
                "invalid order number",
            )
        verdict = api._order_authorization(context, caller, order_no)
        if verdict == api.UNAVAILABLE:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == api.NOT_OWNED:
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

    @app.get("/v1/orders/{order_no}/charging-status")
    def charging_status(
        order_no: str,
        response: Response,
        caller: ScopeContext = Depends(deps.authenticated_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        if not selected_settings.charging_status_base_url:
            from aiops_diagnostics.consumer.knowledge_access import consumer_endpoint

            try:
                consumer_endpoint(selected_settings, caller.effective_tenant_id)
            except ValueError as exc:
                raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "route not found") from exc
        api._require_consumer_self_order(caller, business_entry, order_no)
        api._require_visible_order(context, caller, order_no)
        return api._charged_snapshot_response(selected_settings, context, caller, order_no)

    @app.get("/v1/sites")
    def consumer_sites(
        response: Response,
        name: str = "",
        address: str = "",
        limit: str = "",
        caller: ScopeContext = Depends(deps.authenticated_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        """C 端只读站点列表：范围只含本人订单里出现过的站点。"""
        from aiops_diagnostics.consumer import consumer_sites as consumer_sites_module
        from aiops_diagnostics.consumer.knowledge_access import consumer_endpoint

        response.headers["Cache-Control"] = "no-store"
        api._require_consumer_self(caller, business_entry)
        try:
            base_url = consumer_endpoint(selected_settings, caller.effective_tenant_id)
        except ValueError as exc:
            raise StandardAPIError(
                404, "TENANT_ENDPOINT_UNRESOLVED", "business endpoint is not registered"
            ) from exc
        try:
            return consumer_sites_module.sites_for_consumer(
                caller,
                base_url,
                name=name,
                address=address,
                limit=consumer_sites_module.parse_limit(limit),
            )
        except consumer_sites_module.ConsumerSitesRefused as exc:
            code_status = 403 if exc.code == "CONSUMER_SCOPE_REQUIRED" else 400
            if exc.code.endswith("UNAVAILABLE"):
                code_status = 502
            raise StandardAPIError(code_status, exc.code, str(exc)) from exc

    @app.post("/v1/orders/{order_no}/charging-status/delegated")
    def delegated_charging_status(
        order_no: str,
        payload: ChargingStatusDelegationRequest,
        response: Response,
        caller: ScopeContext = Depends(deps.authenticated_caller),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        """将一次已授权的订单充电状态读取委派给可信执行节点。"""
        del payload
        response.headers["Cache-Control"] = "no-store"
        if not selected_settings.agent_run_config_file:
            raise StandardAPIError(404, "AGENT_RUN_DISABLED", "route not found")
        api._require_consumer_self_order(caller, business_entry, order_no)
        api._require_visible_order(context, caller, order_no)
        try:
            config = AgentRunConfig.load(Path(selected_settings.agent_run_config_file))
        except AgentRunConfigError as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_RUN_CONFIG_UNAVAILABLE",
                "agent run unavailable",
                retryable=True,
            ) from exc
        if config.environment != selected_settings.execution_environment:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_RUN_ENVIRONMENT_MISMATCH",
                "agent run unavailable",
                retryable=True,
            )
        try:
            manifest = execution_manifest.load(selected_settings.execution_manifest_file)
        except execution_manifest.ExecutionManifestError as exc:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "EXEC_CONFIG_UNAVAILABLE",
                "agent run unavailable",
                retryable=True,
            ) from exc
        run = context.agent_run_store.issue(
            scope=caller,
            order_no=order_no,
            config=config,
            gateway_version=__version__,
            expected_agent_version=(manifest.identity if manifest is not None else None),
        )
        try:
            api.SnailDispatcher().dispatch(config, run.run_id)
        except AgentRunDispatchError as exc:
            context.agent_run_store.fail(run.run_id, "platform_unavailable")
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY, "AGENT_RUN_UNAVAILABLE", "agent run unavailable"
            ) from exc
        try:
            settled = context.agent_run_store.get(run.run_id)
        except (ValueError, TypeError) as exc:
            raise StandardAPIError(502, "AGENT_RUN_NO_RESULT", "agent run produced no result") from exc
        if settled is None:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AGENT_RUN_UNAVAILABLE",
                "agent run unavailable",
                retryable=True,
            )
        if settled.state != SUCCEEDED:
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY, "AGENT_RUN_NO_RESULT", "agent run produced no result"
            )
        return api._consumer_charging_result(selected_settings, context, caller, run)
