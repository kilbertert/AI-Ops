"""执行节点能力上报、任务领取/终局回调、管家/C 端工具受控代理与 Dify 内部取数回调路由。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request, Response, status
from fastapi.responses import JSONResponse

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.data.charging_status import ChargingStatusError
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import (
    EXPIRED,
    FAILED,
    REVOKED,
    RUNNING,
    SUCCEEDED,
    AgentRun,
    AgentRunConfig,
    AgentRunConfigError,
    scope_from_json,
)
from aiops_diagnostics.gateway.dependencies import GatewayDeps
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import (
    NodeCapabilitiesRequest,
    NodeClaimRequest,
    NodeLoopCompletionRequest,
    OperatorToolAuthorizationRequest,
)
from aiops_diagnostics.identity.query_scope import resolve_query_scope

_OPERATOR_OPERATIONS = frozenset({"operator_order_diagnosis", "operator_conversation", "operator_agent"})


def _is_releasable_run(record: AgentRun | None) -> bool:
    """判断一条节点运行记录此刻是否满足快照放行条件。"""
    if record is None:
        return False
    if record.state == SUCCEEDED:
        return True
    return (
        record.state == RUNNING and record.snapshot is not None and record.expected_agent_version is not None
    )


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    del deps
    selected_settings = context.settings

    @app.post("/v1/internal/execution-node/capabilities")
    def execution_node_capabilities(
        payload: NodeCapabilitiesRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        from aiops_diagnostics.execution.node_capabilities import record
        from aiops_diagnostics.execution.node_publication import all_targets

        token = (authorization or "").removeprefix("Bearer ")
        for path in {t.config_file for t in all_targets(selected_settings)}:
            config = AgentRunConfig.load(Path(path))
            node = config.authenticate_node(token)
            if node and node.active and node.environment == selected_settings.execution_environment:
                record(selected_settings.database_file, config, node, payload.model_dump())
                return {"accepted": True}
        raise StandardAPIError(403, "NODE_NOT_AUTHORIZED", "node not authorized")

    @app.post("/v1/internal/charging-status/claims/{run_id}")
    def claim_charging_status(
        run_id: str,
        payload: NodeClaimRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
        node_build: Annotated[str | None, Header(alias="X-AI-Ops-Node-Build")] = None,
        exec_config: Annotated[str | None, Header(alias="X-AI-Ops-Exec-Config")] = None,
    ) -> dict[str, Any]:
        """节点领取端点：认证节点 → 核对绑定 → 重做订单授权 → 读业务/放行复核。"""
        del payload
        response.headers["Cache-Control"] = "no-store"
        existing = context.agent_run_store.peek(run_id)
        if existing is not None and existing.operation in _OPERATOR_OPERATIONS:
            raise StandardAPIError(403, "AGENT_RUN_OPERATION_MISMATCH", "agent run refused")
        binding_config = chat_execution.callback_config(context.agent_run_store, run_id, selected_settings)
        if binding_config is None:
            raise StandardAPIError(404, "AGENT_RUN_DISABLED", "route not found")
        token = (authorization or "").removeprefix("Bearer ").strip()

        def load_config() -> AgentRunConfig:
            try:
                resolved = chat_execution.callback_config(context.agent_run_store, run_id, selected_settings)
                if resolved is None:
                    raise AgentRunConfigError("binding is not configured")
                return resolved
            except AgentRunConfigError as exc:
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "AGENT_RUN_CONFIG_UNAVAILABLE",
                    "agent run unavailable",
                    retryable=True,
                ) from exc

        def deny_release(run_id_: str, reason: str, *, actual_node_id: str | None = None) -> None:
            if not context.agent_run_store.fail(run_id_, reason, actual_node_id=actual_node_id):
                context.agent_run_store.refuse(run_id_, reason)

        def release_candidate(run_id_: str) -> AgentRun:
            before = context.agent_run_store.get(run_id_)
            if before is None:
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found"
                )
            if not _is_releasable_run(before):
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
                )
            verdict = api._order_authorization(context, scope_from_json(before.scope_json), before.order_no)
            if verdict == api.UNAVAILABLE:
                deny_release(run_id_, "authorization_unavailable")
                raise StandardAPIError(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "ORDER_AUTHORIZATION_UNAVAILABLE",
                    "order authorization unavailable",
                    retryable=True,
                )
            if verdict == api.NOT_OWNED:
                deny_release(run_id_, "order_not_visible")
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_ORDER_NOT_VISIBLE", "agent run refused"
                )

            current = load_config()
            current_node = current.authenticate_node(token)
            if current_node is None:
                deny_release(run_id_, "node_deactivated")
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NODE_INACTIVE", "agent run refused"
                )
            latest = context.agent_run_store.get(run_id_)
            if latest is None:
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found"
                )
            if not _is_releasable_run(latest):
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
                )
            if (
                current_node.node_id != latest.expected_node_id
                or current.expected_node_id != latest.expected_node_id
                or current_node.environment != latest.environment
                or current.environment != latest.environment
                or current.snail_app_id != latest.app_id
                or str(current.snail_agent_id) != latest.agent_id
            ):
                deny_release(run_id_, "binding_changed")
                raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_NODE", "agent run refused")
            released = api._require_matching_exec_config(
                context, latest, exec_config, actual_node_id=current_node.node_id
            )
            if not _is_releasable_run(released):
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
                )
            return released

        config = load_config()
        node = config.authenticate_node(token)
        if node is None:
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "NODE_TOKEN_REQUIRED", "node credential required"
            )
        run = context.agent_run_store.get(run_id)
        if run is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found")
        if run.environment != config.environment:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_ENVIRONMENT", "agent run refused"
            )
        if node.environment != run.environment or node.node_id != run.expected_node_id:
            raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_NODE", "agent run refused")
        if run.state in {EXPIRED, REVOKED, FAILED}:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
            )
        run = api._require_matching_exec_config(context, run, exec_config, actual_node_id=node.node_id)
        if run.state in {EXPIRED, REVOKED, FAILED}:
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
            )

        try:
            frozen = scope_from_json(run.scope_json)
        except (ValueError, TypeError) as exc:
            context.agent_run_store.fail(run_id, "scope_unreadable", actual_node_id=node.node_id)
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_SCOPE_INVALID", "agent run refused"
            ) from exc

        verdict = api._order_authorization(context, frozen, run.order_no)
        if verdict == api.UNAVAILABLE:
            deny_release(run_id, "authorization_unavailable", actual_node_id=node.node_id)
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "ORDER_AUTHORIZATION_UNAVAILABLE",
                "order authorization unavailable",
                retryable=True,
            )
        if verdict == api.NOT_OWNED:
            deny_release(run_id, "order_not_visible", actual_node_id=node.node_id)
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_ORDER_NOT_VISIBLE", "agent run refused"
            )

        if _is_releasable_run(run):
            return api._claim_payload(release_candidate(run_id))
        if not context.agent_run_store.claim(run_id):
            latest = context.agent_run_store.get(run_id)
            if _is_releasable_run(latest):
                return api._claim_payload(release_candidate(run_id))
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_RUN_IN_PROGRESS",
                "agent run is already being executed",
                retryable=True,
            )
        try:
            snapshot = api._authorized_charging_snapshot(selected_settings, frozen, run.order_no)
        except ChargingStatusError as exc:
            context.agent_run_store.fail(run_id, "upstream_failed", actual_node_id=node.node_id)
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable"
            ) from exc
        if not context.agent_run_store.complete(
            run_id,
            snapshot=snapshot,
            actual_node_id=node.node_id,
            node_build=(node_build or "")[:128] or None,
        ):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active"
            )
        return api._claim_payload(release_candidate(run_id))

    @app.post("/v1/internal/charging-status/execution-config/{run_id}")
    def node_execution_config(
        run_id: str,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """已认证目标节点领取本次冻结执行清单。"""
        response.headers["Cache-Control"] = "no-store"
        run = context.agent_run_store.get(run_id)
        if run is not None and run.operation in _OPERATOR_OPERATIONS:
            return context.operator_cpms.publication(run_id, (authorization or "").removeprefix("Bearer "))
        if run is None or not run.execution_manifest:
            raise StandardAPIError(404, "NODE_PUBLICATION_NOT_FOUND", "node publication unavailable")
        config = chat_execution.callback_config(context.agent_run_store, run_id, context.settings)
        token = (authorization or "").removeprefix("Bearer ").strip()
        node = config.authenticate_node(token) if config else None
        if node is None or node.node_id != run.expected_node_id or node.environment != run.environment:
            raise StandardAPIError(403, "NODE_PUBLICATION_REFUSED", "node publication refused")
        api._require_visible_order(context, scope_from_json(run.scope_json), run.order_no)
        config = chat_execution.callback_config(context.agent_run_store, run_id, context.settings)
        latest = context.agent_run_store.get(run_id)
        current_node = config.authenticate_node(token) if config else None
        if (
            config is None
            or current_node is None
            or latest is None
            or latest.state not in {"issued", "running"}
            or current_node.node_id != latest.expected_node_id
            or current_node.environment != latest.environment
            or config.expected_node_id != latest.expected_node_id
            or config.environment != latest.environment
            or config.snail_app_id != latest.app_id
            or str(config.snail_agent_id) != latest.agent_id
        ):
            raise StandardAPIError(403, "NODE_PUBLICATION_REFUSED", "node publication refused")
        manifest = execution_manifest.from_text(latest.execution_manifest)
        if manifest.identity != latest.expected_agent_version:
            raise StandardAPIError(503, "NODE_PUBLICATION_INVALID", "node publication invalid")
        return {"run_id": run_id, "manifest": latest.execution_manifest, "sha256": manifest.identity}

    @app.post("/v1/operator-tools/{tool}")
    def operator_tool_invoke(
        tool: str,
        payload: NodeClaimRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        del payload
        response.headers["Cache-Control"] = "no-store"
        return context.operator_cpms.invoke(
            request.headers.get("Authorization", "").removeprefix("Bearer "), tool
        )

    @app.post("/v1/internal/operator-runs/{run_id}/tools/{tool}/authorization")
    def operator_tool_authorization(
        run_id: str,
        tool: str,
        payload: OperatorToolAuthorizationRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        grant = (
            context.consumer_tool_grants.grant
            if api._is_consumer_run(context, run_id)
            else context.operator_cpms.grant
        )
        return grant(
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.headers.get("X-AI-Ops-Exec-Config", ""),
            tool,
            request.headers.get("X-AI-Ops-Tool-Contract", ""),
            payload.arguments,
        )

    @app.post("/v1/internal/operator-runs/{run_id}/tools/{tool}/evidence")
    async def operator_tool_evidence(run_id: str, tool: str, request: Request) -> JSONResponse:
        from starlette.concurrency import run_in_threadpool

        body = await api._operator_json(request)
        recorder = (
            context.consumer_tool_grants.record
            if api._is_consumer_run(context, run_id)
            else context.operator_cpms.record
        )
        result = await run_in_threadpool(
            recorder,
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.headers.get("X-AI-Ops-Exec-Config", ""),
            tool,
            body,
        )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    @app.get("/v1/internal/consumer-tools/{run_id}/{path:path}")
    def consumer_tool_proxy(run_id: str, path: str, request: Request, response: Response) -> Any:
        response.headers["Cache-Control"] = "no-store"
        return context.consumer_tool_grants.proxy(
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            "/" + path,
            request.url.query,
        )

    @app.get("/v1/internal/operator-runs/{run_id}/cpms/{path:path}")
    def operator_cpms_proxy(run_id: str, path: str, request: Request, response: Response) -> Any:
        response.headers["Cache-Control"] = "no-store"
        return context.operator_cpms.proxy(
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            "/" + path,
            request.url.query,
        )

    @app.post("/v1/internal/operator-runs/{run_id}/completions")
    async def operator_completion(run_id: str, request: Request) -> JSONResponse:
        from starlette.concurrency import run_in_threadpool

        body = await api._operator_json(request)
        result = await run_in_threadpool(
            context.operator_cpms.complete,
            run_id,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.headers.get("X-AI-Ops-Exec-Config", ""),
            body,
        )
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})

    @app.get("/v1/internal/consumer-knowledge/catalogue")
    def consumer_knowledge_catalogue(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        from aiops_diagnostics.consumer.knowledge_access import catalogue_workspace
        from aiops_diagnostics.dify import candidate_knowledge
        from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError

        response.headers["Cache-Control"] = "no-store"
        presented = (authorization or "").removeprefix("Bearer ")
        if presented.startswith(candidate_knowledge.PREFIX):
            try:
                scope = candidate_knowledge.parse_token(context.settings.internal_token_secret, presented)
                result = candidate_knowledge.catalogue(context, scope)
            except (ValueError, candidate_knowledge.CandidateKnowledgeError) as exc:
                raise StandardAPIError(403, "KNOWLEDGE_CATALOGUE_FORBIDDEN", "知识目录凭据无效") from exc
            return {
                "libraries": [{"knowledge_base_id": scope[3], "name": result["knowledge_items"][0]["name"]}]
            }
        try:
            workspace = catalogue_workspace(
                presented,
                context.settings.internal_token_secret,
                context.settings.environment,
            )
            tenant = context.dify_workspaces.tenant_for_workspace(workspace, context.settings.environment)
        except (ValueError, DifyWorkspaceError) as exc:
            raise StandardAPIError(403, "KNOWLEDGE_CATALOGUE_FORBIDDEN", "知识目录凭据无效") from exc
        try:
            if context.settings.knowledge_delegation_file:
                from aiops_diagnostics.consumer.consumer_knowledge import workspace_libraries

                libraries = workspace_libraries(context.runtime, tenant, workspace)
            else:
                libraries = context.runtime.kb_search_client.for_tenant(tenant).list_knowledge_bases()
        except (ValueError, RuntimeError, AttributeError) as exc:
            raise api._runtime_unavailable(
                "KNOWLEDGE_CATALOGUE_UNAVAILABLE", "知识库目录暂不可用", exc
            ) from exc
        return {
            "libraries": [{"knowledge_base_id": r["knowledge_base_id"], "name": r["name"]} for r in libraries]
        }

    @app.post("/v1/internal/consumer-knowledge")
    @app.post("/v1/internal/consumer-data/{capability}")
    async def consumer_knowledge_context(
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        from starlette.concurrency import run_in_threadpool

        from aiops_diagnostics.consumer import consumer_chat_service
        from aiops_diagnostics.consumer.candidate_trial import PREFIX as CANDIDATE_PREFIX
        from aiops_diagnostics.execution import chat_data_grant

        response.headers["Cache-Control"] = "no-store"
        capability = request.path_params.get("capability")
        if capability is not None and capability not in {"knowledge", "status", "billing", "agent"}:
            raise StandardAPIError(404, "CONSUMER_CAPABILITY_UNKNOWN", "未知取数能力")
        if not authorization or not authorization.startswith("Bearer "):
            raise StandardAPIError(401, "GRANT_REQUIRED", "knowledge grant required")
        if authorization[7:].startswith(CANDIDATE_PREFIX):
            if capability != "agent":
                raise StandardAPIError(403, "GRANT_CAPABILITY_MISMATCH", "candidate callback refused")
            from aiops_diagnostics.consumer.candidate_trial import callback

            return await callback(context, request, authorization[7:])
        try:
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 6000:
                    raise StandardAPIError(413, "PHOTO_CONTEXT_INVALID", "image context too large")
            image_text = raw.decode("utf-8")
            if capability == "agent":
                from aiops_diagnostics.consumer import consumer_agent

                def own_orders(scope):
                    with api.scoped_mysql_source(
                        Settings.from_config(context.settings.server_config_file), resolve_query_scope(scope)
                    ) as source:
                        return source.list_orders(page=1, page_size=5)

                return await run_in_threadpool(
                    consumer_agent.callback,
                    context.runtime,
                    authorization[7:],
                    json.loads(image_text or "{}"),
                    read_snapshot=lambda scope, order, kind: api._authorized_consumer_snapshot(
                        context.settings, scope, order, kind, include_composition=True
                    ),
                    list_orders=own_orders,
                )
            knowledge_base_ids = None
            if capability in {"status", "billing"} and image_text.strip():
                raise StandardAPIError(400, "CONSUMER_DATA_INPUT_INVALID", "订单与身份不能由节点参数指定")
            if request.headers.get("Content-Type", "").startswith("application/json"):
                body = json.loads(image_text or "{}")
                allowed = (
                    {"image_text", "knowledge_base_ids"} if capability == "knowledge" else {"image_text"}
                )
                if not isinstance(body, dict) or set(body) - allowed:
                    raise ValueError("unexpected knowledge input")
                image_text = body.get("image_text", "")
                knowledge_base_ids = body.get("knowledge_base_ids")
            from aiops_diagnostics.consumer import legacy_callbacks

            return await run_in_threadpool(
                legacy_callbacks.callback_context,
                context.runtime,
                authorization[7:],
                require_result=consumer_chat_service.require_result,
                image_text=image_text,
                capability=capability,
                knowledge_base_ids=knowledge_base_ids,
                read_snapshot=lambda caller, order, kind: api._authorized_consumer_snapshot(
                    context.settings, caller, order, kind
                ),
            )
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "knowledge grant refused") from exc
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc)) from exc
        except (ValueError, OSError, ChargingStatusError) as exc:
            raise api._runtime_unavailable("KNOWLEDGE_UNAVAILABLE", "knowledge unavailable", exc) from exc

    @app.post("/v1/internal/charging-status/dify-read")
    def dify_chat_read(
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Dify Workflow 一次性取数授权回调端点。"""
        from aiops_diagnostics.dify import dify_run_store
        from aiops_diagnostics.execution import chat_data_grant

        response.headers["Cache-Control"] = "no-store"
        token = (authorization or "").removeprefix("Bearer ").strip()
        grants = chat_data_grant.DataGrantStore(context.store.path)
        try:
            grant = chat_data_grant.usable(grants.by_token(token))
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "data grant refused") from exc
        runs = context.dify_run_store
        run = runs.by_qa(grant.qa_id)
        if run is None:
            raise StandardAPIError(401, "GRANT_UNKNOWN", "data grant refused")
        if (run.order_no, run.environment, run.workflow_id) != (
            grant.order_no,
            grant.environment,
            grant.workflow_id,
        ) or not api._same_tenant_as_grant(run.tenant_id, grant.tenant_id):
            raise StandardAPIError(401, "GRANT_SCOPE_MISMATCH", "data grant refused")
        api._require_dify_grant_visible(context, run)
        latest = runs.get(run.run_id)
        if latest is None or latest.state not in {dify_run_store.RUNNING, dify_run_store.SUCCEEDED}:
            raise StandardAPIError(403, "DIFY_RUN_NOT_ACTIVE", "dify run is not active")
        try:
            current = chat_data_grant.usable(grants.by_id(grant.grant_id))
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "data grant refused") from exc
        api._require_dify_binding_current(context, run)

        existing = runs.snapshot(run.run_id)
        if existing is not None:
            return existing
        if not grants.consume(current.grant_id):
            raise StandardAPIError(409, "GRANT_ALREADY_CONSUMED", "data grant already used")
        try:
            snapshot = api._authorized_charging_snapshot(
                selected_settings, scope_from_json(run.scope_json), grant.order_no
            )
        except (ChargingStatusError, StandardAPIError) as exc:
            raise StandardAPIError(502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable") from exc
        api._require_dify_grant_visible(context, run)
        try:
            chat_data_grant.usable(grants.by_id(grant.grant_id))
        except chat_data_grant.DataGrantError as exc:
            raise StandardAPIError(401, exc.code, "data grant refused") from exc
        latest_now = runs.get(run.run_id)
        if latest_now is None or latest_now.state != dify_run_store.RUNNING:
            raise StandardAPIError(403, "DIFY_RUN_NOT_ACTIVE", "dify run is not active")
        api._require_dify_binding_current(context, run)
        runs.store_snapshot(run.run_id, snapshot)
        return snapshot

    @app.post("/v1/internal/charging-status/completions/{run_id}")
    def complete_charging_status_loop(
        run_id: str,
        payload: NodeLoopCompletionRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
        exec_config: Annotated[str | None, Header(alias="X-AI-Ops-Exec-Config")] = None,
    ) -> dict[str, Any]:
        """执行节点整轮模型工具循环的终局确认回调。"""
        response.headers["Cache-Control"] = "no-store"
        existing = context.agent_run_store.peek(run_id)
        if existing is not None and existing.operation in _OPERATOR_OPERATIONS:
            if payload.status == "completed":
                raise StandardAPIError(403, "AGENT_RUN_OPERATION_MISMATCH", "agent run refused")
            return context.operator_cpms.complete(
                run_id,
                (authorization or "").removeprefix("Bearer "),
                exec_config,
                {"status": "failed", "reason": payload.reason or ""},
            )
        config = chat_execution.callback_config(context.agent_run_store, run_id, selected_settings)
        if config is None:
            raise StandardAPIError(404, "AGENT_RUN_DISABLED", "route not found")
        token = (authorization or "").removeprefix("Bearer ").strip()
        node = config.authenticate_node(token)
        if node is None:
            raise StandardAPIError(
                status.HTTP_401_UNAUTHORIZED, "NODE_TOKEN_REQUIRED", "node credential required"
            )
        run = context.agent_run_store.peek(run_id)
        if run is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found")
        if (
            node.environment != run.environment
            or node.node_id != run.expected_node_id
            or config.environment != run.environment
            or config.expected_node_id != run.expected_node_id
            or config.snail_app_id != run.app_id
            or str(config.snail_agent_id) != run.agent_id
        ):
            raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_WRONG_NODE", "agent run refused")

        verdict, settled = context.agent_run_store.complete_loop(
            run_id,
            observed_agent_version=exec_config,
            actual_node_id=node.node_id,
            failure_reason=(payload.reason or "model_failed") if payload.status == "failed" else None,
            success_reason=payload.reason if payload.status == "completed" else None,
        )
        if verdict in {"completed", "already_completed", "failed", "already_failed"}:
            assert settled is not None
            return {"run_id": run_id, "state": settled.state}
        if verdict in {"conflict", "incomplete"}:
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_RUN_COMPLETION_CONFLICT",
                "agent run is already settled or not in progress",
            )
        if verdict == "drift":
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN, "AGENT_RUN_EXEC_CONFIG_DRIFT", "agent run refused"
            )
        if verdict == "not_tracked":
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "AGENT_RUN_COMPLETION_NOT_TRACKED",
                "agent run does not track a loop completion",
            )
        if verdict == "missing":
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "AGENT_RUN_NOT_FOUND", "agent run not found")
        raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_NOT_ACTIVE", "agent run is not active")
