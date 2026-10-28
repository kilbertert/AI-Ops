"""WF01 原聊天作业接线：复用会话锁、Dify记录与最终放行，默认无登记不启用。"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from aiops_diagnostics import chat_execution
from aiops_diagnostics.agent_runs import scope_to_json
from aiops_diagnostics.dify_chat_workflow import DifyWorkflowError, load_workflow_key
from aiops_diagnostics.dify_chatflow import DifyChatflowClient
from aiops_diagnostics.dify_run_store import still_valid

PREFIX = "consumer-chatflow:"


def version_key(binding: Any) -> str:
    return f"{binding.agent_id}#v{binding.agent_version_no}"


def fingerprint(binding):
    from aiops_diagnostics.consumer_knowledge import _binding_identity

    return _binding_identity(binding)


def current(runtime, caller, binding):
    if not runtime.gateway_settings.consumer_chatflow_enabled:
        raise chat_execution.ChatRunUnavailable("CHATFLOW_DISABLED", "chatflow disabled", http_status=404)
    selected = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    if selected != binding:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding changed", http_status=410
        )
    return selected


def start(
    runtime,
    caller,
    question,
    *,
    binding,
    conversation,
    registration,
    language,
    order_no="",
    data_kind="status",
    photo_id="",
    uses_context=True,
    expected_previous_qa_id=None,
):
    from aiops_diagnostics import consumer_knowledge

    current(runtime, caller, binding)
    if binding.content_file or binding.content_snapshot is not None:
        consumer_knowledge.binding_content(binding, caller)
    previous = runtime.dify_run_store.latest_conversation_run(
        conversation["conversation_id"], caller.scope_fingerprint
    )
    upstream = ""
    if (
        uses_context
        and expected_previous_qa_id is not None
        and (previous.qa_id if previous else "") != expected_previous_qa_id
    ):
        from aiops_diagnostics.consumer_intent import message

        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_NEW_CONVERSATION_REQUIRED", message("context", language), http_status=409
        )
    if previous:
        previous_qa = runtime.store.get_assistant_question(previous.qa_id, caller.scope_fingerprint)
        if (
            previous_qa is None
            or previous_qa["status"] != "completed"
            or previous.state != "succeeded"
            or previous.binding_digest != fingerprint(binding)
            or previous.workflow_id != binding.workflow_id
            or previous.route_revision != binding.route_revision
            or previous.agent_version_no != binding.agent_version_no
        ):
            from aiops_diagnostics.consumer_intent import message

            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_NEW_CONVERSATION_REQUIRED", message("conversation", language), http_status=409
            )
        if still_valid(previous) and uses_context:
            require_result(runtime, caller, previous_qa)
            upstream = previous.upstream_conversation_id
            if not upstream:
                raise chat_execution.ChatRunUnavailable(
                    "CHATFLOW_EVIDENCE_UNAVAILABLE", "conversation evidence unavailable"
                )
        elif uses_context:
            from aiops_diagnostics.consumer_intent import message

            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_NEW_CONVERSATION_REQUIRED", message("context", language), http_status=409
            )
    # 单据轮次及其下一轮均不继承上游记忆，旧订单数据不会进入后续模型输入。
    if (
        binding.callback_version == 4
        or order_no
        or photo_id
        or (previous and (previous.order_no or previous.has_photo))
    ):
        upstream = ""
    image = (
        runtime.consumer_photos.take(photo_id, caller.scope_fingerprint, fingerprint(binding))
        if photo_id
        else b""
    )
    run_id = "service_" + uuid.uuid4().hex
    try:
        qa = runtime.store.create_assistant_question(
            caller.scope_fingerprint,
            question,
            internal_run_id=PREFIX + run_id,
            tenant_id=caller.effective_tenant_id,
        )
    except Exception:
        runtime.consumer_photos.finish(photo_id)
        raise
    token = ""
    try:
        if binding.content_file or binding.content_snapshot is not None:
            from aiops_diagnostics.chat_data_grant import DataGrantStore

            token, grant = DataGrantStore(runtime.store.path).issue(
                scope_fingerprint=caller.scope_fingerprint,
                order_no=order_no,
                environment=binding.environment,
                tenant_id=caller.effective_tenant_id,
                qa_id=qa["qa_id"],
                workflow_id=binding.workflow_id,
            )
        runtime.dify_run_store.issue(
            run_id=run_id,
            qa_id=qa["qa_id"],
            scope_fingerprint=caller.scope_fingerprint,
            scope_json=scope_to_json(caller),
            binding_key=binding.key,
            order_no=order_no,
            environment=binding.environment,
            tenant_id=caller.effective_tenant_id,
            app_id=binding.app_id,
            workflow_id=binding.workflow_id,
            grant_id=grant.grant_id if token else "",
            agent_version_no=binding.agent_version_no,
            kind="service",
            route_revision=binding.route_revision,
            conversation_id=conversation["conversation_id"],
            binding_digest=fingerprint(binding),
            language=language,
            data_kind=data_kind,
            has_photo=bool(image),
        )
        runtime._qa_registrations[qa["qa_id"]] = registration
        future = runtime._executor.submit(
            execute, runtime, caller, qa, question, binding, upstream, registration, language, token, image
        )
        runtime._futures[qa["qa_id"]] = future
        future.add_done_callback(lambda _: runtime.consumer_photos.finish(photo_id))
        future.add_done_callback(lambda _: runtime._futures.pop(qa["qa_id"], None))
        future.add_done_callback(lambda _: runtime._qa_registrations.pop(qa["qa_id"], None))
    except Exception:
        runtime.consumer_photos.finish(photo_id)
        if token:
            DataGrantStore(runtime.store.path).revoke_for_qa(qa["qa_id"])
        runtime.store.update_assistant_question(
            qa["qa_id"], status="failed", error_code="CHATFLOW_UNAVAILABLE"
        )
        runtime.dify_run_store.fail(run_id, "startup_failed")
        raise
    return qa


def execute(runtime, caller, qa, question, binding, upstream, registration, language, token="", image=b""):
    run_id = qa["internal_run_id"].removeprefix(PREFIX)
    store = runtime.dify_run_store
    if not runtime.store.update_assistant_question(qa["qa_id"], status="running"):
        store.cancel(run_id)
        if token:
            from aiops_diagnostics.chat_data_grant import DataGrantStore

            DataGrantStore(runtime.store.path).revoke_for_qa(qa["qa_id"])
        runtime._release_conversation_turn(registration.conversation_turn)
        return
    try:
        current(runtime, caller, binding)
        key = load_workflow_key(binding.workflow_key_file)
        client = DifyChatflowClient(runtime.gateway_settings.dify_chat_base_url, timeout_seconds=90)
        # 同用户不同App/发布版本也不共用Dify用户上下文；上游只收到不可逆标识。
        user = hashlib.sha256(
            f"{caller.scope_fingerprint}:{binding.key}:{binding.workflow_id}".encode()
        ).hexdigest()

        def started(workflow_run_id, task_id, upstream_conversation_id, message_id):
            if not store.bind_workflow(run_id, workflow_run_id=workflow_run_id, task_id=task_id):
                raise ValueError("workflow identity changed")
            if not store.bind_message(run_id, upstream_conversation_id, message_id):
                raise ValueError("message identity changed")
            registration.interrupt = lambda: client.stop(key=key, task_id=task_id, user=user)
            if registration.cancelled or store.get(run_id).state != "running":
                registration.interrupt()
                raise ValueError("cancelled")

        uploaded = client.upload_photo(key=key, user=user, image=image) if image else ""
        image = b""
        current(runtime, caller, binding)
        latest = store.get(run_id)
        if (
            registration.cancelled
            or latest is None
            or latest.state != "running"
            or not still_valid(latest)
            or latest.binding_digest != fingerprint(binding)
        ):
            raise ValueError("run ended during upload")
        reply = client.run(
            key=key,
            workflow_id=binding.workflow_id,
            user=user,
            query=question,
            inputs={
                "language": language,
                "has_photo": "yes" if uploaded else "no",
                **(
                    {"data_kind": latest.data_kind if latest.order_no else "knowledge"}
                    if binding.callback_version >= 2
                    else {}
                ),
                **({"data_grant": token} if token else {}),
            },
            **({"photo_id": uploaded} if uploaded else {}),
            conversation_id=upstream,
            on_started=started,
        )
        if not latest.order_no:
            from aiops_diagnostics.consumer_knowledge_selection import require_available

            require_available(runtime, caller, binding)
        current(runtime, caller, binding)
        run = store.get(run_id)
        if (
            registration.cancelled
            or run is None
            or run.state != "running"
            or not still_valid(run)
            or run.binding_digest != fingerprint(binding)
        ):
            raise ValueError("run is no longer active")
        if run.order_no:
            runtime._require_chat_order_visible(caller, run.order_no)
            current(runtime, caller, binding)
            run = store.get(run_id)
            if (
                run is None
                or run.state != "running"
                or not still_valid(run)
                or registration.cancelled
                or run.binding_digest != fingerprint(binding)
            ):
                raise ValueError("run ended during authorization")
            live = (store.context(run_id) or {}).get("live_snapshot", {})
            if live.get("order_no") != run.order_no or live.get("source") != (
                "cpms.orderDetail" if run.data_kind == "billing" else "cpms.chargingInfo"
            ):
                raise ValueError("live snapshot missing or belongs to another order")
        if binding.content_file or binding.content_snapshot is not None:
            from aiops_diagnostics.consumer_knowledge import binding_content, final_answer

            content = binding_content(binding, caller)
            answer = final_answer(reply.answer, store.context(run_id), language, content)
        else:
            answer = {"blocks": [{"kind": "text", "text": reply.answer}], "retrieval_status": "unavailable"}
        if not store.store_snapshot(run_id, answer) or not store.succeed(run_id):
            raise ValueError("result could not be committed")
        current(runtime, caller, binding)
        stored = runtime.store.update_assistant_question(qa["qa_id"], status="completed", result=answer)
        runtime._complete_conversation_turn(
            registration.conversation_turn, question, answer, guarded=not stored, status_qa_id=qa["qa_id"]
        )
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, (DifyWorkflowError, chat_execution.ChatRunUnavailable))
            else "CHATFLOW_UNAVAILABLE"
        )
        store.fail(run_id, code)
        runtime.store.update_assistant_question(qa["qa_id"], status="failed", error_code=code)
        runtime._release_conversation_turn(registration.conversation_turn)
    finally:
        if token:
            from aiops_diagnostics.chat_data_grant import DataGrantStore

            DataGrantStore(runtime.store.path).revoke_for_qa(qa["qa_id"])


def require_result(runtime, caller, qa):
    if qa["status"] != "completed":
        return
    if not runtime.gateway_settings.consumer_chatflow_enabled:
        raise chat_execution.ChatRunUnavailable("CHATFLOW_DISABLED", "chatflow disabled", http_status=404)
    run = runtime.dify_run_store.by_qa(qa["qa_id"])
    if run is None or run.kind != "service" or run.scope_fingerprint != caller.scope_fingerprint:
        raise chat_execution.ChatRunUnavailable("QA_NOT_FOUND", "answer not found", http_status=404)
    binding = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    if (
        binding is None
        or fingerprint(binding) != run.binding_digest
        or binding.app_id != run.app_id
        or binding.workflow_id != run.workflow_id
        or binding.route_revision != run.route_revision
        or binding.agent_version_no != run.agent_version_no
        or binding.key != run.binding_key
    ):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding changed", http_status=410
        )
    if run.order_no:
        runtime._require_chat_order_visible(caller, run.order_no)
    else:
        from aiops_diagnostics.consumer_knowledge_selection import require_available

        require_available(runtime, caller, binding)
    latest = runtime.dify_run_store.get(run.run_id)
    if latest is None or latest.state != "succeeded" or not still_valid(latest):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_RESULT_EXPIRED", "chatflow result expired", http_status=410
        )
    current(runtime, caller, binding)
    if run.order_no:
        from aiops_diagnostics.consumer_knowledge import live_answer

        context = runtime.dify_run_store.context(run.run_id) or {}
        live = context.get("live_snapshot", {})
        if (
            live.get("order_no") != run.order_no
            or live.get("source")
            != ("cpms.orderDetail" if run.data_kind == "billing" else "cpms.chargingInfo")
            or live_answer(live, run.language, localized=context.get("answer_language_version") == 2)
            != qa.get("result")
        ):
            raise chat_execution.ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "live evidence differs")
    if runtime.dify_run_store.snapshot(run.run_id) != qa.get("result"):
        raise chat_execution.ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "answer evidence differs")


def previous_context(runtime, caller, binding, conversation):
    """过期结果不作语义上下文，但也不使整个会话失效。"""

    if not conversation:
        return None, ""
    run = runtime.dify_run_store.latest_conversation_run(
        conversation["conversation_id"], caller.scope_fingerprint
    )
    if (
        not run
        or run.state != "succeeded"
        or not still_valid(run)
        or run.binding_digest != fingerprint(binding)
    ):
        return None, ""
    qa = runtime.store.get_assistant_question(run.qa_id, caller.scope_fingerprint)
    if not qa or qa["status"] != "completed":
        return None, ""
    try:
        require_result(runtime, caller, qa)
    except chat_execution.ChatRunUnavailable as exc:
        if exc.code == "KNOWLEDGE_SELECTION_UNAVAILABLE":
            return None, ""
        raise
    return run, qa["question"]
