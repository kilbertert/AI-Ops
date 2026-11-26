"""统一助手问答主路由：提问分派（C 端/管家端）、轮询、取消与列表。

扩展能力（健康检查/FAQ、图片与媒体代理、SSE 流/导出/反馈/重新生成）已拆分至
``routers/health_faq.py``、``routers/media.py`` 与 ``routers/assistant_actions.py``。
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.model_request import ModelRequestError
from aiops_diagnostics.common.private_files import PrivatePathError
from aiops_diagnostics.consumer import consumer_chat_service, consumer_intent, consumer_knowledge
from aiops_diagnostics.consumer.presentation import is_order_status_question
from aiops_diagnostics.conversation.clarification import (
    CLARIFY_ORDER_REQUIRED,
    CLARIFY_WRONG_ENTRY,
)
from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.execution import chat_execution, node_publication
from aiops_diagnostics.gateway import request_observation
from aiops_diagnostics.gateway.dependencies import (
    STANDARD_DIAGNOSIS_SCOPE,
    GatewayDeps,
    request_language,
)
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import AssistantQuestionRequest
from aiops_diagnostics.identity.caller_auth import SOURCE_KEY_HEADER
from aiops_diagnostics.identity.order_identity import (
    extract_order_identifier,
    order_identifier_candidates,
)
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import ScopeContext, ScopeError
from aiops_diagnostics.operator import attachments as attachments_module
from aiops_diagnostics.operator import operator_cpms
from aiops_diagnostics.publication import agent_content
from aiops_diagnostics.publication.shortcut_lifecycle import (
    OPERATIONS_REVIEW_CODE,
    ShortcutError,
)


def _check_shortcut_guards(
    context: Any,
    api: Any,
    caller: ScopeContext,
    decision: Any,
    payload: AssistantQuestionRequest,
    language: str,
) -> tuple[Any, dict[str, Any] | None]:
    """核对已点击的快捷动作：跳转动作与缺单号的订单类快捷动作在此短路澄清。"""
    shortcut = None
    if not payload.shortcut_code:
        return None, None
    try:
        shortcut = context.shortcut_manager.store.find_effective_by_code(
            caller.effective_tenant_id, str(decision.platform), payload.shortcut_code
        )
    except ShortcutError:
        shortcut = None
    if shortcut is not None and shortcut.status == "published":
        if shortcut.jump_path:
            return shortcut, api._clarification(
                context,
                caller,
                decision,
                code=CLARIFY_WRONG_ENTRY,
                question=payload.question,
                language=language,
            )
        if (
            shortcut.requires_order
            and not payload.order_no
            and extract_order_identifier(payload.question) is None
        ):
            return shortcut, api._clarification(
                context,
                caller,
                decision,
                code=CLARIFY_ORDER_REQUIRED,
                question=payload.question,
                language=language,
            )
    return shortcut, None


def _try_operator_cpms_route(
    context: Any,
    api: Any,
    caller: ScopeContext,
    payload: AssistantQuestionRequest,
    conversation: dict[str, Any] | None,
    language: str,
    request: Request,
    *,
    attachments: list[dict[str, Any]] | None = None,
) -> JSONResponse | None:
    """管家端受控工具循环分派（Java 执行节点）；未命中绑定时返回 None。"""
    try:
        binding = chat_execution.select_binding(
            context.settings, caller, scenario=operator_cpms.SCENARIO, entry="operator"
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is None:
        return None

    api._require_operator_company_token(request)
    try:
        resolved_agent_id = agent_content.resolve_operator_agent_id(payload.agent_id)
    except agent_content.UnknownAssistantAgentError as exc:
        raise StandardAPIError(
            status.HTTP_404_NOT_FOUND, "AGENT_NOT_FOUND", "assistant agent not found"
        ) from exc
    conversation = api._operator_conversation(
        context, caller, binding, conversation, trial=False, agent_id=resolved_agent_id
    )
    order = payload.order_no or ""
    target = next(
        (
            r
            for r in node_publication.all_targets(context.settings)
            if r.entry == "operator"
            and r.app_id == binding.app_id
            and api._tenant_in_scope(r.tenant_id, caller.effective_tenant_id)
            and r.environment == binding.environment
        ),
        None,
    )
    if target is None:
        raise operator_cpms.OperatorRefused("OPERATOR_TARGET_MISSING")
    authorization = request.headers.get("Authorization")
    source = request.headers.get(SOURCE_KEY_HEADER)

    def authenticated_operator_task():
        authenticated = api._authenticate_caller(
            context.caller_resolver,
            authorization,
            None,
            required_scope=STANDARD_DIAGNOSIS_SCOPE,
            platform_entry="operator",
            source_key=source,
        )
        return context.operator_cpms.refresh_scope(authenticated, allow_empty=not order)

    resolved_attachments = list(attachments or [])
    attachment_ids = [str(item["file_id"]) for item in resolved_attachments]
    claim_id = "clm_" + uuid.uuid4().hex if conversation is not None else None
    turn_no = api._begin_conversation_turn(
        context,
        caller,
        conversation,
        "qa",
        payload.question,
        attachment_ids=attachment_ids or None,
        claim_id=claim_id,
    )
    try:
        qa = context.runtime.start_operator_cpms(
            context.operator_cpms,
            caller,
            binding,
            order,
            payload.question,
            bearer=(authorization or "").removeprefix("Bearer "),
            authorize=authenticated_operator_task,
            base=target.cpms_base_url,
            proxy=target.proxy_base_url,
            language=language,
            agent_id=resolved_agent_id,
            attachments=resolved_attachments,
            conversation_turn=(conversation["conversation_id"], caller.scope_fingerprint, turn_no)
            if conversation is not None and turn_no is not None
            else None,
            claim_id=claim_id,
        )
    except Exception:
        api._release_conversation_turn(context, conversation, turn_no, claim_id=claim_id)
        raise

    return JSONResponse(
        status_code=202,
        headers={"Cache-Control": "no-store"},
        content={
            **api._assistant_question_response(qa, language),
            "attachments": [attachments_module.public_file_dict(item) for item in resolved_attachments],
            **(
                {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                if conversation
                else {}
            ),
        },
    )


def _resolve_legacy_consumer_chatflow(
    context: Any,
    api: Any,
    caller: ScopeContext,
    service_binding: Any,
    payload: AssistantQuestionRequest,
    conversation: dict[str, Any] | None,
    language: str,
    request: Request,
) -> tuple[JSONResponse | None, str, str, bool, Any]:
    """历史 callback_version 1–5 的安全/转人工短路、意图分类与订单解析。

    返回 ``(early_response, live_order, data_kind, uses_context, previous)``。
    """
    prompt_notices = service_binding.callback_version >= 5
    notice_kind = ""
    if service_binding.content_file or service_binding.content_snapshot is not None:
        local_kind = (
            "safety"
            if consumer_knowledge.SAFETY.search(payload.question)
            else "human"
            if consumer_knowledge.HUMAN.search(payload.question)
            else ""
        )
        if not local_kind and conversation is not None:
            try:
                followup = consumer_knowledge.followup_context(
                    context.runtime,
                    caller,
                    payload.question,
                    conversation["conversation_id"],
                    require_result=consumer_chat_service.require_result,
                )
            except chat_execution.ChatRunUnavailable as exc:
                if exc.code == "CHATFLOW_RESULT_EXPIRED":
                    return api._consumer_intent_clarification("context", language), "", "", False, None
                raise
            if followup["unresolved_count"] >= 2:
                local_kind = "human"
        if local_kind == "human" and prompt_notices:
            notice_kind = "human"
            local_kind = ""
        if local_kind:
            if payload.photo_id:
                context.runtime.consumer_photos.take(
                    payload.photo_id,
                    caller.scope_fingerprint,
                    consumer_chat_service.fingerprint(service_binding),
                )
                context.runtime.consumer_photos.finish(payload.photo_id)
            content = None
            with contextlib.suppress(ValueError, OSError, PrivatePathError):
                content = consumer_knowledge.binding_content(service_binding, caller)
            answer = consumer_knowledge.fallback(local_kind, language, content)
            early = JSONResponse(
                status_code=200,
                headers={"Cache-Control": "no-store"},
                content={
                    "type": "clarification",
                    "missing_fields": [],
                    "message": answer["blocks"][0]["text"],
                    "language": language,
                },
            )
            return early, "", "", False, None

    if payload.order_no:
        api._require_visible_order(context, caller, payload.order_no)
    previous, previous_question = consumer_chat_service.previous_context(
        context.runtime, caller, service_binding, conversation
    )
    selection = {"intent": "knowledge", "order_reference": "none", "uses_context": False}
    if not payload.photo_id and payload.shortcut_code != "smart_diagnosis":
        try:
            selection = consumer_intent.validate(
                context.runtime.classify_consumer(
                    payload.question,
                    language=language,
                    previous_question=previous_question,
                    previous_order=bool(previous and previous.order_no),
                    selected_order=bool(payload.order_no),
                )
            )
        except (ValueError, RuntimeError, OSError) as exc:
            raise api._runtime_unavailable(
                "CONSUMER_MODEL_QUOTA_EXHAUSTED"
                if isinstance(exc, ModelRequestError) and exc.reason == "quota_exhausted"
                else "CONSUMER_INTENT_UNAVAILABLE",
                consumer_intent.message("unavailable", language),
                exc,
            ) from exc
    if payload.shortcut_code == "smart_diagnosis":
        selection = {"intent": "order_status", "order_reference": "explicit", "uses_context": False}
    intent, reference = selection["intent"], selection["order_reference"]
    uses_context = selection["uses_context"] or reference == "previous"
    if previous and uses_context:
        refreshed, _ = consumer_chat_service.previous_context(
            context.runtime, caller, service_binding, conversation
        )
        if refreshed is None or refreshed.qa_id != previous.qa_id:
            return api._consumer_intent_clarification("context", language), "", "", False, None
        previous = refreshed
    if prompt_notices and intent in {"clarify", "human"}:
        notice_kind = intent
        intent, uses_context = "knowledge", False
    if prompt_notices and intent in {"order_status", "billing_record"} and reference in {"none", "unclear"}:
        notice_kind = "order_required"
        intent, uses_context = "knowledge", False
    if intent in {"write_request", "clarify"}:
        return api._consumer_intent_clarification(intent, language), "", "", False, None
    if intent in {"safety", "human", "unsupported_live"}:
        content = None
        with contextlib.suppress(ValueError, OSError, PrivatePathError):
            content = consumer_knowledge.binding_content(service_binding, caller)
        answer = consumer_knowledge.fallback(
            "realtime" if intent == "unsupported_live" else intent, language, content
        )
        early = JSONResponse(
            status_code=200,
            headers={"Cache-Control": "no-store"},
            content={
                "type": "clarification",
                "missing_fields": [],
                "language": language,
                "message": answer["blocks"][0]["text"],
            },
        )
        return early, "", "", False, None
    if uses_context and previous is None:
        return api._consumer_intent_clarification("context", language), "", "", False, None
    observation = request_observation.observation_of(request)
    if observation is not None:
        observation.classify(candidate=intent == "order_status")
        request_observation.mark_tenant(request, caller.effective_tenant_id)
    live_order = ""
    data_kind = notice_kind or ("billing" if intent == "billing_record" else "status")
    if intent in {"order_status", "billing_record"}:
        enabled = (
            service_binding.live_billing_record
            if data_kind == "billing"
            else service_binding.live_order_status
        )
        if not enabled:
            return api._consumer_intent_clarification("disabled", language), "", "", False, None
        api.authenticated_chat_order_reader(context, caller, request)
        candidates = set(order_identifier_candidates(payload.question))
        if (
            reference in {"unclear", "none"}
            or len(candidates) > 1
            or (candidates and reference != "explicit")
            or (payload.order_no and reference != "explicit")
            or (payload.order_no and candidates and candidates != {payload.order_no})
        ):
            return api._consumer_intent_clarification("order", language), "", "", False, None
        live_order = payload.order_no or (next(iter(candidates), "") if reference == "explicit" else "")
        if not live_order and reference == "latest":
            with api.scoped_mysql_source(
                Settings.from_config(context.settings.server_config_file),
                resolve_query_scope(caller),
            ) as source:
                rows = source.list_orders(page=1, page_size=1)
            if not rows:
                raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
            live_order = str(rows[0]["order_no"])
        elif not live_order and reference == "previous" and previous:
            live_order = previous.order_no
        if not live_order:
            return api._consumer_intent_clarification("order", language), "", "", False, None
        api._require_consumer_self_order(caller, "consumer", live_order)
        api._require_visible_order(context, caller, live_order)
    return None, live_order, data_kind, uses_context, previous


def _try_consumer_chatflow_route(
    context: Any,
    api: Any,
    caller: ScopeContext,
    payload: AssistantQuestionRequest,
    conversation: dict[str, Any] | None,
    language: str,
    request: Request,
) -> JSONResponse | None:
    """C 端 WF01（callback_version=6 走官方 Agent，1–5 走历史兼容分支）。"""
    try:
        service_binding = chat_execution.select_binding(
            context.settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
        )
        if service_binding is None:
            return None

        if payload.photo_id and (not service_binding.photo_enabled or payload.order_no):
            raise StandardAPIError(400, "PHOTO_NOT_ALLOWED", "photo input is unavailable for this request")
        api._require_consumer_self_order(caller, "consumer", "pending")
        if service_binding.callback_version == 6:
            api.authenticated_chat_order_reader(context, caller, request)
            if payload.order_no:
                api._require_visible_order(context, caller, payload.order_no)
            live_order, data_kind, uses_context, previous = payload.order_no or "", "agent", True, None
        else:
            early, live_order, data_kind, uses_context, previous = _resolve_legacy_consumer_chatflow(
                context, api, caller, service_binding, payload, conversation, language, request
            )
            if early is not None:
                return early

        if conversation is None:
            conversation = context.conversation_store.create(
                scope_fingerprint=caller.scope_fingerprint,
                business_entry="consumer",
                agent_version_key=consumer_chat_service.version_key(service_binding),
            )
        if conversation["agent_version_key"] != consumer_chat_service.version_key(service_binding):
            raise StandardAPIError(
                409,
                "CHATFLOW_NEW_CONVERSATION_REQUIRED",
                consumer_intent.message("conversation", language),
            )
        turn_no = api._begin_conversation_turn(context, caller, conversation, "qa", payload.question)
        try:
            qa = context.runtime.start_consumer_chatflow(
                caller,
                payload.question,
                binding=service_binding,
                conversation=conversation,
                conversation_turn_no=turn_no,
                language=language,
                order_no=live_order,
                data_kind=data_kind,
                photo_id=payload.photo_id or "",
                uses_context=uses_context,
                expected_previous_qa_id=previous.qa_id if previous else "",
            )
        except Exception:
            api._release_conversation_turn(context, conversation, turn_no)
            raise
        request_observation.mark_qa(request, qa["qa_id"])
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store"},
            content={
                **api._assistant_question_response(qa, language),
                "conversation_id": conversation["conversation_id"],
                "turn_no": turn_no,
            },
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    except (ValueError, OSError, sqlite3.Error, SourceError, ScopeError) as exc:
        raise api._runtime_unavailable("CHATFLOW_UNAVAILABLE", "chatflow unavailable", exc) from exc


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    """挂载 ``/v1/assistant/questions`` 核心创建、轮询、取消与列表路由。"""
    selected_settings = context.settings

    @app.post("/v1/assistant/questions")
    def assistant_questions(
        payload: AssistantQuestionRequest,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> Any:
        """统一助手问答入口，按当前平台与已发布绑定确定性分派：

        1. 快捷动作守卫（跳转动作拒入问答、需单号快捷动作缺单号时澄清）；
        2. 管家端已绑定 CPMS 工具循环 → Java 执行节点（202 异步 QA）；
        3. C 端已启用 WF01 绑定 → callback 6 官方 Agent 或 callback 1–5 兼容链（202 异步 QA）；
        4. C 端确定性本人订单状态直读（启用 ``consumer_order_chat_enabled`` 时）；
        5. 管家运维复盘快捷入口（``OPERATIONS_REVIEW_CODE``）；否则 409 明确拒绝。
        """
        caller, decision = identity
        if str(decision.platform) == "operator":
            try:
                agent_content.resolve_operator_agent_id(payload.agent_id)
            except agent_content.UnknownAssistantAgentError as exc:
                raise StandardAPIError(
                    status.HTTP_404_NOT_FOUND,
                    "AGENT_NOT_FOUND",
                    "assistant agent not found",
                ) from exc
        elif payload.agent_id is not None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "AGENT_NOT_FOUND",
                "assistant agent not found",
            )
        if selected_settings.consumer_order_chat_enabled:
            response.headers["Cache-Control"] = "no-store"

        request_observation.classify_request(
            request,
            consumer_order_chat_enabled=selected_settings.consumer_order_chat_enabled,
            platform=str(decision.platform),
            shortcut_code=payload.shortcut_code,
            language=language,
            question=payload.question,
            is_status_question=is_order_status_question,
        )
        request_observation.mark_tenant(request, caller.effective_tenant_id)

        conversation: dict[str, Any] | None = None
        if payload.conversation_id:
            conversation = api._resolve_conversation(context, caller, decision, payload.conversation_id)

        shortcut, guarded = _check_shortcut_guards(context, api, caller, decision, payload, language)
        if guarded is not None:
            return guarded

        resolved_attachments: list[dict[str, Any]] = []
        if payload.attachment_ids:
            if str(decision.platform) != "operator":
                raise StandardAPIError(
                    status.HTTP_403_FORBIDDEN,
                    "PLATFORM_FORBIDDEN",
                    "attachments are only supported on operator entry",
                )
            try:
                resolved_attachments = attachments_module.resolve_turn_attachments(
                    context.assistant_files,
                    payload.attachment_ids,
                    caller.scope_fingerprint,
                    business_entry="operator",
                )
            except attachments_module.AttachmentError as exc:
                raise StandardAPIError(
                    exc.http_status, exc.code, exc.message, retryable=exc.retryable
                ) from exc

        if str(decision.platform) == "operator" and payload.shortcut_code != OPERATIONS_REVIEW_CODE:
            operator_response = _try_operator_cpms_route(
                context,
                api,
                caller,
                payload,
                conversation,
                language,
                request,
                attachments=resolved_attachments,
            )
            if operator_response is not None:
                return operator_response

        if selected_settings.consumer_chatflow_enabled and str(decision.platform) == "consumer":
            consumer_response = _try_consumer_chatflow_route(
                context, api, caller, payload, conversation, language, request
            )
            if consumer_response is not None:
                return consumer_response

        if (
            selected_settings.consumer_order_chat_enabled
            and str(decision.platform) == "consumer"
            and payload.shortcut_code in {None, "", "smart_diagnosis"}
            and is_order_status_question(payload.question)
        ):
            return api._consumer_order_chat(
                context, caller, decision, payload, conversation, language, request
            )

        if payload.photo_id:
            raise StandardAPIError(404, "PHOTO_UNAVAILABLE", "photo input unavailable")

        if payload.shortcut_code == OPERATIONS_REVIEW_CODE and shortcut is not None:
            return api._start_operations_review(context, caller, decision, payload, language=language)
        raise StandardAPIError(409, "CHAT_ROUTE_UNAVAILABLE", "business execution route is not configured")

    @app.get("/v1/assistant/questions/{qa_id}")
    def get_assistant_question(
        qa_id: str,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        caller, _ = identity
        try:
            qa = context.runtime.get_assistant_qa(caller, qa_id)
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
        if qa is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "QA_NOT_FOUND",
                "assistant question not found",
            )
        api._require_current_chat_order(context, caller, qa, request)
        response.headers["Cache-Control"] = "no-store"
        progress = context.operator_cpms.progress(caller, qa_id, qa)
        qa_attachment_ids = list(qa.get("attachment_ids") or [])
        qa_attachments = (
            [
                attachments_module.public_file_dict(row)
                for row in context.assistant_files.get_many(
                    qa_attachment_ids, caller.scope_fingerprint, business_entry="operator"
                )
            ]
            if qa_attachment_ids
            else []
        )
        return {
            **api._assistant_question_response(qa, language),
            "attachments": qa_attachments,
            "feedback": context.assistant_feedback.get(qa_id, caller.scope_fingerprint),
            "progress": progress,
        }

    @app.post("/v1/assistant/questions/{qa_id}/cancel")
    def cancel_assistant_question(
        qa_id: str,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> dict[str, Any]:
        """停止一条在飞的助手问答作业，保留终态记录用于刷新与审计。"""
        caller, _ = identity
        try:
            qa = context.runtime.cancel_assistant_qa(caller, qa_id)
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
        if qa is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "QA_NOT_FOUND",
                "assistant question not found",
            )
        api._require_current_chat_order(context, caller, qa, request)
        response.headers["Cache-Control"] = "no-store"
        progress = context.operator_cpms.progress(caller, qa_id, qa)
        if str(qa.get("status")) == "cancelled" and not context.qa_events.has_terminal_event(qa_id):
            elapsed = (progress or {}).get("elapsed_ms") or 0
            context.qa_events.append(
                qa_id,
                "run_cancelled",
                {"status": "cancelled", "elapsed_ms": int(elapsed)},
            )
        return api._assistant_question_response(qa, language)

    @app.get("/v1/assistant/questions")
    def list_assistant_questions(
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        caller, _ = identity
        try:
            questions = context.runtime.list_assistant_qa(caller, limit=limit)
        except (ValueError, RuntimeError) as exc:
            raise api._runtime_unavailable("QA_UNAVAILABLE", "general answer unavailable", exc) from exc
        for qa in questions:
            if str(qa.get("internal_run_id") or "").startswith(operator_cpms.PREFIX):
                try:
                    api._require_current_chat_order(context, caller, qa, request)
                except (operator_cpms.OperatorRefused, chat_execution.ChatRunUnavailable):
                    qa["result"] = None
        response.headers["Cache-Control"] = "no-store"
        return {
            "type": "qa_list",
            "language": language,
            "count": len(questions),
            "questions": questions,
        }
