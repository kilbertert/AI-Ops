"""WF01 原聊天作业接线：复用会话锁、Dify记录与最终放行，默认无登记不启用。"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from aiops_diagnostics.dify.dify_chat_workflow import DifyWorkflowError, load_workflow_key
from aiops_diagnostics.dify.dify_chatflow import DifyChatflowClient
from aiops_diagnostics.dify.dify_run_store import still_valid
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.agent_runs import scope_to_json

PREFIX = "consumer-chatflow:"


def version_key(binding: Any) -> str:
    return f"{binding.agent_id}#v{binding.agent_version_no}"


def fingerprint(binding):
    from aiops_diagnostics.consumer.consumer_knowledge import _binding_identity

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
    from aiops_diagnostics.consumer import consumer_knowledge

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
            or previous.binding_digest != fingerprint(binding)
            or previous.workflow_id != binding.workflow_id
            or previous.route_revision != binding.route_revision
            or previous.agent_version_no != binding.agent_version_no
        ):
            from aiops_diagnostics.consumer.consumer_intent import message

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
            from aiops_diagnostics.execution.chat_data_grant import DataGrantStore

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
            from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available

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
            from aiops_diagnostics.consumer.consumer_knowledge import binding_content, final_answer

            content = binding_content(binding, caller)
            context = store.context(run_id) or {}
            answer = final_answer(
                reply.answer,
                context,
                language,
                content,
                copy_pack=copy_pack(runtime, caller, context, language),
            )
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
            from aiops_diagnostics.execution.chat_data_grant import DataGrantStore

            DataGrantStore(runtime.store.path).revoke_for_qa(qa["qa_id"])


def _pinned_route_active(runtime, run) -> bool:
    """签发时那条路由现在是否仍然 active（撤回 = 不再交付）。

    普通发布只推进版本；撤回会把 `state` 改成 `retracted`。这两件事在数据上本来就可区分，
    旧回读却把它们当成同一件事（版本不等就 410），于是**一次正常发布就让历史答案失效**。
    """
    path = str(getattr(runtime.gateway_settings, "chat_route_bindings_file", "") or "")
    if not path:
        return True
    from pathlib import Path

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    rows = [
        row
        for row in ChatRouteBindingStore(Path(path)).all()
        if row.environment == run.environment and row.app_id == run.app_id
    ]
    return bool(rows) and all(row.active for row in rows)


def deliverable_binding(runtime, caller, run):
    """已签发答复回读时的绑定判据：**按签发时那一版交付**，撤回/换 app 才拒。

    与 :func:`current` 的区别是刻意的：新会话必须用当前版本；历史与在途答案用的是它**签发
    时**冻结的版本与摘要（`run.binding_digest` / `run.agent_version_no`）。把两者混成一个
    判据，代价就是复核实测到的那条 —— 运营正常发布一版措辞，旧 QA 从 200 变 410。
    """
    selected = chat_execution.select_binding(
        runtime.gateway_settings, caller, scenario=chat_execution.SCENARIO_CONSUMER_SERVICE
    )
    if selected is None:
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding changed", http_status=410
        )
    if fingerprint(selected) == run.binding_digest:
        return selected
    # 允许的**唯一**差别是"路由推进了一版"（运营正常发布）。四件事必须同时成立：
    #   1. 路由 revision 比签发时新 —— 说明差异来自一次发布，而不是内容被换掉；
    #   2. 凭据引用、app、workflow 都没变 —— 换凭据/换应用是不同事件，必须拒；
    #   3. 这条路由仍然 active —— 撤回就不交付；
    #   4. 旧版本仍在版本表里 —— 冻结内容可读。
    if (
        selected.route_revision <= run.route_revision
        or selected.key != run.binding_key
        or selected.app_id != run.app_id
        or selected.workflow_id != run.workflow_id
        or not _pinned_route_active(runtime, run)
    ):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_BINDING_CHANGED", "chatflow binding changed", http_status=410
        )
    # 同一 app、路由仍 active、只是版本推进：旧答案按签发时的版本继续交付。
    return selected


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
    if run.order_no:
        runtime._require_chat_order_visible(caller, run.order_no)
    else:
        from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available

        require_available(runtime, caller, binding)
    latest = runtime.dify_run_store.get(run.run_id)
    if latest is None or latest.state != "succeeded" or not still_valid(latest):
        raise chat_execution.ChatRunUnavailable(
            "CHATFLOW_RESULT_EXPIRED", "chatflow result expired", http_status=410
        )
    # 这里**不再**要求"当前绑定 == 签发绑定"：那条判据属于新会话（见 `current`）。
    # 撤回已经由 `deliverable_binding` 的 `_pinned_route_active` 挡住。
    if run.order_no:
        context = runtime.dify_run_store.context(run.run_id) or {}
        live = context.get("live_snapshot", {})
        if (
            live.get("order_no") != run.order_no
            or live.get("source")
            != ("cpms.orderDetail" if run.data_kind == "billing" else "cpms.chargingInfo")
            or _live_answer_differs(runtime, caller, context, run, live, qa.get("result"))
        ):
            raise chat_execution.ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "live evidence differs")
    if runtime.dify_run_store.snapshot(run.run_id) != qa.get("result"):
        raise chat_execution.ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "answer evidence differs")


def _live_answer_differs(runtime, caller, context, run, live, stored):
    """缓存的正文还是不是**这条快照**的答复。

    两条合法来源：确定性渲染（可逐字重算）与模型按编排页提示词写的正文（不可重算）。
    后者用**同一套证据校验**判：数字必须在证据里、内部标识不许出现、订单号必须在。
    这比"逐字重算"弱一档（一份保留了全部数字的改写会通过），但比"什么都不校验"强得多，
    而逐字重算在模型写正文的前提下根本做不到 —— 见 ADR-0187。
    """
    from aiops_diagnostics.consumer.consumer_knowledge import live_answer

    expected = live_answer(
        live,
        run.language,
        localized=context.get("answer_language_version") == 2,
        pack=copy_pack(runtime, caller, context, run.language),
    )
    if stored == expected:
        return False
    blocks = (stored or {}).get("blocks") or []
    if len(blocks) != 1 or blocks[0].get("kind") != "text":
        return True
    from aiops_diagnostics.consumer.consumer_reply import validate_reply

    try:
        validate_reply(blocks[0].get("text"), live, language=run.language)
    except chat_execution.ChatRunUnavailable:
        return True
    return False


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
