"""C 端聊天流历史回调版本（``callback_version`` 1–5）兼容逻辑。

现役统一智能体循环使用 ``callback_version == 6``（见 ``consumer_chat_service.py``、
``consumer_agent.py`` 与 ``consumer_trial.py`` 主路径）。
本模块将 1–5 版的会话续接、输入拼装、内部取数回调、旧 FAQ 检索映射、答复渲染、
回读核验与试运行兼容路径集中隔离，避免主路径反复交错历史版本分支。
"""

from __future__ import annotations

import json
import re
from typing import Any

from aiops_diagnostics.common.i18n import rendering_language
from aiops_diagnostics.consumer.consumer_binding import deliverable_binding, identity, require_pinned
from aiops_diagnostics.consumer.presentation import COMPOSED_LANGUAGES
from aiops_diagnostics.dify.dify_run_store import still_valid
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.run_deliverability import deliverable


def is_pre_picker_binding(binding: Any) -> bool:
    """``callback_version < 3`` 发布时尚无节点选库，跳过知识库目录复核。"""
    return binding.callback_version < 3


def is_pre_documents_binding(binding: Any) -> bool:
    """``callback_version < 4`` 仍使用旧 FAQ 条目匹配而非动态文档块。"""
    return binding.callback_version < 4


def select_legacy_context(
    runtime: Any,
    caller: Any,
    binding: Any,
    query: str,
    language: str,
    qa_id: str,
) -> dict[str, Any]:
    """``callback_version`` 1–3 的旧 FAQ 检索路径；v4+ 直接走 ``consumer_knowledge.select_context``。"""
    from aiops_diagnostics.consumer.consumer_knowledge import binding_content, select_context
    from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchGuard

    if not is_pre_documents_binding(binding):
        return select_context(runtime, caller, binding, query, language, qa_id)
    content = binding_content(binding, caller)
    if getattr(getattr(runtime, "gateway_settings", None), "knowledge_delegation_file", ""):
        return {"status": "unavailable", "candidates": []}
    if runtime.kb_search_client is None or runtime.media_signer is None or not binding.knowledge_base_ids:
        return {"status": "unavailable", "candidates": []}
    guard = KnowledgeSearchGuard(
        runtime.kb_search_client.for_tenant(caller.effective_tenant_id),
        tenant_id=caller.effective_tenant_id,
        agent_version=f"consumer:{binding.agent_id}#v{binding.agent_version_no}",
        knowledge_base_ids=binding.knowledge_base_ids,
        media_signer=runtime.media_signer,
        session_id=qa_id,
        max_results=5,
        max_calls=1,
    )
    return select_legacy_faq_context(runtime, caller, binding, content, guard.search(query), language, qa_id)


def select_legacy_faq_context(
    runtime: Any,
    caller: Any,
    binding: Any,
    content: dict[str, Any],
    result: Any,
    language: str,
    qa_id: str,
) -> dict[str, Any]:
    """将 ``callback_version`` 1–3 的检索块映射回旧版 ``KB-C-xxx`` 审核 FAQ 条目。"""
    candidates: dict[str, dict[str, Any]] = {}
    for chunk in result.chunks:
        title_id = re.fullmatch(r"(KB-C-\d{3})(?:\.md)?", chunk.title or "")
        ids = [title_id[1]] if title_id else []
        for key in ids:
            row = content["entries"].get(key)
            if row is None or key in candidates:
                continue
            candidates[key] = {
                "faq_id": key,
                "title": row.get("title", ""),
                "answer": row["answers"][rendering_language(row["answers"], language)],
                "reference_id": chunk.reference_id,
                "media": [
                    runtime.media_signer.issue(
                        tenant_id=caller.effective_tenant_id,
                        agent_version=f"consumer:{binding.agent_id}#v{binding.agent_version_no}",
                        session_id=qa_id,
                        knowledge_base_id=item["knowledge_base_id"],
                        document_id=item["document_id"],
                        backend_id=item["backend_id"],
                        chunk_id=None,
                        kind=item["kind"],
                        mime_type=item["mime_type"],
                        title=item["title"],
                        reference_id=chunk.reference_id,
                    ).to_dict()
                    for media_id in row.get("media_ids", [])
                    if (item := content.get("media", {})[media_id])["knowledge_base_id"]
                    in binding.knowledge_base_ids
                    and item.get("language", "any")
                    in {"any", rendering_language(COMPOSED_LANGUAGES, language)}
                ],
            }
    status = "found" if candidates else "not_found" if result.status.value == "found" else result.status.value
    return {"status": status, "candidates": list(candidates.values())[:5]}


