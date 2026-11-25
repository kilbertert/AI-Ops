"""WF01 原聊天作业接线：复用会话锁、Dify记录与最终放行，默认无登记不启用。"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from aiops_diagnostics.consumer import legacy_callbacks
from aiops_diagnostics.consumer.consumer_binding import deliverable_binding, require_pinned
from aiops_diagnostics.dify.dify_chat_workflow import DifyWorkflowError, load_workflow_key
from aiops_diagnostics.dify.dify_chatflow import DifyChatflowClient
from aiops_diagnostics.dify.dify_run_store import still_valid
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.execution.run_deliverability import deliverable

PREFIX = "consumer-chatflow:"


def version_key(binding: Any) -> str:
    return f"{binding.agent_id}#v{binding.agent_version_no}"


def fingerprint(binding):
    from aiops_diagnostics.consumer.consumer_binding import identity

    return identity(binding)


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
    from aiops_diagnostics.consumer import consumer_knowledge

    current(runtime, caller, binding)
    if binding.content_file or binding.content_snapshot is not None:
        consumer_knowledge.binding_content(binding, caller)
    agent_context = None
    upstream = ""
    if binding.callback_version == 6:
        from aiops_diagnostics.consumer import consumer_agent

        agent_context = consumer_agent.conversation_context(
            runtime, caller, conversation, registration.conversation_turn[2], order_no
        )
        data_kind = "agent"
    else:
        upstream = legacy_callbacks.resolve_legacy_upstream(
            runtime,
            caller,
            binding,
            conversation,
            order_no=order_no,
            photo_id=photo_id,
            uses_context=uses_context,
            expected_previous_qa_id=expected_previous_qa_id,
            language=language,
            fingerprint_fn=fingerprint,
            require_result_fn=require_result,
        )
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
            from aiops_diagnostics.execution.chat_data_grant import DataGrantStore

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
        if agent_context is not None:
            consumer_agent.initialize(runtime, run_id, agent_context)
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


def _revoke_grant(runtime, qa, token):
    """撤销这次问答的取数凭据。幂等：完成/失败/取消三条路都会走到它。"""
    if not token:
        return
    from aiops_diagnostics.execution.chat_data_grant import DataGrantStore

    DataGrantStore(runtime.store.path).revoke_for_qa(qa["qa_id"])


def execute(runtime, caller, qa, question, binding, upstream, registration, language, token="", image=b""):
    run_id = qa["internal_run_id"].removeprefix(PREFIX)
    store = runtime.dify_run_store
    if not runtime.store.update_assistant_question(qa["qa_id"], status="running"):
        store.cancel(run_id)
        if token:
            from aiops_diagnostics.execution.chat_data_grant import DataGrantStore

            DataGrantStore(runtime.store.path).revoke_for_qa(qa["qa_id"])
        runtime._release_conversation_turn(registration.conversation_turn)
        return
    try:
        require_pinned(runtime, caller, binding)
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
        require_pinned(runtime, caller, binding)
        latest = store.get(run_id)
        if (
            registration.cancelled
            or latest is None
            or latest.state != "running"
            or not still_valid(latest)
            or latest.binding_digest != fingerprint(binding)
        ):
            raise ValueError("run ended during upload")
        if binding.callback_version == 6:
            import json

            from aiops_diagnostics.consumer import consumer_agent

            consumer_agent.require_evidence(runtime, caller, binding, store.context(run_id))
            inputs = {
                "language": language,
                "data_grant": token,
                "conversation_context": json.dumps(store.context(run_id)["conversation"], ensure_ascii=False),
            }
        else:
            inputs = legacy_callbacks.build_legacy_inputs(
                latest, binding, language=language, uploaded=uploaded, token=token
            )
        reply = client.run(
            key=key,
            workflow_id=binding.workflow_id,
            user=user,
            query=question,
            inputs=inputs,
            **({"agent_final_node": "answer"} if binding.callback_version == 6 else {}),
            **({"photo_id": uploaded} if uploaded else {}),
            conversation_id=upstream,
            on_started=started,
        )
        if binding.callback_version == 6:
            require_pinned(runtime, caller, binding)
            run = store.get(run_id)
            if (
                registration.cancelled
                or run is None
                or run.state != "running"
                or not still_valid(run)
                or run.binding_digest != fingerprint(binding)
            ):
                raise ValueError("run is no longer active")
            from aiops_diagnostics.consumer import consumer_agent

            context = store.context(run_id)
            consumer_agent.require_evidence(runtime, caller, binding, context)
            answer = consumer_agent.final_answer(reply.answer, context, language=language, question=question)
        else:
            legacy_callbacks.verify_legacy_post_reply(
                runtime,
                caller,
                binding,
                store,
                run_id,
                latest,
                registration,
                fingerprint_fn=fingerprint,
            )
            answer = legacy_callbacks.build_legacy_answer(
                runtime,
                caller,
                binding,
                store,
                run_id,
                reply.answer,
                language,
                copy_pack_fn=copy_pack,
            )
        if not store.store_snapshot(run_id, answer) or not store.succeed(run_id):
            raise ValueError("result could not be committed")
        require_pinned(runtime, caller, binding)
        # **先撤销取数能力，再公布终态。** 反过来会留下一个窗口：`status=completed` 已经
        # 可读，而这次请求的数据凭据还没失效 —— 读到的观察者（回读接口、验收脚本、
        # 任何按状态轮询的客户端）会看到"结果已交付但凭据仍然可用"。
        # 2026-10-30 的 CI 实测过这个窗口：`test_workflow_retrieves_once_and_returns_only_the_approved_answer`
        # 在同一份代码上时而看到 `revoked`、时而看到 `consumed`。
        # `revoke_for_qa` 是幂等的，所以 finally 里那一份保留为兜底。
        _revoke_grant(runtime, qa, token)
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
        # `reason` 保持稳定错误码；具体判据单独记，便于事后区分成因
        # （同一个码可能覆盖片段外复述、片段缺失、字段不符等多种情形）。
        store.fail(
            run_id,
            code,
            str(exc).strip()[:200] if isinstance(exc, chat_execution.ChatRunUnavailable) else None,
        )
        runtime.store.update_assistant_question(qa["qa_id"], status="failed", error_code=code)
        runtime._release_conversation_turn(registration.conversation_turn)
    finally:
        _revoke_grant(runtime, qa, token)


def require_result(runtime, caller, qa):
    if qa["status"] != "completed":
        return
    if not runtime.gateway_settings.consumer_chatflow_enabled:
        raise chat_execution.ChatRunUnavailable("CHATFLOW_DISABLED", "chatflow disabled", http_status=404)
    run = runtime.dify_run_store.by_qa(qa["qa_id"])
    if run is None or run.kind != "service" or run.scope_fingerprint != caller.scope_fingerprint:
        raise chat_execution.ChatRunUnavailable("QA_NOT_FOUND", "answer not found", http_status=404)
    binding = deliverable_binding(runtime, caller, run)
    if (
        binding.app_id != run.app_id
        or binding.workflow_id != run.workflow_id
        or binding.key != run.binding_key
    ):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding changed", http_status=410
        )
    if binding.callback_version == 6:
        from aiops_diagnostics.consumer import consumer_agent

        require_pinned(runtime, caller, binding)
        consumer_agent.require_evidence(runtime, caller, binding, runtime.dify_run_store.context(run.run_id))
        if not deliverable(run):
            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_RESULT_EXPIRED", "chatflow result expired", http_status=410
            )
        if runtime.dify_run_store.snapshot(run.run_id) != qa.get("result"):
            raise chat_execution.ChatRunUnavailable(
                "CHATFLOW_EVIDENCE_UNAVAILABLE", "answer evidence differs"
            )
        return
    legacy_callbacks.require_legacy_result(runtime, caller, binding, run, qa, copy_pack_fn=copy_pack)


def _live_answer_differs(runtime, caller, context, run, live, stored):
    return legacy_callbacks.live_answer_differs(
        runtime, caller, context, run, live, stored, copy_pack_fn=copy_pack
    )


def copy_pack(runtime, caller, context, language):
    """本轮该用哪一版文案。

    `context` 里钉着 `copy_version`（生成时写入）；没有这个键的是历史答复 ——
    它们按内置文案渲染，和引入文案包之前逐字相同。
    """
    from aiops_diagnostics.consumer.consumer_copy import resolve

    return resolve(
        runtime.store,
        caller.effective_tenant_id,
        version=(context or {}).get("copy_version"),
        language=language,
    )


def previous_context(runtime, caller, binding, conversation):
    """过期结果不作语义上下文，但也不使整个会话失效。"""

    if not conversation:
        return None, ""
    run = runtime.dify_run_store.latest_conversation_run(
        conversation["conversation_id"], caller.scope_fingerprint
    )
    if not run or not deliverable(run) or run.binding_digest != fingerprint(binding):
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
