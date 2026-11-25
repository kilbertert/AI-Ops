"""C 端聊天流历史回调版本（``callback_version`` 1–5）兼容逻辑。

现役统一智能体循环使用 ``callback_version == 6``（见 ``consumer_chat_service.py`` 主路径）。
本模块将 1–5 版的上游会话续接、输入拼装、答复渲染与回读核验集中隔离，避免主路径
反复交错历史版本分支。
"""

from __future__ import annotations

from typing import Any

from aiops_diagnostics.consumer.consumer_binding import require_pinned
from aiops_diagnostics.dify.dify_run_store import still_valid
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.run_deliverability import deliverable


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
        from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available

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
    from aiops_diagnostics.consumer.consumer_knowledge import live_answer

    if context.get("fact_contract") == "slots":
        from aiops_diagnostics.consumer.consumer_reply import model_reply

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
    from aiops_diagnostics.consumer.consumer_reply import validate_reply

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
        from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available

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