def _require_binding(runtime: Any, caller: Any, binding: Any) -> None:
    if not runtime.gateway_settings.consumer_chatflow_enabled:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403
        )
    try:
        require_pinned(runtime, caller, binding)
    except chat_execution.ChatRunUnavailable as exc:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403
        ) from exc


def callback_context(
    runtime: Any,
    token: str,
    *,
    require_result: Any,
    read_snapshot: Any,
    image_text: str = "",
    capability: str | None = None,
    knowledge_base_ids: list[str] | None = None,
) -> dict[str, Any]:
    """``callback_version`` 1–5 专属内部取数回调；v6 统一智能体走 ``consumer_agent.callback``。"""
    from aiops_diagnostics.consumer.consumer_knowledge import SAFETY, followup_context, require_available
    from aiops_diagnostics.execution import chat_data_grant
    from aiops_diagnostics.execution.agent_runs import scope_from_json

    grants = chat_data_grant.DataGrantStore(runtime.store.path)
    grant = chat_data_grant.usable(grants.by_token(token))
    run = runtime.dify_run_store.by_qa(grant.qa_id)
    if (
        run is None
        or run.kind not in {"service", "service_trial"}
        or run.grant_id != grant.grant_id
        or run.order_no != grant.order_no
        or (run.scope_fingerprint, run.tenant_id, run.environment, run.workflow_id)
        != (grant.scope_fingerprint, grant.tenant_id, grant.environment, grant.workflow_id)
    ):
        raise chat_execution.ChatRunUnavailable(
            "GRANT_SCOPE_MISMATCH", "knowledge grant refused", http_status=403
        )
    caller = scope_from_json(run.scope_json)
    trial = None
    if run.kind == "service_trial":
        entry = runtime._trial_contexts.get(run.qa_id)
        if entry is None or getattr(entry[1], "binding", None) is None:
            raise chat_execution.ChatRunUnavailable("TRIAL_NOT_ACTIVE", "trial not active", http_status=403)
        trial = entry[1]
        binding = trial.binding
    else:
        binding = deliverable_binding(runtime, caller, run)
    if binding is None or (not binding.content_file and binding.content_snapshot is None):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "knowledge binding changed", http_status=403
        )
    if binding.callback_version == 6:
        raise chat_execution.ChatRunUnavailable(
            "GRANT_CAPABILITY_MISMATCH", "Agent授权仅限新版工具回调", http_status=403
        )
    expected = run.data_kind if run.order_no else "knowledge"
    if (binding.callback_version >= 2 and capability != expected) or (
        binding.callback_version == 1 and capability is not None
    ):
        raise chat_execution.ChatRunUnavailable(
            "GRANT_CAPABILITY_MISMATCH", "本轮授权与取数节点不一致", http_status=403
        )

    if binding.callback_version >= 3 and expected == "knowledge":
        if knowledge_base_ids != list(binding.knowledge_base_ids):
            raise chat_execution.ChatRunUnavailable(
                "KNOWLEDGE_SELECTION_MISMATCH", "节点选库与本次发布版本不一致", http_status=403
            )
    elif knowledge_base_ids is not None:
        raise chat_execution.ChatRunUnavailable(
            "KNOWLEDGE_SELECTION_UNEXPECTED", "此版本不接受节点选库参数", http_status=400
        )

    def require_current() -> None:
        if trial:
            trial.require_current(runtime.gateway_settings)
        else:
            _require_binding(runtime, caller, binding)

    def question_record() -> dict[str, Any] | None:
        if trial:
            qa = runtime.store.get_standard_diagnosis(run.qa_id, caller.scope_fingerprint)
            return {**qa, "question": trial.question} if qa else None
        return runtime.store.get_assistant_question(run.qa_id, caller.scope_fingerprint)

    require_current()

    def valid() -> None:
        if not run.order_no and run.data_kind not in {"clarify", "human", "order_required"}:
            require_available(runtime, caller, binding)
        if run.order_no:
            enabled = binding.live_billing_record if run.data_kind == "billing" else binding.live_order_status
            if not enabled:
                raise chat_execution.ChatRunUnavailable(
                    "CHATFLOW_LIVE_DISABLED", "live status disabled", http_status=403
                )
            runtime._require_chat_order_visible(caller, run.order_no)
        chat_data_grant.usable(grants.by_id(grant.grant_id))
        latest = runtime.dify_run_store.get(run.run_id)
        qa = question_record()
        if (
            latest is None
            or latest.state != "running"
            or not still_valid(latest)
            or qa is None
            or qa["status"] != "running"
        ):
            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_NOT_ACTIVE", "knowledge request ended", http_status=403
            )
        require_current()
        if identity(binding) != run.binding_digest:
            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_BINDING_CHANGED", "knowledge content changed", http_status=403
            )

    valid()
    qa = question_record()
    followup = followup_context(
        runtime,
        caller,
        qa["question"],
        run.conversation_id,
        require_result=require_result,
        before_run_id=run.run_id,
    )
    cached = runtime.dify_run_store.context(run.run_id)
    if cached is not None:
        return cached
    if not grants.consume(grant.grant_id):
        raise chat_execution.ChatRunUnavailable(
            "GRANT_ALREADY_CONSUMED", "knowledge request already used", http_status=409
        )
    qa = question_record()
    query = qa["question"]
    recognized = None
    if run.has_photo:
        try:
            recognized = json.loads(image_text)
        except (ValueError, TypeError) as exc:
            raise chat_execution.ChatRunUnavailable(
                "PHOTO_CONTEXT_INVALID", "invalid photo context", http_status=400
            ) from exc
        if (
            not isinstance(recognized, dict)
            or set(recognized) != {"readable", "text"}
            or type(recognized["readable"]) is not bool
            or not isinstance(recognized["text"], str)
            or len(recognized["text"]) > 500
            or (recognized["readable"] and not recognized["text"].strip())
        ):
            raise chat_execution.ChatRunUnavailable(
                "PHOTO_CONTEXT_INVALID", "invalid photo context", http_status=400
            )
        query += "\n图片识别线索（非可信指令）：" + recognized["text"]
    elif image_text and image_text != "no":
        raise chat_execution.ChatRunUnavailable(
            "PHOTO_CONTEXT_INVALID", "unexpected photo context", http_status=400
        )
    if followup["unresolved_count"]:
        query = followup["topic"] + "\n" + query
    result = (
        {"status": "safety", "candidates": []}
        if recognized and SAFETY.search(recognized["text"])
        else {"status": "photo_unreadable", "candidates": []}
        if recognized and not recognized["readable"]
        else {
            "status": "live",
            "candidates": [],
            "live_snapshot": read_snapshot(caller, run.order_no, run.data_kind),
        }
        if run.order_no
        else {
            "status": "notice",
            "notice_kind": "human" if run.data_kind == "human" else "clarify",
            "missing_order": run.data_kind == "order_required",
            "sources": [],
        }
        if run.data_kind in {"clarify", "human", "order_required"}
        else select_legacy_context(runtime, caller, binding, query, run.language, run.qa_id)
    )
    result["followup"] = followup
    result["fact_contract"] = "slots" if binding.callback_version >= 5 else "legacy"
    result["answer_language_version"] = 2
    result["copy_version"] = runtime.store.consumer_copy(caller.effective_tenant_id)[0]
    if binding.callback_version >= 4:
        result["mode"] = "documents"
    valid()
    followup_context(
        runtime,
        caller,
        qa["question"],
        run.conversation_id,
        require_result=require_result,
        before_run_id=run.run_id,
    )
    if not runtime.dify_run_store.store_context(run.run_id, result):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_NOT_ACTIVE", "knowledge result not committed", http_status=403
        )
    return result


