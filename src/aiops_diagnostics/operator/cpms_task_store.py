"""管家 CPMS 任务状态机、授权闸门、轨迹记录与回读校验。

从 `operator_cpms.py` 拆出，使任务生命周期、锁管理、范围刷新、节点鉴权与回读授权
集中在一个高内聚模块内。
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.async_job_lifecycle import QUESTION
from aiops_diagnostics.common.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.common.endpoint_policy import canonical_gateway_url
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import AgentRunConfig
from aiops_diagnostics.execution.run_deliverability import expired
from aiops_diagnostics.operator import operator_agent, operator_conversation
from aiops_diagnostics.operator.cpms_evidence import (
    FEEDBACK_REFUSALS,
    SCENARIO,
    SITE_TOOL,
    TOOL_NAMES,
    TOOL_REFUSED_PREFIX,
    TOOL_SUMMARY_FIELDS,
    _arg_summary,
    _declared,
    allowed_in,
    diagnoses_an_order,
    operator_scope,
    run_values,
    still_authorized,
    tool_request,
    validate_body,
)
from aiops_diagnostics.operator.operator_conversation import OperatorRefused


@dataclass(repr=False)
class ActiveTask:
    caller: object
    binding: object
    authorize: object
    bearer: str
    base_url: str
    proxy_url: str
    order_site_id: str
    grant: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    evidence: dict = field(default_factory=dict)
    delivered: set = field(default_factory=set)
    #: 每个工具这一次的取值（订单号或模型提议的关键词）。放行一次就固定，后续步骤只读它。
    arguments: dict = field(default_factory=dict)
    conversation_turn: tuple | None = None
    selected_order: str = ""
    active_order: str = ""
    messages: list = field(default_factory=list)
    history_runs: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    question: str = ""
    language: str = "zh"
    trial: bool = False
    candidate: bool = False
    #: 本次运行可接受答复未完成时最多重派一次。
    resamples: int = 0
    #: 本轮已回喂模型的业务拒绝（`FEEDBACK_REFUSALS` 里的码）。
    refusals: list = field(default_factory=list)
    lock: object = field(default_factory=threading.RLock)
    completion_lock: object = field(default_factory=threading.Lock)
    trace_lock: object = field(default_factory=threading.Lock)
    started_at: float = field(default_factory=time.monotonic)
    trace_failed: bool = False
    finished_elapsed_ms: int | None = None
    answer_review: dict = field(default_factory=dict)
    #: `_complete` 里的终局闸拒原码。
    completion_refusal: str | None = None
    qa_id: str = ""
    agent_id: str = "operator_main"
    attempt_no: int = 1
    parent_qa_id: str | None = None
    claim_id: str | None = None
    step_counter: int = 0
    open_steps: dict = field(default_factory=dict)


def trace_event(
    service: Any,
    run_id: str,
    phase: str,
    status: str = "running",
    *,
    tool: str | None = None,
    duration_ms: float | None = None,
    detail: Any = None,
    summary: str | None = None,
) -> None:
    task = service.tasks.get(run_id)
    if task is None or task.trace_failed:
        return
    with task.trace_lock:
        if task.finished_elapsed_ms is not None:
            return
        elapsed_ms = max(0, int((time.monotonic() - task.started_at) * 1000))
        event: dict[str, Any] = {
            "phase": phase,
            "status": status,
            "at": format_stored_time(now_utc()),
            "elapsed_ms": elapsed_ms,
        }
        if phase in {"completed", "failed", "cancelled", "expired"}:
            task.finished_elapsed_ms = elapsed_ms
            duration_ms = elapsed_ms
        if tool:
            event["tool"] = tool
        if duration_ms is not None:
            event["duration_ms"] = max(0, int(duration_ms))
        if detail:
            event["detail"] = str(detail)[:64]
        try:
            service.context.agent_run_store.record_trace(run_id, event)
        except sqlite3.Error:
            task.trace_failed = True
            logging.getLogger("aiops.operator").warning("operator_trace_unavailable")
        qa_events = getattr(service.context, "qa_events", None)
        if qa_events is not None and task.qa_id:
            from aiops_diagnostics.operator.qa_events import sanitize_step_summary, step_descriptor

            desc = step_descriptor(phase, tool, task.language)
            if desc is not None:
                node_type, node_code, title = desc
                key = (node_type, node_code)
                clean_summary = sanitize_step_summary(summary)
                try:
                    if status == "running":
                        task.step_counter += 1
                        step_id = f"st_{task.step_counter}"
                        task.open_steps[key] = step_id
                        qa_events.append(
                            task.qa_id,
                            "step_started",
                            {
                                "step_id": step_id,
                                "node_type": node_type,
                                "node_code": node_code,
                                "title": title,
                                "input_summary": clean_summary,
                                "elapsed_ms": elapsed_ms,
                            },
                        )
                    else:
                        step_id = task.open_steps.pop(key, None)
                        if step_id is None:
                            task.step_counter += 1
                            step_id = f"st_{task.step_counter}"
                        mapped = (
                            "succeeded"
                            if status in {"succeeded", "completed"}
                            else "skipped"
                            if status == "skipped"
                            else "failed"
                        )
                        qa_events.append(
                            task.qa_id,
                            "step_finished",
                            {
                                "step_id": step_id,
                                "node_type": node_type,
                                "node_code": node_code,
                                "title": title,
                                "status": mapped,
                                "duration_ms": max(0, int(duration_ms)) if duration_ms is not None else None,
                                "elapsed_ms": elapsed_ms,
                                "output_summary": clean_summary,
                            },
                        )
                except sqlite3.Error:
                    logging.getLogger("aiops.operator").warning("operator_qa_event_unavailable")


def summarize_tool(
    service: Any,
    run_id: str,
    task: ActiveTask | None,
    tool: str | None,
    *,
    refusal: str | None = None,
) -> None:
    """把该次工具调用的行级展示摘要写进运行记录（ADR-0251）。"""
    if task is None or not tool:
        return
    try:
        fields: dict[str, Any] = {}
        args = _arg_summary(task, tool)
        if args:
            fields["入参"] = args
        if refusal:
            fields["拒绝"] = str(refusal)
        data = (task.evidence.get(tool) or {}).get("data")
        if isinstance(data, dict):
            for source_key, label in TOOL_SUMMARY_FIELDS.get(tool, ()):
                if source_key in data:
                    fields[label] = data[source_key]
        elif isinstance(data, list):
            fields["结果"] = f"{len(data)}项"
        if fields:
            service.context.agent_run_store.record_tool_summary(run_id, tool, fields)
    except (sqlite3.Error, ValueError, TypeError):
        logging.getLogger("aiops.operator").warning("operator_tool_summary_unavailable")


def note_refusal(task: ActiveTask | None, tool: str | None, exc: OperatorRefused) -> None:
    """把一次会回喂模型的业务拒绝记到任务上，供答复复核区分「查无此单」与编造。"""
    code = str(exc)
    if task is None or code not in FEEDBACK_REFUSALS:
        return
    refusal = {"tool": tool or "", "code": code, "message": FEEDBACK_REFUSALS[code]}
    with task.lock:
        if refusal not in task.refusals and len(task.refusals) < 16:
            task.refusals.append(refusal)


def progress(
    service: Any, caller: Any, qa_id: str, qa: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """只在调用方完成当前入口授权后读取；不包含业务对象或答案。"""
    run = service.context.agent_run_store.by_qa(qa_id)
    if run is None or not operator_agent.is_agent(run):
        return None
    caller = service.refresh_scope(caller, allow_empty=True)
    if run.scope_fingerprint != caller.scope_fingerprint:
        return None
    task = service.tasks.get(run.run_id)
    if run.state in {"issued", "running"} and expired(run):
        run = service.context.agent_run_store.get(run.run_id)
    if run.state == "expired" and qa is not None and qa["status"] in QUESTION.active:
        service.context.store.update_assistant_question(qa_id, status="expired")
        qa.update(status="expired", result=None)
    terminal = run.state not in {"issued", "running", "succeeded"} or (
        qa is not None and qa["status"] == "completed"
    )
    phase = {"succeeded": "completed", "revoked": "cancelled"}.get(run.state, run.state)
    if terminal and task is not None:
        service._trace(run.run_id, phase, phase)
        run = service.context.agent_run_store.by_qa(qa_id)
    last = run.trace[-1] if run.trace else {}
    if not terminal:
        phase = last.get("phase", "queued")
        if phase == "tool" and last.get("status") != "running":
            phase = "generating"
    if task is not None and task.trace_failed:
        elapsed_ms = None
    elif terminal and task is not None and task.finished_elapsed_ms is not None:
        elapsed_ms = task.finished_elapsed_ms
    elif terminal and last.get("phase") in {"completed", "failed", "cancelled", "expired"}:
        elapsed_ms = last["elapsed_ms"]
    elif task is not None:
        elapsed_ms = int((time.monotonic() - task.started_at) * 1000)
    else:
        elapsed_ms = None
    from aiops_diagnostics.operator.qa_events import project_steps_from_trace

    return {
        "phase": phase,
        "elapsed_ms": max(0, elapsed_ms) if elapsed_ms is not None else None,
        "tool_count": len({e["tool"] for e in run.trace if e.get("tool") and e["status"] == "succeeded"}),
        "resamples": sum(e["phase"] == "resample" for e in run.trace),
        "steps": project_steps_from_trace(run.trace, language=getattr(run, "language", None) or "zh"),
    }


def console_identity(service: Any, session: str) -> dict[str, Any]:
    settings = service.context.settings
    if not settings.operator_trial_login_url or not settings.operator_trial_key or not session:
        raise OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")

    def unavailable(_: Any) -> OperatorRefused:
        return OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")

    result = request_json(
        RequestSpec(
            url=canonical_gateway_url(settings.operator_trial_login_url) + "/__aiops/internal/operator-token",
            method="POST",
            headers={
                "Authorization": "Bearer " + settings.operator_trial_key,
                "Content-Type": "application/json",
            },
            body=json.dumps({"session": session}).encode(),
            timeout=5,
            max_read_bytes=20000,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=unavailable,
            http_error=unavailable,
            unavailable=unavailable,
            invalid_body=unavailable,
            invalid_envelope=unavailable,
        ),
    )
    if not isinstance(result, dict) or any(
        not isinstance(result.get(k), str) or not result[k] for k in ("token", "user_id", "tenant_id")
    ):
        raise OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")
    return result


def refresh_scope(service: Any, caller: Any, *, allow_empty: bool = False) -> Any:
    from aiops_diagnostics.gateway.environment_registry import RegistryError, require_registered
    from aiops_diagnostics.identity.scope_context import DataScope

    try:
        require_registered(service.context.runtime, caller.effective_tenant_id)
    except RegistryError as exc:
        raise OperatorRefused("TENANT_ENVIRONMENT_NOT_ENABLED") from exc
    if operator_scope(caller, allow_empty=allow_empty).site_ids is None:
        return caller
    directory = getattr(service.context.runtime, "_trial_operator_scope", None)
    if directory is None:
        raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
    try:
        scope = directory.site_scope_by_b_user_id(caller.subject.b_user_id, caller.effective_tenant_id)
    except Exception as exc:  # noqa: BLE001 - 授权源失败不沿用旧范围。
        raise OperatorRefused("OPERATOR_SCOPE_UNAVAILABLE") from exc
    if scope.tenant_id != caller.effective_tenant_id or (not scope.site_ids and not allow_empty):
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    return replace(
        caller, data_scope=DataScope(type="organ", site_ids=tuple(scope.site_ids)), scope_fingerprint=""
    )


def prune_tasks(service: Any) -> None:
    for key, (_, _, end) in list(service.candidate_authorizations.items()):
        if time.monotonic() >= end:
            del service.candidate_authorizations[key]
    for key in list(service.tasks):
        run = service.context.agent_run_store.get(key)
        if run is None or expired(run) or run.state in {"failed", "revoked", "expired"}:
            del service.tasks[key]


def expire_candidate(service: Any, run_id: str) -> None:
    with service.lock:
        service.candidate_authorizations.pop(run_id, None)


def stamp_gate_refusal(service: Any, run_id: str, code: str) -> None:
    """闸拒把原始码写进 run.reason（首拒生效、终局幂等）。"""
    if code == "OPERATOR_TOOL_DISABLED" or code in FEEDBACK_REFUSALS:
        return
    try:
        if service.context.agent_run_store.fail(run_id, TOOL_REFUSED_PREFIX + code):
            logging.getLogger("aiops.operator").warning(
                "operator_gate_refused run_id=%s code=%s", run_id, code
            )
    except Exception:
        logging.getLogger("aiops.operator").warning(
            "operator_gate_refused stamp_failed run_id=%s code=%s", run_id, code
        )


def authenticate_inner(
    service: Any,
    run_id: str,
    *,
    node_token: str | None = None,
    manifest: str | None = None,
) -> tuple[Any, AgentRunConfig]:
    """run 存在 + 节点身份 + manifest——不判断「这次执行还许不许可」。"""
    run = service.context.agent_run_store.get(run_id)
    if run is None or run.operation not in {
        SCENARIO,
        operator_conversation.SCENARIO,
        "operator_agent",
    }:
        raise OperatorRefused("OPERATOR_RUN_INACTIVE")
    binding = chat_execution.binding_for_key(service.context.settings, run.chat_binding)
    if binding is None:
        raise OperatorRefused("OPERATOR_NODE_REJECTED")
    config = AgentRunConfig.load(Path(binding.config_file))
    if node_token is not None:
        node = config.authenticate_node(node_token)
        if node is None or node.node_id != run.expected_node_id or node.environment != run.environment:
            raise OperatorRefused("OPERATOR_NODE_REJECTED")
    expected = execution_manifest.from_text(run.execution_manifest)
    if expected.identity != run.expected_agent_version:
        raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
    if manifest is not None and manifest != expected.identity:
        raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
    return run, config


def authorize_execution_inner(
    service: Any,
    run_id: str,
    *,
    run: Any,
    config: AgentRunConfig,
    node_token: str | None = None,
    manifest: str | None = None,
    tool: str | None = None,
) -> tuple[ActiveTask, Any, AgentRunConfig]:
    task = service.tasks.get(run_id)
    if task is None or run.state not in {"issued", "running"}:
        raise OperatorRefused("OPERATOR_RUN_INACTIVE")
    qa = service.context.store.get_assistant_question(run.qa_id, run.scope_fingerprint)
    if not qa or qa["status"] not in QUESTION.active:
        raise OperatorRefused("OPERATOR_RUN_INACTIVE")
    binding = chat_execution.binding_for_key(service.context.settings, run.chat_binding)
    if not still_authorized(binding, run, trial=task.trial) or binding.entry != "operator":
        raise OperatorRefused("OPERATOR_PUBLICATION_CHANGED")
    from aiops_diagnostics.execution.node_publication import all_targets

    if not any(
        t.entry == "operator"
        and t.app_id == binding.app_id
        and t.tenant_id == task.caller.effective_tenant_id
        and t.environment == run.environment
        and t.cpms_base_url.rstrip("/") == task.base_url
        and t.proxy_base_url.rstrip("/") == task.proxy_url
        for t in all_targets(service.context.settings)
    ):
        raise OperatorRefused("OPERATOR_TARGET_CHANGED")
    if (config.environment, config.snail_app_id, str(config.snail_agent_id), config.expected_node_id) != (
        run.environment,
        run.app_id,
        run.agent_id,
        run.expected_node_id,
    ):
        raise OperatorRefused("OPERATOR_NODE_CHANGED")
    if not any(
        n.active and n.node_id == run.expected_node_id and n.environment == run.environment
        for n in config.node_token_hashes.values()
    ):
        raise OperatorRefused("OPERATOR_NODE_REJECTED")
    caller = task.authorize()
    if caller.scope_fingerprint != run.scope_fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    binding = chat_execution.binding_for_key(service.context.settings, run.chat_binding)
    if not still_authorized(binding, run, trial=task.trial):
        raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
    run, config = service._authenticate(run_id, node_token=node_token, manifest=manifest)
    if (config.environment, config.snail_app_id, str(config.snail_agent_id), config.expected_node_id) != (
        run.environment,
        run.app_id,
        run.agent_id,
        run.expected_node_id,
    ):
        raise OperatorRefused("OPERATOR_NODE_CHANGED")
    if not any(
        n.active and n.node_id == run.expected_node_id and n.environment == run.environment
        for n in config.node_token_hashes.values()
    ):
        raise OperatorRefused("OPERATOR_NODE_REJECTED")
    expected = execution_manifest.from_text(run.execution_manifest)
    if tool is not None and (
        tool not in TOOL_NAMES or tool not in expected.tool_names or not allowed_in(tool, run)
    ):
        raise OperatorRefused("OPERATOR_TOOL_DISABLED")
    latest = service.context.agent_run_store.get(run_id)
    if latest is None or latest.state not in {"issued", "running"} or expired(latest):
        raise OperatorRefused("OPERATOR_RUN_INACTIVE")
    return task, latest, config


def absorb_superseded(service: Any, run_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """授权已死后送达的交付物：收下进终局快照、标记 superseded、永不投递。"""
    store = service.context.agent_run_store
    store.absorb(run_id, {"superseded": True, **patch})
    latest = store.get(run_id)
    return {
        "run_id": run_id,
        "state": latest.state if latest else "failed",
        "delivered": False,
    }


def authorized_order(
    task: ActiveTask,
    order: str,
    *,
    cpms_get_fn: Callable[..., Any],
    expected_site: str = "",
) -> dict[str, Any]:
    current = task.authorize()
    if current.scope_fingerprint != task.caller.scope_fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    body = cpms_get_fn(
        task.base_url + tool_request(TOOL_NAMES[0], {"order_no": order})["path"],
        task.bearer,
        current.effective_tenant_id,
    )
    validate_body(body, current, order, TOOL_NAMES[0])
    if expected_site and body["data"]["siteId"] != expected_site:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    if task.authorize().scope_fingerprint != current.scope_fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    return body


def authorize_snapshot(
    service: Any,
    task: ActiveTask,
    run: Any,
    *,
    cpms_get_fn: Callable[..., Any],
) -> None:
    from aiops_diagnostics.operator import offline_tool

    refs = run.snapshot.get("evidence", {}).get(offline_tool.NAME, {}).get("offline_references")
    if refs is not None:
        try:
            offline_tool.require_current(service, task, refs)
        except SourceError as exc:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED") from exc
    scope = operator_scope(task.authorize(), allow_empty=True)
    evidence = run.snapshot.get("evidence", {})
    if run.order_no:
        detail = evidence.get(TOOL_NAMES[0], {}).get("data", {})
        if not detail.get("siteId"):
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        service._authorized_order(task, run.order_no, expected_site=detail["siteId"])
    for name, item in evidence.items():
        data = item["data"]
        sites = [row["id"] for row in data.get("records", [])] if name == SITE_TOOL else []
        if data.get("site_id"):
            sites.append(data["site_id"])
        for site in sites:
            if scope.site_ids is not None and site not in scope.site_ids:
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            body = cpms_get_fn(
                task.base_url + "/chSite/" + site, task.bearer, task.caller.effective_tenant_id
            )
            row = body.get("data") if isinstance(body, dict) and body.get("code") == 0 else None
            if not isinstance(row, dict) or (row.get("id"), row.get("tenantId")) != (
                site,
                task.caller.effective_tenant_id,
            ):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    if task.authorize().scope_fingerprint != task.caller.scope_fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")


_MAX_SINGLE_USER_MESSAGE_UTF16 = 44_000
_MAX_TOTAL_MESSAGES_UTF16 = 48_000


def _utf16_len(text: str) -> int:
    """精确度量 Java `String.length()` 对应的 UTF-16 代码单元数。"""
    return len(text.encode("utf-16-le")) // 2


def build_messages(service: Any, task: ActiveTask) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    task.history_runs = []
    if task.conversation_turn:
        conversation, scope, before = task.conversation_turn
        store = service.context.runtime.conversation_store
        current = store.get(conversation, scope)
        task.active_order = current.get("active_order_no") or ""
        if task.active_order:
            try:
                service._authorized_order(task, task.active_order)
            except OperatorRefused:
                task.active_order = ""
        for turn in store.completed_context(conversation, scope, before=before):
            previous = service.context.agent_run_store.by_qa(turn.get("status_qa_id"))
            if not previous or previous.scope_fingerprint != scope or not previous.snapshot:
                continue
            if previous.publication_app_id != task.binding.app_id:
                continue
            try:
                service._authorize_snapshot(task, previous)
                inherited = previous.snapshot.get("history_runs", [])
                if len(inherited) > 60:
                    raise OperatorRefused("OPERATOR_HISTORY_LIMIT")
                for dependency in inherited:
                    source = service.context.agent_run_store.peek(dependency)
                    if source is None or source.scope_fingerprint != scope or not source.snapshot:
                        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                    service._authorize_snapshot(task, source)
            except OperatorRefused as exc:
                if str(exc) == "OPERATOR_HISTORY_LIMIT":
                    raise
                continue
            answer = "\n".join(b.get("text", "") for b in (turn.get("answer") or {}).get("blocks", []))
            if len(answer) > 10000:
                continue
            dependencies = [key for key in [previous.run_id, *inherited] if key not in task.history_runs]
            if len(task.history_runs) + len(dependencies) > 60:
                raise OperatorRefused("OPERATOR_HISTORY_LIMIT")
            task.history_runs.extend(dependencies)
            messages.extend(
                [
                    {"role": "user", "content": turn["question"][:4000]},
                    {"role": "assistant", "content": answer},
                ]
            )
    user_payload: dict[str, Any] = {
        "question": task.question,
        "page_selected_order": task.selected_order,
        "conversation_order": task.active_order,
        "language": task.language,
        "current_time": format_stored_time(now_utc()),
        "business_timezone": service.context.runtime.diagnostic_settings.mysql.timezone,
        "history_is_previous_observation": True,
    }
    if task.attachments:
        from aiops_diagnostics.operator.attachments import (
            MAX_TURN_ATTACHMENT_CONTEXT_CHARS,
            UNTRUSTED_ATTACHMENT_POLICY,
            build_untrusted_attachment_context,
        )

        user_payload["untrusted_attachment_policy"] = UNTRUSTED_ATTACHMENT_POLICY
        envelope_overhead = _utf16_len(json.dumps(user_payload, ensure_ascii=False))
        available = max(
            2_000,
            min(
                MAX_TURN_ATTACHMENT_CONTEXT_CHARS, _MAX_SINGLE_USER_MESSAGE_UTF16 - envelope_overhead - 1_024
            ),
        )
        user_payload["untrusted_user_attachments"] = build_untrusted_attachment_context(
            task.attachments, max_total_chars=available
        )
        serialized_user = json.dumps(user_payload, ensure_ascii=False)
        if _utf16_len(serialized_user) > _MAX_SINGLE_USER_MESSAGE_UTF16:
            overflow = _utf16_len(serialized_user) - _MAX_SINGLE_USER_MESSAGE_UTF16 + 1_024
            shrunk = max(0, available - overflow)
            user_payload["untrusted_user_attachments"] = build_untrusted_attachment_context(
                task.attachments, max_total_chars=shrunk
            )
            serialized_user = json.dumps(user_payload, ensure_ascii=False)
    else:
        serialized_user = json.dumps(user_payload, ensure_ascii=False)
    messages.append(
        {
            "role": "user",
            "content": serialized_user,
        }
    )
    while (
        _utf16_len(json.dumps(messages, ensure_ascii=False)) > _MAX_TOTAL_MESSAGES_UTF16 and len(messages) > 1
    ):
        del messages[:2]
    return messages


def authorize_readback(
    service: Any,
    caller: Any,
    qa_id: str,
    bearer: str,
    *,
    cpms_get_fn: Callable[..., Any],
    candidate_authorize: Any = None,
) -> dict[str, Any]:
    run = service.context.agent_run_store.by_qa(qa_id)
    if candidate_authorize is not None:
        with service.lock:
            service._prune()
            lease = service.candidate_authorizations.get(run.run_id) if run else None
        if lease is None or candidate_authorize is not lease[0]:
            raise OperatorRefused("OPERATOR_TRIAL_SCOPE_MISMATCH")
        caller = candidate_authorize()
    else:
        caller = service.refresh_scope(caller, allow_empty=bool(run and not diagnoses_an_order(run)))
    if run is None or run.scope_fingerprint != caller.scope_fingerprint:
        raise OperatorRefused("QA_NOT_FOUND")
    if run.state != "succeeded" or not run.snapshot:
        raise OperatorRefused("OPERATOR_RESULT_UNAVAILABLE")
    from aiops_diagnostics.execution.node_publication import all_targets

    binding = chat_execution.binding_for_key(service.context.settings, run.chat_binding)
    if not still_authorized(binding, run, trial=bool((run.snapshot or {}).get("trial"))) or expired(run):
        raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
    target = next(
        (
            t
            for t in all_targets(service.context.settings)
            if t.entry == "operator"
            and t.app_id == binding.app_id
            and t.tenant_id == caller.effective_tenant_id
            and t.environment == run.environment
        ),
        None,
    )
    if target is None:
        raise OperatorRefused("OPERATOR_TARGET_MISSING")
    if operator_agent.is_agent(run):
        task = ActiveTask(
            caller,
            binding,
            candidate_authorize or (lambda: service.refresh_scope(caller, allow_empty=True)),
            bearer,
            target.cpms_base_url,
            target.proxy_base_url,
            "",
        )
        service._authorize_snapshot(task, run)
        for previous_id in run.snapshot.get("history_runs", []):
            previous = service.context.agent_run_store.peek(previous_id)
            if (
                previous is None
                or previous.scope_fingerprint != caller.scope_fingerprint
                or not previous.snapshot
            ):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            service._authorize_snapshot(task, previous)
        return service.result(caller, qa_id)
    if not diagnoses_an_order(run):
        plan = (run.snapshot or {}).get("evidence", {}).get("cpms_station_plan", {}).get("data", {})
        if plan.get("site_id"):
            body = cpms_get_fn(
                target.cpms_base_url + "/chSite/" + plan["site_id"], bearer, caller.effective_tenant_id
            )
            site = body.get("data") if isinstance(body, dict) and body.get("code") == 0 else None
            if (
                not isinstance(site, dict)
                or site.get("id") != plan["site_id"]
                or site.get("tenantId") != caller.effective_tenant_id
            ):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        return service.result(caller, qa_id)
    body = cpms_get_fn(
        target.cpms_base_url + tool_request(TOOL_NAMES[0], run_values(TOOL_NAMES[0], run.order_no))["path"],
        bearer,
        caller.effective_tenant_id,
    )
    validate_body(body, caller, run.order_no, TOOL_NAMES[0])
    if body["data"]["siteId"] != run.snapshot["evidence"][TOOL_NAMES[0]]["data"]["siteId"]:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    refreshed = service.refresh_scope(caller)
    if refreshed.scope_fingerprint != caller.scope_fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    return service.result(caller, qa_id)


def verify_live_stream_access(
    service: Any,
    caller: Any,
    qa_id: str,
    bearer: str,
    *,
    cpms_get_fn: Callable[..., Any],
) -> None:
    """对运行中/失败/取消态的管家问答 SSE 实时复核当前账号范围与已触及订单/站点权限。"""
    run = service.context.agent_run_store.by_qa(qa_id)
    if run is None:
        raise OperatorRefused("QA_NOT_FOUND")
    task = service.tasks.get(run.run_id)
    needs_order = bool(run.order_no or (task is not None and task.order_site_id))
    current = service.refresh_scope(caller, allow_empty=not needs_order)
    if run.scope_fingerprint != current.scope_fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    binding = chat_execution.binding_for_key(service.context.settings, run.chat_binding)
    trial = bool((run.snapshot or {}).get("trial")) or bool(task and task.trial)
    if not still_authorized(binding, run, trial=trial):
        raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
    from aiops_diagnostics.execution.node_publication import all_targets

    target = next(
        (
            t
            for t in all_targets(service.context.settings)
            if t.entry == "operator"
            and t.app_id == binding.app_id
            and t.tenant_id == current.effective_tenant_id
            and t.environment == run.environment
        ),
        None,
    )
    if target is None:
        raise OperatorRefused("OPERATOR_TARGET_MISSING")

    check_task = ActiveTask(
        current,
        binding,
        lambda: service.refresh_scope(current, allow_empty=not needs_order),
        bearer,
        target.cpms_base_url,
        target.proxy_base_url,
        task.order_site_id if task is not None else "",
    )
    snapshot_evidence = (run.snapshot or {}).get("evidence") or {}
    if run.order_no:
        expected_site = (task.order_site_id if task is not None else "") or str(
            snapshot_evidence.get(TOOL_NAMES[0], {}).get("data", {}).get("siteId") or ""
        )
        service._authorized_order(check_task, run.order_no, expected_site=expected_site)
    if run.snapshot and snapshot_evidence:
        service._authorize_snapshot(check_task, run)
    if task is not None:
        for previous_id in list(task.history_runs):
            previous = service.context.agent_run_store.peek(previous_id)
            if (
                previous is None
                or previous.scope_fingerprint != current.scope_fingerprint
                or not previous.snapshot
            ):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            service._authorize_snapshot(check_task, previous)
    if task is not None and task.evidence:
        scope = operator_scope(current, allow_empty=True)
        with task.lock:
            live_items = list(task.evidence.items())
        for name, item in live_items:
            data = (item or {}).get("data") if isinstance(item, dict) else None
            if not isinstance(data, dict):
                continue
            sites = (
                [row["id"] for row in data.get("records", []) if isinstance(row, dict) and row.get("id")]
                if name == SITE_TOOL
                else []
            )
            if data.get("site_id"):
                sites.append(data["site_id"])
            for site in sites:
                if scope.site_ids is not None and site not in scope.site_ids:
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                body = cpms_get_fn(
                    target.cpms_base_url + "/chSite/" + site, bearer, current.effective_tenant_id
                )
                row = body.get("data") if isinstance(body, dict) and body.get("code") == 0 else None
                if not isinstance(row, dict) or (row.get("id"), row.get("tenantId")) != (
                    site,
                    current.effective_tenant_id,
                ):
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")


def previous_plan(service: Any, task: ActiveTask) -> dict[str, Any]:
    turn = task.conversation_turn
    if not turn:
        return {}
    store = service.context.runtime.conversation_store
    previous = store.previous_completed_qa(*turn)
    run = service.context.agent_run_store.by_qa(previous) if previous else None
    if not run or run.scope_fingerprint != task.caller.scope_fingerprint or not run.snapshot:
        return {}
    return run.snapshot.get("plan_arguments", {})


def grant_sites(task: ActiveTask, tool: str) -> list[str] | None:
    """这一次授权允许碰的站点（订单诊断收窄到订单站点；站点工具保持账号可见站点集）。"""
    if "order_no" in _declared(tool).inputs:
        if not task.order_site_id:
            raise OperatorRefused("OPERATOR_ORDER_REQUIRED")
        return [task.order_site_id]
    scope = operator_scope(task.caller, allow_empty=True)
    return list(scope.site_ids) if scope.site_ids is not None else None


def retract_tasks(service: Any, route: Any) -> None:
    with service.lock:
        tasks = list(service.tasks.items())
    for run_id, task in tasks:
        run = service.context.agent_run_store.get(run_id)
        if run and run.chat_binding == route.key and run.route_revision <= route.revision:
            service.context.runtime.cancel_assistant_qa(task.caller, run.qa_id)
