"""Dify / 节点草稿试运行作业执行器与并发闸。

从 `gateway_runtime.py` 拆出，独立承载试运行并发闸、范围构造、取消与终态落库，
不与常规聊天或健康/分析执行路径混杂。
"""

from __future__ import annotations

import time
from typing import Any

from aiops_diagnostics.gateway import metrics_store as metrics_dims
from aiops_diagnostics.gateway.gateway_store import ACTIVE_DIAGNOSIS_STATUSES
from aiops_diagnostics.identity.capability import CAPABILITY_ASSERTED
from aiops_diagnostics.identity.scope_context import SCOPE_TYPE_ALL, DataScope, ScopeContext, SubjectRecord


def tenant_wide_context(tenant_id: str) -> ScopeContext:
    """固定调试租户的顶层账号范围：整个租户可见，不受站点维度约束。"""
    subject = SubjectRecord(b_user_id="aiops-admin", tenant_id=tenant_id)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant_id,
        data_scope=DataScope(type=SCOPE_TYPE_ALL),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
        capability_source=CAPABILITY_ASSERTED,
    )


def trial_run_context(runtime: Any) -> ScopeContext:
    """试运行的固定调试租户范围（按顶层账号口径，路由与 worker 共用）。"""
    return tenant_wide_context(runtime.gateway_settings.dify_trial_debug_tenant)


def trial_context_for(runtime: Any, initiator: Any) -> ScopeContext:
    """发起人的按人范围：公司身份 → 运营商站点范围（与路由共用同一 ScopeContext）。"""
    from aiops_diagnostics.dify.dify_trial import build_trial_scope

    return build_trial_scope(initiator, operator_scope=runtime._trial_operator_scope)


def start_trial(
    runtime: Any,
    initiator: str,
    order_no: str,
    draft: Any,
    language: str,
    trial_initiator: Any = None,
    run_context: ScopeContext | None = None,
) -> dict[str, Any] | None:
    """提交一次试运行，或返回 ``None`` 表示并发超限（路由转 429）。"""
    resolved_context = run_context or (
        runtime.trial_context_for(trial_initiator)
        if trial_initiator is not None
        else runtime.trial_run_context()
    )
    with runtime._trial_lock:
        if len(runtime._trial_jobs) >= runtime._trial_max_concurrent:
            return None
        trial = runtime.store.create_trial_job(
            resolved_context.scope_fingerprint,
            order_no=order_no,
            draft_fingerprint=draft.fingerprint(),
            initiator=initiator,
            language=language,
            tenant_id=resolved_context.effective_tenant_id,
            workflow_trial=callable(getattr(draft, "execute", None)),
            node_trial=getattr(draft, "engine", "") == "node",
            source_app_id=getattr(getattr(draft, "source", None), "app_id", ""),
            managed_draft=bool(getattr(draft, "draft", False)),
        )
        trial_id = str(trial["diagnosis_id"])
        runtime._trial_jobs.add(trial_id)
        runtime._trial_contexts[trial_id] = (resolved_context, draft, order_no, language)
        future = runtime._executor.submit(runtime._execute_trial, trial_id)
        runtime._futures[trial_id] = future
        future.add_done_callback(lambda _: runtime._futures.pop(trial_id, None))
        future.add_done_callback(lambda _: runtime._release_trial(trial_id))
    return trial


def release_trial(runtime: Any, trial_id: str) -> None:
    with runtime._trial_lock:
        runtime._trial_jobs.discard(trial_id)
        runtime._trial_contexts.pop(trial_id, None)


def cancel_trial(runtime: Any, context: ScopeContext, trial_id: str) -> dict[str, Any] | None:
    """先撤销取数再落取消终态；迟到 worker 由现有终态保护拒绝。"""
    from aiops_diagnostics.execution.chat_data_grant import DataGrantStore

    trial = runtime.get_trial(context, trial_id)
    if trial is None or trial["status"] not in ACTIVE_DIAGNOSIS_STATUSES:
        return trial
    DataGrantStore(runtime.store.path).revoke_for_qa(trial_id)
    run = runtime.dify_run_store.by_qa(trial_id)
    if run:
        runtime.dify_run_store.cancel(run.run_id, "cancelled_by_caller")
    registration = runtime._qa_registrations.get(trial_id)
    if registration:
        registration.cancelled = True
    runtime.store.update_standard_diagnosis(trial_id, status="cancelled")
    if registration:
        runtime._interrupt_registered_turn(registration)
    return runtime.get_trial(context, trial_id)


def execute_trial(runtime: Any, trial_id: str) -> None:
    """认领一行 → 跑草稿 → 写终态；任何异常都收敛至 failed，防止永久占用并发闸。"""
    entry = runtime._trial_contexts.get(trial_id)
    if entry is None:  # pragma: no cover - 只在释放竞态下发生
        return
    run_context, draft, order_no, language = entry
    if not runtime.store.update_standard_diagnosis(trial_id, status="running"):
        return
    started_ms = time.monotonic()
    try:
        result = draft.execute(runtime, trial_id, run_context, order_no, language)
    except Exception as exc:  # noqa: BLE001 - 逃出去就等于没有终态行
        from aiops_diagnostics.common.model_request import ModelRequestError
        from aiops_diagnostics.consumer.consumer_intent import TRIAL_MESSAGES
        from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

        code = (
            "CONSUMER_MODEL_QUOTA_EXHAUSTED"
            if isinstance(exc, ModelRequestError) and exc.reason == "quota_exhausted"
            else "CONSUMER_INTENT_UNAVAILABLE"
            if isinstance(exc, ModelRequestError)
            else exc.code
            if isinstance(exc, ChatRunUnavailable) and exc.code in TRIAL_MESSAGES
            else "TRIAL_FAILED"
        )
        if runtime.store.update_standard_diagnosis(
            trial_id,
            status="failed",
            error_code=code,
            error_message=f"{type(exc).__name__}: {exc}",
        ):
            record_trial_metric(runtime, run_context, "failed", started_ms)
        return
    if runtime.store.update_standard_diagnosis(
        trial_id,
        status="completed",
        result={
            "status": result.status,
            "report": result.report,
            "evidence_ids": list(result.evidence_ids),
            "blocked_tools": list(result.blocked_tools),
            "failed_sources": list(result.failed_sources),
            "language": result.language,
        },
    ):
        record_trial_metric(runtime, run_context, "completed", started_ms)


def record_trial_metric(
    runtime: Any,
    context: ScopeContext,
    outcome: str,
    started_ms: float,
) -> None:
    runtime._record_metric(
        tenant_id=context.effective_tenant_id,
        route_type=metrics_dims.ROUTE_TRIAL,
        outcome=outcome,
        duration_ms=int((time.monotonic() - started_ms) * 1000),
    )