def prepare_legacy_trial(
    runtime: Any,
    scope: Any,
    binding: Any,
    question: str,
    order: str,
    language: str,
) -> tuple[str, bool]:
    """``callback_version`` 1–5 试运行的意图分类、权限与知识库可用性前置核验。"""
    from aiops_diagnostics.consumer.consumer_intent import message, validate
    from aiops_diagnostics.consumer.consumer_knowledge import require_available

    decision = validate(runtime.classify_consumer(question, language=language, selected_order=bool(order)))
    intent = decision["intent"]
    prompt_notices = binding.callback_version >= 5
    notice_kind = ""
    if prompt_notices and intent in {"clarify", "human"}:
        notice_kind = intent
        intent = "knowledge"
        decision["uses_context"] = False
    if intent not in {"knowledge", "order_status", "billing_record"} or decision["uses_context"]:
        raise chat_execution.ChatRunUnavailable("TRIAL_CLARIFICATION_REQUIRED", message("clarify", language))
    live = intent in {"order_status", "billing_record"}
    kind = notice_kind or (("billing" if intent == "billing_record" else "status") if live else "knowledge")
    if live and (not order or decision["order_reference"] != "explicit"):
        raise chat_execution.ChatRunUnavailable(
            "TRIAL_ORDER_REQUIRED", "试用查询请明确使用选定订单，不支持另选最新单"
        )
    if live and not (binding.live_billing_record if kind == "billing" else binding.live_order_status):
        raise chat_execution.ChatRunUnavailable("CHATFLOW_LIVE_DISABLED", message("disabled", language))
    if order:
        runtime._require_chat_order_visible(scope, order)
    if not live:
        require_available(runtime, scope, binding)
    return kind, live


