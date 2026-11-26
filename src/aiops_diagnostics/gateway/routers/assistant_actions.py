"""助手问答扩展动作路由：SSE 实时流、报告导出、点赞/点踩反馈与原地重新生成。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from aiops_diagnostics.common.async_job_lifecycle import QUESTION
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.conversation.conversation_store import (
    ConversationBusy,
    ConversationNotFound,
    RegenerateNotAllowed,
)
from aiops_diagnostics.execution import chat_execution, node_publication
from aiops_diagnostics.gateway.dependencies import STANDARD_DIAGNOSIS_SCOPE, GatewayDeps, request_language
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.schemas import (
    AssistantQuestionExportRequest,
    AssistantQuestionFeedbackRequest,
    AssistantQuestionRegenerateRequest,
)
from aiops_diagnostics.identity.caller_auth import SOURCE_KEY_HEADER
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.operator import (
    attachments,
    download_center,
    operator_cpms,
    qa_events,
    report_export,
)


def register(app: FastAPI, context: Any, deps: GatewayDeps, api: Any) -> None:
    """挂载 ``/v1/assistant/questions/{qa_id}/{events,export,feedback,regenerate}`` 路由。"""

    @app.get("/v1/assistant/questions/{qa_id}/events")
    async def stream_assistant_question_events(
        qa_id: str,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
        after_seq: Annotated[int | None, Query(ge=0)] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """订阅问答 SSE 实时事件流：节点路径 + 正文打字机增量 + 引用 + 复核替换 + 断线续传。"""
        caller, _ = identity
        cursor = int(after_seq or 0)
        if last_event_id is not None and last_event_id.strip():
            try:
                parsed_id = int(last_event_id.strip())
                if parsed_id < 0:
                    raise ValueError("negative")
                cursor = max(cursor, parsed_id)
            except ValueError as exc:
                raise StandardAPIError(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "INVALID_REQUEST",
                    "Last-Event-ID must be a non-negative integer",
                ) from exc

        def _authorize_stream_access(target_qa: dict[str, Any]) -> None:
            api._require_current_chat_order(context, caller, target_qa, request)
            if (
                str(target_qa.get("internal_run_id") or "").startswith(operator_cpms.PREFIX)
                and target_qa.get("status") != "completed"
            ):
                api._require_operator_company_token(request)
                context.operator_cpms.verify_live_stream_access(
                    caller,
                    target_qa["qa_id"],
                    request.headers.get("Authorization", "").removeprefix("Bearer "),
                )

        qa = await run_in_threadpool(context.runtime.get_assistant_qa, caller, qa_id)
        if qa is None:
            raise StandardAPIError(
                status.HTTP_404_NOT_FOUND,
                "QA_NOT_FOUND",
                "assistant question not found",
            )
        await run_in_threadpool(_authorize_stream_access, qa)
        heartbeat = float(getattr(context, "sse_heartbeat_seconds", qa_events.SSE_HEARTBEAT_SECONDS))

        def _synthesize_terminal(current_qa: dict[str, Any], seq_num: int) -> str:
            status_val = str(current_qa["status"])
            shaped = api._assistant_question_response(current_qa, language)
            if status_val == QUESTION.cancelled:
                return qa_events.format_sse_frame(
                    "run_cancelled",
                    {"seq": seq_num, "status": "cancelled", "elapsed_ms": 0},
                    event_id=seq_num,
                )
            if status_val in {QUESTION.failed, QUESTION.expired}:
                return qa_events.format_sse_frame(
                    "run_failed",
                    {
                        "seq": seq_num,
                        "status": status_val,
                        "elapsed_ms": 0,
                        "error": shaped["error"],
                    },
                    event_id=seq_num,
                )
            result = current_qa.get("result") or {}
            citations = list(result.get("citations") or [])
            return qa_events.format_sse_frame(
                "message_done",
                {
                    "seq": seq_num,
                    "status": "completed",
                    "elapsed_ms": 0,
                    "delivery_mode": current_qa.get("delivery_mode") or "diagnosis",
                    "reasoning_duration_ms": 0,
                    "step_count": 0,
                    "citation_count": len(citations),
                    "result": result,
                },
                event_id=seq_num,
            )

        async def _event_stream():
            nonlocal cursor
            saw_terminal = False
            first_iteration = True
            while True:
                if not first_iteration:
                    current_qa = await run_in_threadpool(context.runtime.get_assistant_qa, caller, qa_id)
                    if current_qa is None:
                        return
                    try:
                        await run_in_threadpool(_authorize_stream_access, current_qa)
                    except Exception:  # noqa: BLE001 - 权限撤回立即终止流，绝不继续吐草稿
                        return
                else:
                    current_qa = qa
                    first_iteration = False
                batch = await run_in_threadpool(context.qa_events.list_after, qa_id, after_seq=cursor)
                for item in batch:
                    cursor = item["seq"]
                    yield qa_events.format_sse_frame(item["event"], item["data"], event_id=item["seq"])
                    if item["event"] in qa_events.TERMINAL_EVENT_TYPES:
                        saw_terminal = True
                if saw_terminal:
                    return
                current_qa = await run_in_threadpool(context.runtime.get_assistant_qa, caller, qa_id)
                if current_qa is None:
                    return
                try:
                    await run_in_threadpool(_authorize_stream_access, current_qa)
                except Exception:  # noqa: BLE001 - 权限撤回立即终止流
                    return
                if str(current_qa["status"]) not in QUESTION.active:
                    # 补发最新落库事件；若已有事件流，短暂等待终局复核帧原子落库
                    tail = await run_in_threadpool(context.qa_events.list_after, qa_id, after_seq=cursor)
                    for item in tail:
                        cursor = item["seq"]
                        yield qa_events.format_sse_frame(item["event"], item["data"], event_id=item["seq"])
                        if item["event"] in qa_events.TERMINAL_EVENT_TYPES:
                            return
                    if await run_in_threadpool(context.qa_events.has_any_events, qa_id):
                        await context.qa_events.wait_for_update(qa_id, timeout=1.0)
                        tail_after_wait = await run_in_threadpool(
                            context.qa_events.list_after, qa_id, after_seq=cursor
                        )
                        for item in tail_after_wait:
                            cursor = item["seq"]
                            yield qa_events.format_sse_frame(
                                item["event"], item["data"], event_id=item["seq"]
                            )
                            if item["event"] in qa_events.TERMINAL_EVENT_TYPES:
                                return
                    yield _synthesize_terminal(current_qa, cursor + 1)
                    return
                if await request.is_disconnected():
                    return
                woke = await context.qa_events.wait_for_update(qa_id, timeout=heartbeat)
                if not woke:
                    if await request.is_disconnected():
                        return
                    yield qa_events.format_sse_frame("ping", {"ts": format_stored_time(now_utc())})

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/v1/assistant/questions/{qa_id}/export")
    def export_assistant_question_report(
        qa_id: str,
        payload: AssistantQuestionExportRequest,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """将已完成的后台排查结果导出为 .docx / .md 报告并投递至公司下载中心导出列表。"""
        caller, decision = identity
        if str(decision.platform) != "operator":
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "PLATFORM_FORBIDDEN",
                "operator entry is required",
            )
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
        if str(qa.get("status")) != "completed" or not qa.get("result"):
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "REPORT_NOT_READY",
                "diagnosis answer is not completed and cannot be exported",
            )
        progress = context.operator_cpms.progress(caller, qa_id, qa)
        steps = list((progress or {}).get("steps") or [])
        citations = list((qa.get("result") or {}).get("citations") or [])
        file_name, content_type, report_bytes = report_export.render_diagnosis_report(
            qa=qa,
            fmt=payload.format,
            steps=steps,
            citations=citations,
        )
        user_id = str(getattr(caller.subject, "b_user_id", "") or "")
        username = user_id or "operator"
        try:
            _, record_id = context.download_center.upload_and_register_export(
                tenant_id=caller.effective_tenant_id,
                user_id=user_id,
                username=username,
                file_name=file_name,
                file_bytes=report_bytes,
                content_type=content_type,
            )
        except download_center.DownloadCenterError as exc:
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc
        response.headers["Cache-Control"] = "no-store"
        return {
            "download_center_record_id": record_id,
            "direction": -1,
            "status": "ready",
            "file_name": file_name,
            "format": payload.format,
            "notify_file_center": True,
        }

    @app.post("/v1/assistant/questions/{qa_id}/feedback")
    def submit_assistant_question_feedback(
        qa_id: str,
        payload: AssistantQuestionFeedbackRequest,
        response: Response,
        request: Request,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
    ) -> dict[str, Any]:
        """提交或撤销回答点赞/点踩反馈（幂等 upsert）。"""
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
        if str(qa.get("status")) != "completed":
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "FEEDBACK_NOT_ALLOWED",
                "feedback is only allowed on completed answers",
            )
        saved = context.assistant_feedback.upsert(
            qa_id,
            caller.scope_fingerprint,
            rating=payload.rating,
            reasons=payload.reasons,
            comment=payload.comment,
        )
        response.headers["Cache-Control"] = "no-store"
        return saved

    @app.post("/v1/assistant/questions/{qa_id}/regenerate")
    def regenerate_assistant_question(
        qa_id: str,
        request: Request,
        payload: AssistantQuestionRegenerateRequest | None = None,
        identity: tuple[ScopeContext, Any] = Depends(deps.assistant_identity),  # noqa: B008
        language: str = Depends(request_language),  # noqa: B008
    ) -> JSONResponse:
        """仅允许对会话最后一轮已完成问答原地重新生成（复用原问题与原附件，排除旧回答上下文）。"""
        del payload
        caller, decision = identity
        if str(decision.platform) != "operator":
            raise StandardAPIError(
                status.HTTP_403_FORBIDDEN,
                "PLATFORM_FORBIDDEN",
                "operator entry is required",
            )
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
        turn = context.conversation_store.find_turn_by_qa_id(qa_id, caller.scope_fingerprint)
        if turn is None or turn.get("business_entry") != "operator":
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "REGENERATE_NOT_ALLOWED",
                "only the latest completed operator turn can be regenerated",
            )

        try:
            binding = chat_execution.select_binding(
                context.settings, caller, scenario=operator_cpms.SCENARIO, entry="operator"
            )
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
        if binding is None:
            raise StandardAPIError(
                409, "CHAT_ROUTE_UNAVAILABLE", "business execution route is not configured"
            )
        api._require_operator_company_token(request)
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

        conversation_id = str(turn["conversation_id"])
        try:
            regen = context.conversation_store.begin_regenerate_turn(
                conversation_id, caller.scope_fingerprint, expected_qa_id=qa_id
            )
        except ConversationBusy as exc:
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "CONVERSATION_BUSY",
                "conversation already has a generating turn",
            ) from exc
        except (RegenerateNotAllowed, ConversationNotFound) as exc:
            raise StandardAPIError(
                status.HTTP_409_CONFLICT,
                "REGENERATE_NOT_ALLOWED",
                "only the latest completed turn in a conversation can be regenerated",
            ) from exc

        turn_no = int(regen["turn_no"])
        claim_id = str(regen["claim_id"])
        conv_ref = {"conversation_id": conversation_id, "scope_fingerprint": caller.scope_fingerprint}
        try:
            attachment_ids = list(regen.get("attachment_ids") or [])
            resolved_attachments = (
                attachments.resolve_turn_attachments(
                    context.assistant_files,
                    attachment_ids,
                    caller.scope_fingerprint,
                    business_entry="operator",
                )
                if attachment_ids
                else []
            )
        except attachments.AttachmentError as exc:
            api._release_conversation_turn(
                context, conv_ref, turn_no, preserve_completed=True, claim_id=claim_id
            )
            raise StandardAPIError(exc.http_status, exc.code, exc.message, retryable=exc.retryable) from exc

        internal_run_id = str(qa.get("internal_run_id") or "")
        order = (
            internal_run_id.removeprefix(operator_cpms.PREFIX)
            if internal_run_id.startswith(operator_cpms.PREFIX)
            else ""
        )
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

        try:
            new_qa = context.runtime.start_operator_cpms(
                context.operator_cpms,
                caller,
                binding,
                order,
                str(regen["question"]),
                bearer=(authorization or "").removeprefix("Bearer "),
                authorize=authenticated_operator_task,
                base=target.cpms_base_url,
                proxy=target.proxy_base_url,
                language=language,
                agent_id=str(regen["agent_id"]),
                attachments=resolved_attachments,
                conversation_turn=(conversation_id, caller.scope_fingerprint, turn_no),
                attempt_no=int(regen["next_attempt_no"]),
                parent_qa_id=qa_id,
                claim_id=claim_id,
            )
        except Exception:
            api._release_conversation_turn(
                context, conv_ref, turn_no, preserve_completed=True, claim_id=claim_id
            )
            raise

        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store"},
            content={
                **api._assistant_question_response(new_qa, language),
                "attachments": [attachments.public_file_dict(item) for item in resolved_attachments],
                "conversation_id": conversation_id,
                "turn_no": turn_no,
            },
        )
