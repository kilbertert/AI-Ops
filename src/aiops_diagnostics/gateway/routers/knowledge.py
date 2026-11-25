"""知识库控制台跳转、同步、静态页与租户知识源自助管理路由。"""

from __future__ import annotations

import logging
from importlib.resources import files
from typing import Any

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from aiops_diagnostics.gateway.dependencies import GatewayDeps
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import KnowledgeSourceRequest
from aiops_diagnostics.identity.order_visibility import tenant_matches
from aiops_diagnostics.identity.scope_context import ScopeContext

_LOGGER = logging.getLogger("aiops.gateway")


def _require_knowledge_admin(caller: ScopeContext) -> None:
    """校验调用者具备有效租户身份（跨租户越权由按 effective_tenant_id 隔离拦截）。"""
    if not str(getattr(caller, "effective_tenant_id", "") or "").strip():
        raise StandardAPIError(
            status.HTTP_403_FORBIDDEN,
            "KNOWLEDGE_FORBIDDEN",
            "knowledge-source configuration requires a tenant identity",
        )


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    del api

    @app.get("/v1/knowledge/console", include_in_schema=False)
    def knowledge_console(
        request: Request,
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> Response:
        """为本租户运营签发 RAGFlow 控制台会话（HTML 顶层导航 303，脚本 fetch 返回 200 JSON）。"""
        from aiops_diagnostics.knowledge.ragflow_console import (
            RAGFlowLoginClient,
            RAGFlowLoginError,
            open_console,
            record,
        )

        settings = context.settings
        tenant_id = str(caller.effective_tenant_id)
        if (
            not settings.ragflow_console_base_url
            or not settings.ragflow_credential_key
            or not settings.ragflow_console_public_pem
        ):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_CONSOLE_DISABLED", "console access is not enabled"
            )
        client = RAGFlowLoginClient(settings.ragflow_console_base_url)
        try:
            target, session = open_console(
                store=context.store,
                environment=settings.environment,
                caller=caller,
                credential_key=settings.ragflow_credential_key,
                client=client,
                public_base_url=settings.ragflow_console_public_url or settings.ragflow_console_base_url,
                public_pem_path=settings.ragflow_console_public_pem,
            )
        except RAGFlowLoginError as exc:
            record(
                context.store,
                tenant_id=tenant_id,
                environment=settings.environment,
                outcome="refused",
                caller=caller,
                reason_code=exc.code,
            )
            http_status = (
                status.HTTP_403_FORBIDDEN
                if exc.code in {"KNOWLEDGE_FORBIDDEN"}
                else status.HTTP_424_FAILED_DEPENDENCY
                if exc.code != "KNOWLEDGE_TENANT_NOT_ONBOARDED"
                else status.HTTP_409_CONFLICT
            )
            raise StandardAPIError(http_status, exc.code, exc.message) from exc
        record(
            context.store,
            tenant_id=tenant_id,
            environment=settings.environment,
            outcome="granted",
            caller=caller,
            ragflow_email=session.ragflow_email,
        )
        wants_html = "text/html" in (request.headers.get("accept") or "")
        response: Response = (
            RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
            if wants_html
            else JSONResponse(
                {"target": target, "ragflow_email": session.ragflow_email},
                status_code=status.HTTP_200_OK,
            )
        )
        for name, value in session.cookies.items():
            response.set_cookie(
                name,
                value,
                domain=settings.ragflow_console_cookie_domain or None,
                path="/",
                httponly=True,
                samesite="lax",
            )
        return response

    @app.post("/v1/internal/knowledge/sync", include_in_schema=False)
    def knowledge_sync_by_workspace(
        request: Request,
        workspace: str = "",
        _: None = Depends(deps.dify_workspace_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """按 Dify workspace 反查租户，将 RAGFlow 库幂等收敛至 Dify dataset。"""
        for forbidden in ("tenant_id", "tenant", "environment"):
            if forbidden in request.query_params:
                raise StandardAPIError(
                    status.HTTP_400_BAD_REQUEST,
                    "KNOWLEDGE_SYNC_PARAM_NOT_ACCEPTED",
                    f"{forbidden} is resolved from the workspace, never from the request",
                )
        if not workspace.strip():
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "KNOWLEDGE_SYNC_WORKSPACE_REQUIRED",
                "workspace is required",
            )
        settings = context.settings
        if not settings.dify_console_base_url or not settings.dify_console_api_key:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SYNC_DISABLED", "console access is not enabled"
            )
        if not settings.dify_external_knowledge_api_id and not settings.dify_knowledge_api_key:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "KNOWLEDGE_SYNC_DISABLED",
                "external knowledge connection is not configured",
            )
        workspaces = getattr(context.runtime, "dify_workspaces", None)
        client = getattr(context.runtime, "kb_search_client", None)
        if workspaces is None or client is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SYNC_DISABLED", "knowledge sync is not available"
            )

        from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
        from aiops_diagnostics.dify.dify_knowledge_datasets import libraries_for, run
        from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError

        try:
            tenant_id = workspaces.tenant_for_workspace(workspace.strip(), settings.environment)
        except DifyWorkspaceError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message) from exc
        try:
            owned = client.for_tenant(tenant_id).list_knowledge_bases()
        except Exception:  # noqa: BLE001 - upstream unavailable degrades cleanly
            return {"created": 0, "tenant_id": tenant_id, "unavailable": True}

        libraries = libraries_for(tenant_id, owned)
        console = DifyConsoleAdmin(
            ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
        )

        def converge() -> Any:
            from aiops_diagnostics.dify.knowledge_connection import ensure

            workspace_api_id = ensure(console, workspace.strip(), settings)
            return run(
                workspace_id=workspace.strip(),
                libraries=libraries,
                console=console,
                external_knowledge_api_id=workspace_api_id,
                dry_run=False,
                retire_orphans=True,
            )

        guard = getattr(context.runtime, "knowledge_dataset_sync_guard", None)
        outcome: list[dict[str, Any]] = []
        if guard is None:
            guard_outcome = "unguarded"
            outcome.append(converge())
        else:
            guard_outcome = guard.run(work=lambda: (outcome.append(converge()), None)[1])
        if not outcome:
            _LOGGER.info("knowledge sync workspace=%s tenant=%s %s", workspace, tenant_id, guard_outcome)
            return {"created": 0, "tenant_id": tenant_id, "skipped": True}
        applied = outcome[0]
        created = sum(1 for item in applied.get("applied") or [] if item.get("kind") == "create")
        changed = len(applied.get("applied") or []) + len(applied.get("retired") or [])
        _LOGGER.info(
            "knowledge sync workspace=%s tenant=%s applied=%d created=%d orphans=%d changed=%d",
            workspace,
            tenant_id,
            len(applied.get("applied") or []),
            created,
            len(applied.get("orphans") or []),
            changed,
        )
        return {"created": created, "changed": changed, "tenant_id": tenant_id, "outcome": applied}

    @app.get("/v1/internal/knowledge/open", include_in_schema=False)
    def knowledge_open_by_workspace(
        request: Request,
        workspace: str = "",
        _: None = Depends(deps.dify_workspace_credential),  # noqa: B008
    ) -> Response:
        """按 Dify workspace 反查租户并签发 60 秒一次性知识库跳转票据。"""
        for forbidden in ("tenant_id", "tenant", "environment"):
            if forbidden in request.query_params:
                raise StandardAPIError(
                    status.HTTP_400_BAD_REQUEST,
                    "KNOWLEDGE_OPEN_PARAM_NOT_ACCEPTED",
                    f"{forbidden} is resolved from the workspace, never from the request",
                )
        if not workspace.strip():
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "KNOWLEDGE_OPEN_WORKSPACE_REQUIRED",
                "workspace is required",
            )
        from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError
        from aiops_diagnostics.knowledge.ragflow_console import (
            DeclaredTenant,
            RAGFlowLoginClient,
            RAGFlowLoginError,
            open_console,
            record,
        )

        settings = context.settings
        if (
            not settings.ragflow_console_base_url
            or not settings.ragflow_credential_key
            or not settings.ragflow_console_public_pem
        ):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_CONSOLE_DISABLED", "console access is not enabled"
            )
        try:
            tenant_id = context.dify_workspaces.tenant_for_workspace(workspace, settings.environment)
        except DifyWorkspaceError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc
        except ValueError as exc:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST, "KNOWLEDGE_OPEN_WORKSPACE_INVALID", str(exc)
            ) from exc

        client = RAGFlowLoginClient(settings.ragflow_console_base_url)
        try:
            _target, session = open_console(
                store=context.store,
                environment=settings.environment,
                caller=DeclaredTenant(effective_tenant_id=tenant_id),
                credential_key=settings.ragflow_credential_key,
                client=client,
                public_base_url=settings.ragflow_console_public_url or settings.ragflow_console_base_url,
                public_pem_path=settings.ragflow_console_public_pem,
            )
        except RAGFlowLoginError as exc:
            record(
                context.store,
                tenant_id=tenant_id,
                environment=settings.environment,
                outcome="refused",
                reason_code=exc.code,
            )
            http_status = (
                status.HTTP_409_CONFLICT
                if exc.code == "KNOWLEDGE_TENANT_NOT_ONBOARDED"
                else status.HTTP_424_FAILED_DEPENDENCY
            )
            raise StandardAPIError(http_status, exc.code, exc.message) from exc
        record(
            context.store,
            tenant_id=tenant_id,
            environment=settings.environment,
            outcome="granted",
            ragflow_email=session.ragflow_email,
        )
        ticket = context.store.create_hop_ticket(
            tenant_id=tenant_id,
            environment=settings.environment,
            workspace_id=workspace,
            ttl_seconds=settings.knowledge_hop_ttl_seconds,
        )
        return JSONResponse(
            {
                "target": (settings.ragflow_console_public_url or "").rstrip("/")
                + "/__aiops/knowledge/hop?ticket="
                + ticket,
                "tenant_id": tenant_id,
                "expires_in": int(settings.knowledge_hop_ttl_seconds),
            },
            status_code=status.HTTP_200_OK,
        )

    @app.get("/v1/internal/knowledge/hop", include_in_schema=False)
    def knowledge_hop(ticket: str = "") -> Response:
        """兑换一次性知识库跳转票据，在同域种植 RAGFlow 会话 cookie 并 303 重定向。"""
        from aiops_diagnostics.knowledge.ragflow_console import (
            DeclaredTenant,
            RAGFlowLoginClient,
            RAGFlowLoginError,
            open_console,
            record,
        )

        settings = context.settings
        if (
            not settings.ragflow_console_base_url
            or not settings.ragflow_credential_key
            or not settings.ragflow_console_public_pem
        ):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_CONSOLE_DISABLED", "console access is not enabled"
            )
        consumed = context.store.consume_hop_ticket(ticket, environment=settings.environment)
        if consumed is None:
            raise StandardAPIError(
                status.HTTP_410_GONE,
                "KNOWLEDGE_HOP_EXPIRED",
                "this link has expired; go back to Dify and click the button again",
            )
        tenant_id = consumed["tenant_id"]
        client = RAGFlowLoginClient(settings.ragflow_console_base_url)
        try:
            target, session = open_console(
                store=context.store,
                environment=settings.environment,
                caller=DeclaredTenant(effective_tenant_id=tenant_id),
                credential_key=settings.ragflow_credential_key,
                client=client,
                public_base_url=settings.ragflow_console_public_url or settings.ragflow_console_base_url,
                public_pem_path=settings.ragflow_console_public_pem,
            )
        except RAGFlowLoginError as exc:
            record(
                context.store,
                tenant_id=tenant_id,
                environment=settings.environment,
                outcome="refused",
                reason_code=exc.code,
            )
            raise StandardAPIError(status.HTTP_424_FAILED_DEPENDENCY, exc.code, exc.message) from exc
        record(
            context.store,
            tenant_id=tenant_id,
            environment=settings.environment,
            outcome="granted",
            ragflow_email=session.ragflow_email,
        )
        response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        for name, value in session.cookies.items():
            response.set_cookie(
                name,
                value,
                domain=settings.ragflow_console_cookie_domain or None,
                path="/",
                httponly=True,
                samesite="lax",
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/v1/knowledge", include_in_schema=False)
    def knowledge_page() -> HTMLResponse:
        html = (
            files("aiops_diagnostics.knowledge").joinpath("static/knowledge.html").read_text(encoding="utf-8")
        )
        return HTMLResponse(content=html)

    @app.get("/v1/knowledge/available")
    def list_available_knowledge(
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        """列出调用者所在租户拥有的 RAGFlow 数据集及其绑定状态。"""
        _require_knowledge_admin(caller)
        client = context.runtime.kb_search_client
        if client is None:
            raise StandardAPIError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "KB_UNAVAILABLE",
                "knowledge base service is not configured",
                retryable=True,
            )
        tenant_id = caller.effective_tenant_id
        try:
            available = client.for_tenant(tenant_id).list_knowledge_bases()
        except Exception as exc:  # noqa: BLE001 - report upstream outage
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "KB_UNAVAILABLE",
                "knowledge base service is temporarily unavailable",
                retryable=True,
            ) from exc
        bound_ids = {
            kb_id
            for row in context.store.list_knowledge_id_bindings(tenant_id)
            for kb_id in row["knowledge_base_ids"]
        }
        return {
            "type": "knowledge_available",
            "tenant_id": tenant_id,
            "knowledge_bases": [
                {**item, "bound": item["knowledge_base_id"] in bound_ids} for item in available
            ],
        }

    @app.get("/v1/knowledge/sources")
    def list_knowledge_sources(
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        _require_knowledge_admin(caller)
        rows = context.store.list_knowledge_id_bindings(caller.effective_tenant_id)
        return {
            "type": "knowledge_source_list",
            "count": len(rows),
            "sources": [
                {
                    "knowledge_id": row["knowledge_id"],
                    "tenant_id": row["tenant_id"],
                    "knowledge_base_ids": list(row["knowledge_base_ids"]),
                    "display_name": row["display_name"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    @app.put("/v1/knowledge/sources/{knowledge_id}")
    def put_knowledge_source(
        knowledge_id: str,
        payload: KnowledgeSourceRequest,
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        _require_knowledge_admin(caller)
        if not tenant_matches(payload.tenant_id, caller.effective_tenant_id):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "KNOWLEDGE_TENANT_MISMATCH",
                "a knowledge source can only be registered for your own tenant",
            )
        if payload.knowledge_id != knowledge_id:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "KNOWLEDGE_ID_MISMATCH",
                "path knowledge_id and body knowledge_id disagree",
            )
        existing = context.store.get_knowledge_id_binding(knowledge_id)
        if existing is not None and not tenant_matches(existing["tenant_id"], caller.effective_tenant_id):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SOURCE_NOT_FOUND", "knowledge source not found"
            )
        row = context.store.set_knowledge_id_binding(
            knowledge_id,
            payload.tenant_id,
            tuple(payload.knowledge_base_ids),
            display_name=payload.display_name,
            created_by=str(getattr(caller, "subject_id", "") or ""),
        )
        return {
            "type": "knowledge_source",
            "knowledge_id": row["knowledge_id"],
            "tenant_id": row["tenant_id"],
            "knowledge_base_ids": list(row["knowledge_base_ids"]),
            "display_name": row["display_name"],
        }

    @app.delete("/v1/knowledge/sources/{knowledge_id}")
    def delete_knowledge_source(
        knowledge_id: str,
        caller: ScopeContext = Depends(deps.authenticated_agent_caller),  # noqa: B008
    ) -> dict[str, Any]:
        _require_knowledge_admin(caller)
        existing = context.store.get_knowledge_id_binding(knowledge_id)
        if existing is None or not tenant_matches(existing["tenant_id"], caller.effective_tenant_id):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "KNOWLEDGE_SOURCE_NOT_FOUND", "knowledge source not found"
            )
        context.store.remove_knowledge_id_binding(knowledge_id)
        return {"type": "knowledge_source_deleted", "knowledge_id": knowledge_id}