def build_legacy_trial_inputs(
    binding: Any,
    *,
    language: str,
    kind: str,
    token: str,
) -> dict[str, Any]:
    """构造 ``callback_version`` 1–5 试运行发往 Dify 的 ``inputs``。"""
    return {
        "language": language,
        "has_photo": "no",
        "data_grant": token,
        **({"data_kind": kind} if binding.callback_version >= 2 else {}),
    }


def verify_legacy_trial_pre_commit(
    runtime: Any,
    scope: Any,
    binding: Any,
    *,
    live: bool,
) -> None:
    """``callback_version`` 1–5 试运行返回后复核知识库授权。"""
    if not live:
        from aiops_diagnostics.consumer.consumer_knowledge import require_available

        require_available(runtime, scope, binding)


def finish_legacy_trial(
    *,
    live: bool,
    kind: str,
    order: str,
    context: dict[str, Any] | None,
    reply_answer: str,
    language: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    """``callback_version`` 1–5 试运行核对实时快照并生成最终 blocks 答复。"""
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    if live:
        snapshot = (context or {}).get("live_snapshot", {})
        expected_source = "cpms.orderDetail" if kind == "billing" else "cpms.chargingInfo"
        if snapshot.get("order_no") != order or snapshot.get("source") != expected_source:
            raise ValueError("trial has no matching live snapshot")
    return final_answer(reply_answer, context, language, content)


def resolve_legacy_upstream(
    runtime: Any,
    caller: Any,
    binding: Any,
    conversation: dict[str, Any],
    *,
    order_no: str,
    photo_id: str,
    uses_context: bool,
    expected_previous_qa_id: str | None,
    language: str,
    fingerprint_fn: Any,
    require_result_fn: Any,
) -> str:
    """校验 1–5 版会话连续性并决定是否继承上游 ``conversation_id``。"""
    previous = runtime.dify_run_store.latest_conversation_run(
        conversation["conversation_id"], caller.scope_fingerprint
    )
    upstream = ""
    if (
        uses_context
        and expected_previous_qa_id is not None
        and (previous.qa_id if previous else "") != expected_previous_qa_id
    ):
        from aiops_diagnostics.consumer.consumer_intent import message

        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_NEW_CONVERSATION_REQUIRED", message("context", language), http_status=409
        )
    if previous:
        previous_qa = runtime.store.get_assistant_question(previous.qa_id, caller.scope_fingerprint)
        if (
            previous_qa is None
            or previous_qa["status"] != "completed"
            or previous.state != "succeeded"
            or previous.binding_digest != fingerprint_fn(binding)
            or previous.workflow_id != binding.workflow_id
            or previous.route_revision != binding.route_revision
            or previous.agent_version_no != binding.agent_version_no
        ):
            from aiops_diagnostics.consumer.consumer_intent import message

            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_NEW_CONVERSATION_REQUIRED", message("conversation", language), http_status=409
            )
        if deliverable(previous) and uses_context:
            require_result_fn(runtime, caller, previous_qa)
            upstream = previous.upstream_conversation_id
            if not upstream:
                raise chat_execution.ChatRunUnavailable(
                    "CHATFLOW_EVIDENCE_UNAVAILABLE", "conversation evidence unavailable"
                )
        elif uses_context:
            from aiops_diagnostics.consumer.consumer_intent import message

            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_NEW_CONVERSATION_REQUIRED", message("context", language), http_status=409
            )
    # 单据轮次及其下一轮均不继承上游记忆，旧订单数据不会进入后续模型输入。
    if (
        binding.callback_version >= 4
        or order_no
        or photo_id
        or (previous and (previous.order_no or previous.has_photo))
    ):
        upstream = ""
    return upstream


