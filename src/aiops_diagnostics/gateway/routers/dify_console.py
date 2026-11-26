"""Dify 外部知识检索、发布/撤回/恢复、工作区解析与草稿试运行路由。"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request, status

from aiops_diagnostics.consumer import consumer_intent, consumer_publication, consumer_trial
from aiops_diagnostics.dify import self_service_config, self_service_identity
from aiops_diagnostics.dify.dify_debug_identity import DifyIdentity
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslSource
from aiops_diagnostics.dify.dify_knowledge_api import (
    DIFY_KNOWLEDGE_LIST_PATH,
    DIFY_RETRIEVAL_PATH,
    DifyRequestError,
    dify_records,
    parse_dify_retrieval_request,
)
from aiops_diagnostics.dify.dify_trial import TrialInitiator, TrialRefused
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.execution import chat_execution, chat_route_bindings, node_publication
from aiops_diagnostics.gateway.dependencies import GatewayDeps, request_language
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import (
    DifyPublishRequest,
    DifyRestoreRequest,
    DifyRetractRequest,
    DifyTrialRequest,
    ManagedTrialRequest,
    ManagedTrialResultRequest,
    OperatorPublishRequest,
    OperatorTrialCancelRequest,
)
from aiops_diagnostics.identity.scope_context import ScopeContext, is_operator_entry
from aiops_diagnostics.knowledge import knowledge_delegation
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KnowledgeSearchUnavailable,
    normalize_search_response,
)
from aiops_diagnostics.onboarding import candidate_knowledge
from aiops_diagnostics.operator import operator_cpms


def _start_operator_preview_trial(
    context: Any,
    api: Any,
    payload: DifyTrialRequest,
    request: Request,
    target: Any,
    language: str,
) -> dict[str, Any]:
    """启动受管管家草稿试运行（由 Dify 控制台原生预览发起）。"""
    session = request.headers.get("X-AIOps-Console-Session", "")

    def console_operator_identity():
        return api.authenticated_operator_console(context, session)

    caller, bearer = console_operator_identity()
    managed = (
        SelfServiceStore(context.settings.database_file).app(payload.app_id)
        if context.settings.operator_profiles_file
        else None
    )
    if managed:
        if not api._tenant_in_scope(managed["tenant_id"], caller.effective_tenant_id):
            raise operator_cpms.OperatorRefused("OPERATOR_TRIAL_SCOPE_MISMATCH")
        binding = chat_execution.trial_binding(context.settings, payload.app_id)
    else:
        binding = chat_execution.select_binding(
            context.settings, caller, scenario=operator_cpms.SCENARIO, entry="operator"
        )
    if binding is None or binding.app_id != payload.app_id:
        raise operator_cpms.OperatorRefused("OPERATOR_TRIAL_SCOPE_MISMATCH")
    workspace = api._workspace_for_tenant(context, caller.effective_tenant_id)
    source = DifyDslSource(
        context.settings.dify_console_base_url,
        payload.app_id,
        context.settings.dify_console_api_key,
        workspace,
    )
    try:
        manifest = node_publication.candidate(
            source,
            target,
            **(
                {"operator_preview": payload.operator_preview.model_dump()}
                if payload.operator_preview is not None
                else {}
            ),
        )
    except ValueError as exc:
        raise StandardAPIError(400, "OPERATOR_PREVIEW_INVALID", "invalid operator draft") from exc

    def current_console_scope():
        current, token = console_operator_identity()
        if token != bearer:
            raise operator_cpms.OperatorRefused("OPERATOR_TRIAL_LOGIN_CHANGED")
        return current

    previous = (
        api._resolve_conversation(
            context, caller, SimpleNamespace(platform="operator"), payload.conversation_id
        )
        if payload.conversation_id
        else None
    )
    conversation = api._operator_conversation(context, caller, binding, previous, trial=True)
    turn_no = api._begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    try:
        qa = context.runtime.start_operator_cpms(
            context.operator_cpms,
            caller,
            binding,
            payload.order_no,
            payload.question,
            bearer=bearer,
            authorize=current_console_scope,
            base=target.cpms_base_url,
            proxy=target.proxy_base_url,
            conversation_turn=(
                conversation["conversation_id"],
                caller.scope_fingerprint,
                turn_no,
            ),
            language=language,
            trial_manifest=manifest,
        )
    except Exception:
        api._release_conversation_turn(context, conversation, turn_no)
        raise
    return {
        "trial_id": qa["qa_id"],
        "status": qa["status"],
        "conversation_id": conversation["conversation_id"],
    }


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:

    def managed_trial_caller(request: Request, app_id: str) -> ScopeContext:
        session = request.headers.get("X-AIOps-Console-Session", "")
        row = context.self_service.authorize_app(self_service_identity.identity(context, session), app_id)
        caller, _ = api.authenticated_operator_console(context, session)
        if (
            row["entry"] != "consumer"
            or row["environment"] != context.settings.execution_environment
            or not api._tenant_in_scope(row["tenant_id"], caller.effective_tenant_id)
        ):
            raise StandardAPIError(403, "TRIAL_SCOPE_MISMATCH", "请使用客户公司账号预览当前客户端")
        return caller

    def managed_trial_result(
        payload: ManagedTrialResultRequest, request: Request
    ) -> tuple[Any, dict[str, Any]]:
        caller = managed_trial_caller(request, payload.app_id)
        trial = context.runtime.get_trial(caller, payload.trial_id)
        if not trial or trial.get("trial_source_app_id") != payload.app_id:
            raise StandardAPIError(404, "TRIAL_NOT_FOUND", "trial not found")
        return caller, trial

    @app.post(DIFY_RETRIEVAL_PATH)
    def dify_retrieval(
        payload: dict[str, Any],
        identity: DifyIdentity = Depends(deps.dify_shared_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """Dify External Knowledge API 检索适配入口。"""
        try:
            request = parse_dify_retrieval_request(payload, max_top_k=context.settings.kb_search_max_results)
        except DifyRequestError as exc:
            raise StandardAPIError(status.HTTP_400_BAD_REQUEST, exc.code, exc.message) from exc
        if identity.delegated_scope:
            try:
                return knowledge_delegation.retrieve(context, identity.delegated_scope, request)
            except ValueError as exc:
                raise StandardAPIError(403, "DIFY_CREDENTIAL_REJECTED", "credential rejected") from exc
            except KnowledgeSearchUnavailable as exc:
                raise StandardAPIError(
                    502, "DIFY_KNOWLEDGE_UNAVAILABLE", "knowledge unavailable", retryable=True
                ) from exc
        if identity.candidate_scope:
            try:
                return candidate_knowledge.retrieve(context, identity.candidate_scope, request)
            except candidate_knowledge.CandidateKnowledgeError as exc:
                raise StandardAPIError(exc.status, exc.code, str(exc)) from exc

        sources = deps.knowledge_sources(context.settings, context.store)
        binding = sources.resolve(
            request.knowledge_id, fetch_libraries=deps.libraries_for_tenant(context.settings, context.runtime)
        )
        if binding is None or not identity.allows(request.knowledge_id, binding.tenant_id):
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_KNOWLEDGE_NOT_FOUND", "knowledge source not found"
            )

        client = context.runtime.kb_search_client
        signer = context.runtime.media_signer
        if client is None or signer is None:
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "DIFY_KNOWLEDGE_UNAVAILABLE",
                "knowledge retrieval is temporarily unavailable",
                retryable=True,
            )
        top_k = request.top_k or context.settings.kb_search_max_results
        search_client = client.for_tenant(binding.tenant_id) if hasattr(client, "for_tenant") else client
        try:
            raw = search_client.search(binding.knowledge_base_ids, request.query, top_k)
            chunks = normalize_search_response(
                raw,
                tenant_id=binding.tenant_id,
                agent_version=f"dify:{binding.knowledge_id}",
                session_id=None,
                knowledge_base_ids=binding.knowledge_base_ids,
                media_signer=signer,
                max_results=top_k,
            )
        except KnowledgeSearchUnavailable as exc:
            raise StandardAPIError(
                status.HTTP_502_BAD_GATEWAY,
                "DIFY_KNOWLEDGE_UNAVAILABLE",
                "knowledge retrieval is temporarily unavailable",
                retryable=True,
            ) from exc
        return {
            "records": dify_records(
                chunks,
                score_threshold=request.score_threshold,
                public_base_url=context.settings.public_base_url,
            )
        }

    @app.get(DIFY_KNOWLEDGE_LIST_PATH)
    def dify_knowledge_list(
        identity: DifyIdentity = Depends(deps.dify_shared_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """列出当前凭据可访问的已登记外部知识源。"""
        if identity.delegated_scope:
            try:
                return knowledge_delegation.catalogue(context, identity.delegated_scope)
            except ValueError as exc:
                raise StandardAPIError(403, "DIFY_CREDENTIAL_REJECTED", "credential rejected") from exc
            except KnowledgeSearchUnavailable as exc:
                raise StandardAPIError(
                    502, "DIFY_KNOWLEDGE_UNAVAILABLE", "knowledge unavailable", retryable=True
                ) from exc
        if identity.candidate_scope:
            try:
                return candidate_knowledge.catalogue(context, identity.candidate_scope)
            except candidate_knowledge.CandidateKnowledgeError as exc:
                raise StandardAPIError(exc.status, exc.code, str(exc)) from exc
        sources = deps.knowledge_sources(context.settings, context.store)
        bindings = sources.visible(
            identity, fetch_libraries=deps.libraries_for_tenant(context.settings, context.runtime)
        )
        items = [
            {
                "knowledge_id": binding.knowledge_id,
                "name": binding.display_name or binding.knowledge_id,
                "knowledge_base_ids": list(binding.knowledge_base_ids),
            }
            for binding in bindings
        ]
        return {"knowledge_items": items, "total": len(items)}

    @app.post("/v1/dify/publish")
    def dify_publish(
        payload: DifyPublishRequest,
        _: None = Depends(deps.dify_publish_credential),  # noqa: B008
        company_user_id: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        company_username: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        dify_account_id: Annotated[str | None, Header(alias="X-AIOps-Dify-Account")] = None,
    ) -> dict[str, Any]:
        """将已绑定 Dify 应用当前内容冻结为新的正式发布版本。"""
        return api._publish_bound_app(
            context,
            payload.app_id,
            initiator=api._publish_initiator(company_user_id, company_username, dify_account_id),
        )

    @app.post("/v1/dify/retract")
    def dify_retract(
        payload: DifyRetractRequest,
        _: None = Depends(deps.dify_publish_credential),  # noqa: B008
        company_user_id: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        company_username: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        dify_account_id: Annotated[str | None, Header(alias="X-AIOps-Dify-Account")] = None,
    ) -> dict[str, Any]:
        """撤回已绑定应用的正式版本并收敛在飞运行。"""
        binding = next(
            (item for item in context.runtime.dify_app_registry.all() if item.app_id == payload.app_id),
            None,
        )
        initiator = api._publish_initiator(company_user_id, company_username, dify_account_id)
        if binding is None:
            api._record_publish_audit(
                context,
                app_id=payload.app_id,
                action="refused",
                reason_code="DIFY_APP_NOT_BOUND",
                initiator=initiator,
            )
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_APP_NOT_BOUND", "app is not a bound agent source"
            )
        database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
        environment = str(getattr(context.settings, "execution_environment", "") or "")
        if not database or not environment:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "CHAT_ROUTE_BINDINGS_DISABLED",
                "route bindings are not managed on this server",
            )
        store = chat_route_bindings.ChatRouteBindingStore(Path(database).expanduser().absolute())
        entry = str(getattr(binding, "business_entry", "") or "consumer")
        scenario = consumer_publication.route_scenario(context.settings, binding)
        current = store.get(environment, binding.tenant_id, entry, scenario)
        if current is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "CHAT_ROUTE_BINDING_MISSING", "no route to retract"
            )
        if current.app_id != payload.app_id:
            raise StandardAPIError(
                status.HTTP_409_CONFLICT, "CHAT_ROUTE_BINDING_MISMATCH", "route names another app"
            )
        try:
            retired = store.retract(
                environment=environment,
                tenant_id=binding.tenant_id,
                entry=entry,
                scenario=scenario,
                expected_revision=current.revision,
                updated_by=initiator[0] or initiator[1] or "",
            )
        except chat_route_bindings.RouteBindingError as exc:
            raise StandardAPIError(status.HTTP_409_CONFLICT, exc.code, "route was not retracted") from exc
        settled: dict[str, Any] = {"settled_runs": []}
        with contextlib.suppress(Exception):
            settled = context.runtime.settle_retracted_runs(route=current)
        context.operator_cpms.retract(current)
        api._record_publish_audit(
            context,
            app_id=payload.app_id,
            action="retracted",
            reason_code="",
            binding=binding,
            initiator=initiator,
            version_no=current.agent_version_no,
        )
        return {
            "app_id": payload.app_id,
            "tenant_id": binding.tenant_id,
            "state": retired.state,
            "revision": retired.revision,
            "version_no": current.agent_version_no,
            "settled_runs": settled["settled_runs"],
        }

    @app.post("/v1/dify/restore")
    def dify_restore(
        payload: DifyRestoreRequest,
        _: None = Depends(deps.dify_publish_credential),  # noqa: B008
        initiator: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
    ) -> dict[str, Any]:
        try:
            return api._restore_bound_app(
                context, payload.app_id, payload.version_no, (initiator or "", "", "")
            )
        except StandardAPIError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不可验证历史版本必须拒绝
            raise StandardAPIError(409, "DIFY_VERSION_UNAVAILABLE", "version not available") from exc

    @app.post("/v1/dify/versions")
    def dify_versions(
        payload: DifyPublishRequest,
        _: None = Depends(deps.dify_publish_credential),  # noqa: B008
    ) -> dict[str, Any]:
        binding, row, workspace = api._managed_app_route(context, payload.app_id)
        with context.agent_manager.store._connection() as connection:
            rows = connection.execute(
                "SELECT version_no, snapshot_json FROM agent_versions WHERE agent_id=? AND tenant_id=?",
                (row.agent_id, binding.tenant_id),
            ).fetchall()
        versions = []
        for version in rows:
            snapshot = json.loads(version["snapshot_json"])
            if snapshot.get("app_id") == payload.app_id and snapshot.get("workspace_id") == workspace:
                versions.append({"version_no": version["version_no"]})
        return {"app_id": payload.app_id, "versions": versions, "state": row.state}

    @app.post("/v1/operator/publish")
    def operator_publish(
        payload: OperatorPublishRequest,
        identity: tuple[ScopeContext, Any] = Depends(deps.operator_diagnosis_identity),  # noqa: B008
        business_entry: Annotated[str | None, Header(alias="X-Business-Entry")] = None,
    ) -> dict[str, Any]:
        """运营通过已验证公司会话自助触发本租户绑定应用的发布。"""
        caller, _decision = identity
        if not is_operator_entry(business_entry):
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "OPERATOR_ENTRY_REQUIRED",
                "publishing requires the operator entry",
            )
        registered_name, configured = context.runtime._registered_agent(
            caller.effective_tenant_id, "operator"
        )
        if not configured or registered_name is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "OPERATOR_NOT_BOUND",
                "this tenant has no bound operator agent",
            )
        binding = context.runtime.dify_app_registry.lookup(caller.effective_tenant_id, "operator")
        if binding is None or binding.app_id != payload.app_id:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND, "DIFY_APP_NOT_BOUND", "app is not a bound agent source"
            )
        return api._publish_bound_app(
            context,
            payload.app_id,
            initiator=(str(getattr(caller, "subject", None) and caller.subject.b_user_id or ""), "", ""),
        )

    @app.get("/v1/dify/workspace")
    def dify_workspace(
        request: Request,
        tenant_id: str,
        _: None = Depends(deps.dify_workspace_credential),  # noqa: B008
    ) -> dict[str, Any]:
        """按本机部署环境与租户反查已登记的 Dify workspace id（只查不建）。"""
        if "environment" in request.query_params:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST,
                "DIFY_WORKSPACE_ENVIRONMENT_NOT_ACCEPTED",
                "environment comes from this deployment's identity, not from the request",
            )
        try:
            workspace_id = context.dify_workspaces.resolve(tenant_id, context.settings.environment)
        except DifyWorkspaceError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc
        except ValueError as exc:
            raise StandardAPIError(
                status.HTTP_400_BAD_REQUEST, "DIFY_WORKSPACE_TENANT_INVALID", str(exc)
            ) from exc
        return {"workspace_id": workspace_id, "environment": context.settings.environment}

    @app.post("/v1/dify/management/trial", status_code=202)
    def managed_consumer_trial(
        payload: ManagedTrialRequest,
        request: Request,
        _: None = Depends(deps.dify_trial_credential),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller = managed_trial_caller(request, payload.app_id)
        if payload.order_no:
            api._require_visible_order(context, caller, payload.order_no)
        draft = api._workflow_trial_draft(context, payload.app_id, caller, payload.question, payload.order_no)
        if not isinstance(draft, consumer_trial.ConsumerTrial):
            raise StandardAPIError(409, "TRIAL_TARGET_INVALID", "当前应用不是客户端客服编排")

        def authorize():
            current = managed_trial_caller(request, payload.app_id)
            if current.scope_fingerprint != caller.scope_fingerprint:
                raise StandardAPIError(403, "TRIAL_SCOPE_MISMATCH", "预览期间登录或授权已变化")

        draft.authorize = authorize
        trial = context.runtime.start_trial(
            caller.subject.b_user_id, payload.order_no, draft, language, run_context=caller
        )
        if trial is None:
            raise StandardAPIError(429, "TRIAL_BUSY", "已有预览正在执行", retryable=True)
        return {"trial_id": trial["diagnosis_id"], "status": trial["status"]}

    @app.post("/v1/dify/management/trial/result")
    def read_managed_consumer_trial(
        payload: ManagedTrialResultRequest,
        request: Request,
        _: None = Depends(deps.dify_trial_credential),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, trial = managed_trial_result(payload, request)
        if trial.get("result"):
            api._require_workflow_trial_result(context, caller, trial)
        return {
            "trial_id": payload.trial_id,
            "status": trial["status"],
            "result": trial.get("result"),
            "error_code": trial.get("error_code"),
            "error_message": consumer_intent.trial_message(trial["error_code"], language)
            if trial.get("error_code")
            else None,
        }

    @app.post("/v1/dify/management/trial/cancel")
    def cancel_managed_consumer_trial(
        payload: ManagedTrialResultRequest,
        request: Request,
        _: None = Depends(deps.dify_trial_credential),  # noqa: B008
    ) -> dict[str, Any]:
        caller, _trial = managed_trial_result(payload, request)
        result = context.runtime.cancel_trial(caller, payload.trial_id)
        return {"trial_id": payload.trial_id, "status": result["status"]}

    @app.post("/v1/dify/trial", status_code=status.HTTP_202_ACCEPTED)
    def dify_trial(
        payload: DifyTrialRequest,
        request: Request,
        _: None = Depends(deps.dify_trial_credential),  # noqa: B008
        initiator: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        initiator_tenant: Annotated[str | None, Header(alias="X-AIOps-Initiator-Tenant")] = None,
        initiator_type: Annotated[str | None, Header(alias="X-AIOps-Initiator-Type")] = None,
        initiator_name: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """在受控范围内试运行草稿，不产生正式版本、不修改生产路由。"""
        if payload.app_id:
            bound = self_service_config.app_binding(context.settings, payload.app_id)
            target = node_publication.target_for(context.settings, bound) if bound is not None else None
            if target is not None and target.entry == "operator":
                return _start_operator_preview_trial(context, api, payload, request, target, language)
        if payload.operator_preview is not None or payload.conversation_id is not None:
            raise StandardAPIError(403, "OPERATOR_PREVIEW_UNBOUND", "operator preview target not registered")
        if not payload.order_no:
            raise StandardAPIError(400, "TRIAL_ORDER_REQUIRED", "this trial requires an order")
        trial_initiator = TrialInitiator.from_headers(
            initiator, initiator_tenant, initiator_type, initiator_name
        )
        run_context = api._trial_run_context(context, trial_initiator)
        api._require_visible_order(context, run_context, payload.order_no)
        try:
            draft = api._workflow_trial_draft(
                context, payload.app_id, run_context, payload.question, payload.order_no
            )
        except TrialRefused as exc:
            raise StandardAPIError(400, "TRIAL_DRAFT_REJECTED", str(exc)) from exc
        try:
            trial = context.runtime.start_trial(
                (initiator or "").strip() or "unknown",
                payload.order_no,
                draft,
                language,
                trial_initiator=trial_initiator,
            )
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable("TRIAL_UNAVAILABLE", "trial unavailable", exc) from exc
        if trial is None:
            raise StandardAPIError(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "TRIAL_BUSY",
                "another trial is already running",
                retryable=True,
            )
        return {"trial_id": trial["diagnosis_id"], "status": trial["status"]}

    @app.post("/v1/dify/trial/{trial_id}/cancel")
    def cancel_operator_trial(
        trial_id: str,
        payload: OperatorTrialCancelRequest,
        request: Request,
        _: None = Depends(deps.dify_trial_credential),  # noqa: B008
    ) -> dict[str, Any]:
        caller, _bearer = api.authenticated_operator_console(
            context, request.headers.get("X-AIOps-Console-Session", "")
        )
        run = context.agent_run_store.by_qa(trial_id)
        task = context.operator_cpms.tasks.get(run.run_id) if run else None
        binding = (
            task.binding
            if task
            else (chat_execution.binding_for_key(context.settings, run.chat_binding) if run else None)
        )
        if (
            run is None
            or run.scope_fingerprint != caller.scope_fingerprint
            or binding is None
            or binding.app_id != payload.app_id
            or not ((task and task.trial) or (run.snapshot or {}).get("trial"))
        ):
            raise StandardAPIError(404, "TRIAL_NOT_FOUND", "trial not found")
        qa = context.runtime.cancel_assistant_qa(caller, trial_id)
        context.operator_cpms.progress(caller, trial_id, qa)
        return {"trial_id": trial_id, "status": qa["status"]}

    @app.get("/v1/dify/trial/{trial_id}")
    def get_dify_trial(
        trial_id: str,
        request: Request,
        _: None = Depends(deps.dify_trial_credential),  # noqa: B008
        initiator: Annotated[str | None, Header(alias="X-AIOps-Initiator")] = None,
        initiator_tenant: Annotated[str | None, Header(alias="X-AIOps-Initiator-Tenant")] = None,
        initiator_type: Annotated[str | None, Header(alias="X-AIOps-Initiator-Type")] = None,
        initiator_name: Annotated[str | None, Header(alias="X-AIOps-Initiator-Name")] = None,
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """读取试运行状态与结果（支持管家 qa_ 试运行与工作流 tr_ 试运行）。"""
        if trial_id.startswith("qa_"):
            caller, bearer = api.authenticated_operator_console(
                context, request.headers.get("X-AIOps-Console-Session", "")
            )
            qa = context.store.get_assistant_question(trial_id, caller.scope_fingerprint)
            if qa is None or not str(qa.get("internal_run_id", "")).startswith(operator_cpms.PREFIX):
                raise StandardAPIError(404, "TRIAL_NOT_FOUND", "trial not found")
            progress = context.operator_cpms.progress(caller, trial_id, qa)
            result = None
            if qa["status"] == "completed":
                rendered = context.operator_cpms.authorize_readback(caller, trial_id, bearer)
                result = {"report": {"summary": rendered["blocks"][0]["text"]}}
            return {
                "trial_id": trial_id,
                "status": qa["status"],
                "result": result,
                "error_code": qa.get("error_code"),
                "progress": progress,
                "error_message": api._qa_user_message(language, qa.get("error_code"))
                if qa.get("error_code")
                else None,
            }
        trial_initiator = TrialInitiator.from_headers(
            initiator, initiator_tenant, initiator_type, initiator_name
        )
        caller = api._trial_run_context(context, trial_initiator)
        trial = context.runtime.get_trial(caller, trial_id)
        if trial is None:
            raise StandardAPIError(status.HTTP_404_NOT_FOUND, "TRIAL_NOT_FOUND", "trial not found")
        body: dict[str, Any] = {
            "trial_id": trial["diagnosis_id"],
            "status": trial["status"],
            "order_no": trial.get("order_no", ""),
        }
        result = trial.get("result")
        if isinstance(result, dict):
            api._require_workflow_trial_result(context, caller, trial)
            body["result"] = result
        if trial.get("error_code"):
            body["error_code"] = trial["error_code"]
            body["error_message"] = consumer_intent.trial_message(trial["error_code"], language)
        return body
