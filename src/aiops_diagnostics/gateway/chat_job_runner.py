"""聊天订单状态异步作业执行器（Dify 路线与 Java 节点路线）。

从 `gateway_runtime.py` 拆出，使 `GatewayRuntime` 只保留线程池、取消登记与共享
Store 容器，而聊天执行、取数凭据核对与撤回收敛集中于本模块。
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
import threading
from typing import Any

from aiops_diagnostics.conversation.conversation_store import TurnCompletionMetadata
from aiops_diagnostics.dify.dify_run_store import DifyRunSpec
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.identity.scope_context import ScopeContext

_LOGGER = logging.getLogger("aiops.gateway_runtime")


def execute_chat_dify_qa(
    runtime: Any,
    qa_id: str,
    order_no: str,
    context: ScopeContext,
    language: str,
    registration: Any,
    question: str,
    binding: Any,
) -> None:
    """后台：签发取数凭据 → 建执行记录 → 调 Dify → 核结果 → 放行 → 写终态。

    与节点 worker 并列：两条共用的部分是规则（写前再授权、失败落 failed、不回落），不是流程。
    Dify 侧成功判据缺一不可：
    1. 实际运行的 workflow 身份与绑定一致；
    2. 存在同一轮的取数记录；
    3. 输出能适配成 blocks，且订单号与本次冻结的订单一致。
    """
    from aiops_diagnostics.dify import dify_chat_workflow
    from aiops_diagnostics.dify.dify_run_store import DifyRunStore
    from aiops_diagnostics.execution import chat_data_grant, chat_execution

    dify_store = DifyRunStore(runtime.store.path)
    grant_store = chat_data_grant.DataGrantStore(runtime.store.path)
    if registration.cancelled:
        return
    if not runtime.store.update_assistant_question(qa_id, status="running"):
        return
    run_id: str | None = None
    try:
        workflow_key = dify_chat_workflow.load_workflow_key(binding.workflow_key_file)
        token, grant = grant_store.issue(
            scope_fingerprint=context.scope_fingerprint,
            order_no=order_no,
            environment=str(runtime.gateway_settings.execution_environment or ""),
            tenant_id=context.effective_tenant_id,
            qa_id=qa_id,
            workflow_id=binding.workflow_id,
        )
        run_id = "dify_" + secrets.token_urlsafe(16)
        dify_store.issue_spec(
            DifyRunSpec(
                run_id=run_id,
                qa_id=qa_id,
                scope_fingerprint=context.scope_fingerprint,
                scope_json=scope_to_json(context),
                binding_key=binding.key,
                order_no=order_no,
                environment=str(runtime.gateway_settings.execution_environment or ""),
                tenant_id=context.effective_tenant_id,
                app_id=getattr(binding, "app_id", ""),
                workflow_id=binding.workflow_id,
                grant_id=grant.grant_id,
                agent_version_no=int(getattr(binding, "agent_version_no", 0) or 0),
                route_revision=int(getattr(binding, "route_revision", 0) or 0),
            )
        )
        if registration.cancelled:
            runtime._settle_dify_cancelled(dify_store, grant_store, run_id, qa_id)
            return
        client = dify_chat_workflow.DifyWorkflowClient(
            str(runtime.gateway_settings.dify_chat_base_url),
            timeout_seconds=chat_data_grant.GRANT_TTL_SECONDS - 30,
        )
        stop_lock = threading.Lock()
        stop_sent = False

        def started(workflow_run_id: str, task_id: str) -> None:
            if not dify_store.bind_workflow(run_id, workflow_run_id=workflow_run_id, task_id=task_id):
                raise chat_execution.ChatRunUnavailable("DIFY_WORKFLOW_INVALID", "dify identity changed")

            def stop() -> None:
                nonlocal stop_sent
                with stop_lock:
                    if stop_sent:
                        return
                    stop_sent = True
                client.stop(workflow_key=workflow_key, task_id=task_id, user=f"aiops:{grant.grant_id}")

            registration.interrupt = stop
            current = dify_store.get(run_id)
            if registration.cancelled or current is None or current.state != "running":
                stop()

        result = client.run(
            workflow_key=workflow_key,
            workflow_id=binding.workflow_id,
            inputs={
                "data_grant": token,
                "order_no": order_no,
                "question": question,
                "language": language,
            },
            user=f"aiops:{grant.grant_id}",
            on_started=started,
        )
        if result.workflow_id != binding.workflow_id:
            raise chat_execution.ChatRunUnavailable("DIFY_WORKFLOW_MISMATCH", "dify ran a different workflow")
        if registration.cancelled:
            runtime._settle_dify_cancelled(dify_store, grant_store, run_id, qa_id)
            return
        runtime._require_chat_order_visible(context, order_no)
        answer = runtime._release_dify_answer(
            run_id=run_id,
            grant_id=grant.grant_id,
            binding=binding,
            context=context,
            order_no=order_no,
            language=language,
            outputs=result.outputs,
        )
        runtime._require_dify_binding_current(binding)
        if not dify_store.succeed(run_id):
            raise chat_execution.ChatRunUnavailable("DIFY_RUN_NOT_ACTIVE", "dify run is no longer active")
    except chat_execution.ChatRunUnavailable as exc:
        runtime._settle_dify_failed(dify_store, grant_store, run_id, qa_id, exc.code, detail=str(exc))
        _LOGGER.warning("chat dify route rejected: %s: %s", exc.code, exc)
        return
    except dify_chat_workflow.DifyWorkflowError as exc:
        runtime._settle_dify_failed(dify_store, grant_store, run_id, qa_id, exc.code)
        _LOGGER.warning("chat dify route failed: %s: %s", exc.code, exc)
        return
    except Exception as exc:  # noqa: BLE001 — 未预期失败也是失败，不是"换个引擎再试"
        runtime._settle_dify_failed(dify_store, grant_store, run_id, qa_id, "QA_FAILED")
        _LOGGER.warning("chat dify route failed: %s: %s", type(exc).__name__, exc)
        return
    finally:
        with contextlib.suppress(Exception):
            grant_store.revoke_for_qa(qa_id)
    if not runtime.store.update_assistant_question(qa_id, status="completed", result=answer):
        return
    with contextlib.suppress(Exception):
        runtime._complete_conversation_turn(
            registration.conversation_turn,
            question,
            answer,
            metadata=TurnCompletionMetadata(status_qa_id=qa_id),
        )


def settle_dify_failed(
    runtime: Any,
    dify_store: Any,
    grant_store: Any,
    run_id: str | None,
    qa_id: str,
    code: str,
    detail: str | None = None,
) -> None:
    if run_id is not None:
        with contextlib.suppress(Exception):
            dify_store.fail(run_id, code.lower(), detail=detail[:200] if detail else None)
    with contextlib.suppress(Exception):
        grant_store.revoke_for_qa(qa_id)
    registration = runtime._qa_registrations.get(qa_id)
    if registration is not None:
        runtime._release_conversation_turn(registration.conversation_turn)
    runtime.store.update_assistant_question(qa_id, status="failed", error_code=code)


def settle_dify_cancelled(
    runtime: Any,
    dify_store: Any,
    grant_store: Any,
    run_id: str,
    qa_id: str,
) -> None:
    with contextlib.suppress(Exception):
        dify_store.cancel(run_id, "cancelled_by_caller")
    with contextlib.suppress(Exception):
        grant_store.revoke_for_qa(qa_id)
    registration = runtime._qa_registrations.get(qa_id)
    if registration is not None:
        runtime._release_conversation_turn(registration.conversation_turn)


def settle_retracted_runs(runtime: Any, *, route: Any) -> dict[str, Any]:
    """收敛撤回时冻结的路由代次；之后发布或恢复的任务不属于本次操作。"""
    from aiops_diagnostics.dify import dify_chat_workflow
    from aiops_diagnostics.dify.dify_run_store import DifyRunStore
    from aiops_diagnostics.execution import chat_data_grant

    store = DifyRunStore(runtime.store.path)
    grants = chat_data_grant.DataGrantStore(runtime.store.path)
    settled: list[str] = []
    for run in store.running_for(
        tenant_id=route.tenant_id, environment=route.environment, binding_key=route.key
    ):
        if run.route_revision > route.revision:
            continue
        if not store.cancel(run.run_id, "retracted_by_operator"):
            continue
        grants.revoke_for_qa(run.qa_id)
        registration = runtime._qa_registrations.get(run.qa_id)
        if registration is not None:
            registration.cancelled = True
        if run.kind in {"trial", "service_trial"}:
            runtime.store.update_standard_diagnosis(run.qa_id, status="cancelled")
        else:
            if registration is not None:
                runtime._release_conversation_turn(registration.conversation_turn)
            runtime.store.update_assistant_question(run.qa_id, status="cancelled")
        if registration is not None and registration.interrupt is not None:
            runtime._interrupt_registered_turn(registration)
        elif run.task_id and run.kind == "chat" and run.app_id == route.app_id:
            with contextlib.suppress(Exception):
                dify_chat_workflow.DifyWorkflowClient(runtime.gateway_settings.dify_chat_base_url).stop(
                    workflow_key=dify_chat_workflow.load_workflow_key(route.workflow_key_ref),
                    task_id=run.task_id,
                    user=f"aiops:{run.grant_id}",
                )
        settled.append(run.run_id)
    if getattr(route, "engine", "dify") == "node" and runtime.agent_run_store is not None:
        with runtime.agent_run_store._connection() as connection:
            rows = connection.execute(
                "SELECT run_id, qa_id FROM agent_runs WHERE chat_binding=? "
                "AND route_revision<=? AND state IN ('issued','running')",
                (route.key, route.revision),
            ).fetchall()
        for row in rows:
            runtime.agent_run_store.revoke(row["run_id"], "retracted_by_operator")
            if row["qa_id"]:
                if row["qa_id"].startswith("tr_"):
                    runtime.store.update_standard_diagnosis(row["qa_id"], status="cancelled")
                else:
                    runtime.store.update_assistant_question(row["qa_id"], status="cancelled")
            settled.append(row["run_id"])
    return {"settled_runs": settled}


def release_dify_answer(
    runtime: Any,
    *,
    run_id: str,
    grant_id: str,
    binding: Any,
    context: ScopeContext,
    order_no: str,
    language: str,
    outputs: dict[str, Any],
) -> dict[str, Any]:
    """把 Dify 的输出适配成原 blocks 形状，并对本轮快照做完整性校验。"""
    from aiops_diagnostics.consumer.presentation import status_answer
    from aiops_diagnostics.dify.dify_run_store import DifyRunStore
    from aiops_diagnostics.execution import chat_data_grant
    from aiops_diagnostics.execution import chat_execution as _chat

    del binding, context
    grant = chat_data_grant.usable(chat_data_grant.DataGrantStore(runtime.store.path).by_id(grant_id))
    if grant.order_no != order_no:
        raise _chat.ChatRunUnavailable("DIFY_ORDER_MISMATCH", "dify read a different order")
    stored = DifyRunStore(runtime.store.path).snapshot(run_id)
    if stored is None:
        raise _chat.ChatRunUnavailable("DIFY_NO_DATA", "dify produced no data")
    snapshot = outputs.get("snapshot")
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except ValueError:
            snapshot = None
    if not isinstance(snapshot, dict):
        _LOGGER.warning(
            "chat dify outputs shape: keys=%s snapshot_type=%s",
            sorted(outputs),
            type(outputs.get("snapshot")).__name__,
        )
        raise _chat.ChatRunUnavailable("DIFY_NO_DATA", "dify produced no data")
    if snapshot.get("order_no") != order_no:
        raise _chat.ChatRunUnavailable("DIFY_ORDER_MISMATCH", "dify read a different order")
    if set(snapshot) != _chat.SNAPSHOT_FIELDS:
        raise _chat.ChatRunUnavailable("DIFY_NO_DATA", "dify data is not a status snapshot")
    if {key: str(value) for key, value in snapshot.items()} != {
        key: str(value) for key, value in stored.items()
    }:
        raise _chat.ChatRunUnavailable("DIFY_SNAPSHOT_MISMATCH", "dify answered from a different read")
    return status_answer(snapshot, language)


def require_dify_binding_current(runtime: Any, binding: Any) -> None:
    """校验冻结的 Dify 绑定此刻仍然有效且与签发代次一致。"""
    from aiops_diagnostics.execution import chat_execution

    try:
        current = chat_execution.binding_for_key(runtime.gateway_settings, getattr(binding, "key", None))
    except chat_execution.ChatRunUnavailable as exc:
        raise chat_execution.ChatRunUnavailable(exc.code, str(exc)) from exc
    if (
        current is None
        or current.engine != chat_execution.ENGINE_DIFY
        or current.workflow_id != getattr(binding, "workflow_id", None)
        or (getattr(binding, "route_revision", 0) and current.route_revision != binding.route_revision)
        or (
            int(getattr(binding, "agent_version_no", 0) or 0)
            and int(getattr(binding, "agent_version_no", 0) or 0)
            != int(getattr(current, "agent_version_no", 0) or 0)
        )
    ):
        raise chat_execution.ChatRunUnavailable(
            "CHARGING_STATUS_DISABLED", "dify binding is no longer active"
        )


def require_chat_order_visible(runtime: Any, context: ScopeContext, order_no: str) -> None:
    """写结果之前再判一次本人订单归属（与 API 层共用同一授权器与判据，fail closed）。"""
    from aiops_diagnostics.execution import chat_execution

    authorizer = runtime._order_authorizer
    if authorizer is None:
        raise chat_execution.ChatRunUnavailable(
            "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    try:
        allowed = authorizer.can_access(context, order_no)
    except Exception as exc:  # noqa: BLE001 — 授权源故障与"不可见"同样是拒绝
        raise chat_execution.ChatRunUnavailable(
            "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        ) from exc
    if not allowed:
        raise chat_execution.ChatRunUnavailable(
            "ORDER_NOT_VISIBLE", "order not visible to this caller", http_status=404
        )


def execute_chat_order_qa(
    runtime: Any,
    qa_id: str,
    order_no: str,
    context: ScopeContext,
    language: str,
    registration: Any,
    question: str,
    binding: Any,
    *,
    gateway_version: str,
) -> None:
    """后台：签发 → 绑定 QA↔run → 派发 → 等结果 → 放行 → 写终态。"""
    from aiops_diagnostics.execution import chat_execution

    store = runtime.agent_run_store
    if store is None:
        runtime.store.update_assistant_question(qa_id, status="failed", error_code="AGENT_RUN_DISABLED")
        return
    if registration.cancelled:
        return
    if not runtime.store.update_assistant_question(qa_id, status="running"):
        return
    run_id: str | None = None
    try:
        run, config = chat_execution.issue_for_store(
            store=store,
            settings=runtime.gateway_settings,
            binding=binding,
            caller=context,
            order_no=order_no,
            gateway_version=gateway_version,
            language=language,
        )
        run_id = run.run_id
        store.bind_qa(run.run_id, qa_id)
        if registration.cancelled:
            store.revoke(run.run_id, "cancelled_before_dispatch")
            return
        chat_execution.dispatch_run(store=store, config=config, run=run)
        chat_execution.settled_run(store=store, run_id=run.run_id)
        runtime._require_chat_order_visible(context, order_no)
        released = chat_execution.final_release(
            store=store,
            settings=runtime.gateway_settings,
            caller=context,
            run_id=run.run_id,
        )
        answer = chat_execution.answer_from_run(released, language=language)
    except chat_execution.ChatRunUnavailable as exc:
        if run_id is not None:
            store.fail(run_id, exc.code.lower())
        _LOGGER.warning("chat node route rejected: %s: %s", exc.code, exc)
        runtime._release_conversation_turn(registration.conversation_turn)
        runtime.store.update_assistant_question(qa_id, status="failed", error_code=exc.code)
        return
    except Exception as exc:  # noqa: BLE001 — 未预期失败也必须是失败，不是"再试直读"
        if run_id is not None:
            store.fail(run_id, "unexpected_error")
        runtime._release_conversation_turn(registration.conversation_turn)
        runtime.store.update_assistant_question(qa_id, status="failed", error_code="QA_FAILED")
        _LOGGER.warning("chat node route failed: %s: %s", type(exc).__name__, exc)
        return
    if not runtime.store.update_assistant_question(qa_id, status="completed", result=answer):
        return
    with contextlib.suppress(Exception):
        runtime._complete_conversation_turn(
            registration.conversation_turn,
            question,
            answer,
            metadata=TurnCompletionMetadata(status_qa_id=qa_id),
        )