def build_legacy_inputs(
    latest: Any,
    binding: Any,
    *,
    language: str,
    uploaded: str,
    token: str,
) -> dict[str, Any]:
    """构造 1–5 版 Dify chatflow 的 ``inputs`` 载荷。"""
    return {
        "language": language,
        "has_photo": "yes" if uploaded else "no",
        **(
            {"data_kind": latest.data_kind if latest.order_no else "knowledge"}
            if binding.callback_version >= 2
            else {}
        ),
        **({"data_grant": token} if token else {}),
    }


def verify_legacy_post_reply(
    runtime: Any,
    caller: Any,
    binding: Any,
    store: Any,
    run_id: str,
    latest: Any,
    registration: Any,
    *,
    fingerprint_fn: Any,
) -> None:
    """1–5 版在 Dify 返回后、提交终态前复核知识库/订单授权与快照一致性。"""
    if not latest.order_no and latest.data_kind not in {"clarify", "human", "order_required"}:
        from aiops_diagnostics.consumer.consumer_knowledge import require_available

        require_available(runtime, caller, binding)
    require_pinned(runtime, caller, binding)
    run = store.get(run_id)
    if (
        registration.cancelled
        or run is None
        or run.state != "running"
        or not still_valid(run)
        or run.binding_digest != fingerprint_fn(binding)
    ):
        raise ValueError("run is no longer active")
    if run.order_no:
        runtime._require_chat_order_visible(caller, run.order_no)
        require_pinned(runtime, caller, binding)
        run = store.get(run_id)
        if (
            run is None
            or run.state != "running"
            or not still_valid(run)
            or registration.cancelled
            or run.binding_digest != fingerprint_fn(binding)
        ):
            raise ValueError("run ended during authorization")
        live = (store.context(run_id) or {}).get("live_snapshot", {})
        if live.get("order_no") != run.order_no or live.get("source") != (
            "cpms.orderDetail" if run.data_kind == "billing" else "cpms.chargingInfo"
        ):
            raise ValueError("live snapshot missing or belongs to another order")


def build_legacy_answer(
    runtime: Any,
    caller: Any,
    binding: Any,
    store: Any,
    run_id: str,
    reply_answer: str,
    language: str,
    *,
    copy_pack_fn: Any,
) -> dict[str, Any]:
    """1–5 版将模型原始回答转为标准 blocks 响应并按需保存槽位回复源文。"""
    if not (binding.content_file or binding.content_snapshot is not None):
        return {"blocks": [{"kind": "text", "text": reply_answer}], "retrieval_status": "unavailable"}

    from aiops_diagnostics.consumer.consumer_knowledge import binding_content, final_answer

    content = binding_content(binding, caller)
    context = store.context(run_id) or {}
    answer = final_answer(
        reply_answer,
        context,
        language,
        content,
        copy_pack=copy_pack_fn(runtime, caller, context, language),
    )
    if context.get("fact_contract") == "slots" and context.get("live_snapshot"):
        from aiops_diagnostics.consumer.consumer_documents import load_selection

        if not store.store_reply_source(run_id, load_selection(reply_answer)["reply"]):
            raise ValueError("reply source could not be stored")
    return answer


def live_answer_differs(
    runtime: Any,
    caller: Any,
    context: dict[str, Any],
    run: Any,
    live: dict[str, Any],
    stored: dict[str, Any] | None,
    *,
    copy_pack_fn: Any,
) -> bool:
    """核验 1–5 版缓存正文是否仍与本轮快照一致（确定性重算或证据校验）。"""
    from aiops_diagnostics.consumer.consumer_facts import model_reply, validate_reply
    from aiops_diagnostics.consumer.consumer_knowledge import live_answer

    if context.get("fact_contract") == "slots":
        source = runtime.dify_run_store.reply_source(run.run_id)
        try:
            return model_reply(source, live, language=run.language, contract="slots") != stored
        except chat_execution.ChatRunUnavailable:
            return True
    expected = live_answer(
        live,
        run.language,
        localized=context.get("answer_language_version") == 2,
        pack=copy_pack_fn(runtime, caller, context, run.language),
    )
    if stored == expected:
        return False
    blocks = (stored or {}).get("blocks") or []
    if len(blocks) != 1 or blocks[0].get("kind") != "text":
        return True
    try:
        validate_reply(blocks[0].get("text"), live, language=run.language, contract="legacy")
    except chat_execution.ChatRunUnavailable:
        return True
    return False


def require_legacy_result(
    runtime: Any,
    caller: Any,
    binding: Any,
    run: Any,
    qa: dict[str, Any],
    *,
    copy_pack_fn: Any,
) -> None:
    """1–5 版回读核验：重新检查订单/知识库授权、交付窗口与实时快照一致性。"""
    if run.order_no:
        runtime._require_chat_order_visible(caller, run.order_no)
    elif run.data_kind not in {"clarify", "human", "order_required"}:
        from aiops_diagnostics.consumer.consumer_knowledge import require_available

        require_available(runtime, caller, binding)
    latest = runtime.dify_run_store.get(run.run_id)
    if latest is None or not deliverable(latest):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_RESULT_EXPIRED", "chatflow result expired", http_status=410
        )
    if run.order_no:
        context = runtime.dify_run_store.context(run.run_id) or {}
        live = context.get("live_snapshot", {})
        if (
            live.get("order_no") != run.order_no
            or live.get("source")
            != ("cpms.orderDetail" if run.data_kind == "billing" else "cpms.chargingInfo")
            or live_answer_differs(
                runtime, caller, context, run, live, qa.get("result"), copy_pack_fn=copy_pack_fn
            )
        ):
            raise chat_execution.ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "live evidence differs")
    if runtime.dify_run_store.snapshot(run.run_id) != qa.get("result"):
        raise chat_execution.ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "answer evidence differs")
