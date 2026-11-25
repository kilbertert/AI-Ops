"""Gateway C 端订单问答分发、节点/Dify 运行回读守卫与授权快照读取。

本模块承接原 ``gateway_api.py`` 中的 C 端订单问答分发与运行结果回读守卫逻辑，
通过显式回调参数接驳认证、会话槽位与可测试数据源，保持单向依赖（不反向导入
``gateway_api``）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import status
from fastapi.responses import JSONResponse

from aiops_diagnostics import __version__
from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE, rendering_language
from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file, write_private_text
from aiops_diagnostics.consumer import consumer_knowledge
from aiops_diagnostics.consumer import knowledge_access as consumer_knowledge_selection
from aiops_diagnostics.consumer.presentation import (
    COMPOSED_LANGUAGES,
    asks_latest_order,
    status_answer,
)
from aiops_diagnostics.conversation.clarification import CLARIFY_ORDER_REQUIRED
from aiops_diagnostics.conversation.conversation_store import ConversationError
from aiops_diagnostics.data.charging_status import ChargingStatusError, read_charging_status
from aiops_diagnostics.data.sources import SourceError, scoped_mysql_source
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.execution.agent_runs import AgentRun, scope_from_json
from aiops_diagnostics.execution.run_deliverability import deliverable
from aiops_diagnostics.gateway import metrics_store as metrics_dims
from aiops_diagnostics.gateway import request_observation
from aiops_diagnostics.gateway.dependencies import STANDARD_ORDER_READ_SCOPE
from aiops_diagnostics.gateway.dify_publish_ops import _tenant_in_scope
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.identity.caller_auth import SOURCE_KEY_HEADER
from aiops_diagnostics.identity.order_identity import (
    extract_order_identifier,
    is_order_identifier,
)
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import (
    SCOPE_TYPE_SELF,
    ScopeContext,
    ScopeError,
)

_LOGGER = logging.getLogger("aiops.gateway")

#: The three answers an order authorization check can give.
OWNED = "owned"
NOT_OWNED = "not_owned"
UNAVAILABLE = "unavailable"

ORDER_AUTH_ROUTE = "order_auth"
ORDER_AUTHENTICATION_UNAVAILABLE = "ORDER_AUTHORIZATION_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ChatDispatchHooks:
    """由 ``gateway_api`` 注入的认证、会话、指标与数据源边界回调。"""

    authenticate_caller: Callable[..., Any]
    order_authorization: Callable[[Any, ScopeContext, str], str]
    begin_conversation_turn: Callable[..., int | None]
    release_conversation_turn: Callable[..., None]
    assistant_question_response: Callable[[dict[str, Any], str], dict[str, Any]]
    clarification: Callable[..., dict[str, Any]]
    record_route_metric: Callable[..., None]
    require_operator_company_token: Callable[[Any], None]
    scoped_mysql_source_fn: Callable[..., Any] = scoped_mysql_source
    write_private_text_fn: Callable[[Path, str], None] = write_private_text
    read_charging_status_fn: Callable[..., dict[str, Any]] = read_charging_status
    authorized_charging_snapshot_fn: Callable[..., dict[str, Any]] | None = None
    consumer_mall_payment_fn: Callable[..., Any] | None = None


def _require_matching_exec_config(
    context: Any, run: AgentRun, observed: str | None, *, actual_node_id: str | None = None
) -> AgentRun:
    """领取时对照执行配置身份。**签发时固定、领取时对照、并把实际值落库。**"""
    expected = run.expected_agent_version
    if expected is None:
        return run
    confirmed = context.agent_run_store.confirm_exec_config(
        run.run_id, observed, actual_node_id=actual_node_id
    )
    if confirmed is None:
        raise StandardAPIError(status.HTTP_403_FORBIDDEN, "AGENT_RUN_EXEC_CONFIG_DRIFT", "agent run refused")
    return confirmed


def _require_consumer_self_order(caller: ScopeContext, business_entry: str | None, order_no: str) -> None:
    """两个充电状态入口共用的入口判据与订单号形状。"""
    if (
        (business_entry or "").strip().lower() != "consumer"
        or caller.delegated
        or caller.data_scope.type != SCOPE_TYPE_SELF
        or not caller.subject.c_user_id
    ):
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "consumer self scope required")
    if not is_order_identifier(order_no):
        raise StandardAPIError(400, "INVALID_ORDER_NO", "invalid order number")


def _require_consumer_self(caller: ScopeContext, business_entry: str | None) -> None:
    """C 端本人入口判据，**与订单无关**的那一半。"""
    if (
        (business_entry or "").strip().lower() != "consumer"
        or caller.delegated
        or caller.data_scope.type != SCOPE_TYPE_SELF
        or not caller.subject.c_user_id
    ):
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "consumer self scope required")


def _require_visible_order(
    context: Any,
    caller: ScopeContext,
    order_no: str,
    *,
    order_authorization_fn: Callable[[Any, ScopeContext, str], str],
) -> None:
    """既有订单可见性判据；越权与不存在同形。返回 None，失败即抛。"""
    verdict = order_authorization_fn(context, caller, order_no)
    if verdict == UNAVAILABLE:
        raise StandardAPIError(
            503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    if verdict == NOT_OWNED:
        raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")


def _consumer_order_chat_via_node(
    context,
    caller,
    decision,
    payload,
    conversation,
    language,
    order_no,
    binding,
    *,
    hooks: ChatDispatchHooks,
):
    """聊天入口的节点路线：**只做授权与建 QA，然后把等待搬到后台**。"""
    if order_no is None:
        request_observation.mark_stage(None, request_observation.STAGE_ORDER_CHAT_LOOKUP)
        try:
            scope = resolve_query_scope(caller)
            with hooks.scoped_mysql_source_fn(
                Settings.from_config(context.settings.server_config_file), scope
            ) as source:
                rows = source.list_orders(page=1, page_size=1)
        except (SourceError, ScopeError, ValueError) as exc:
            raise StandardAPIError(
                503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order lookup unavailable", retryable=True
            ) from exc
        if not rows:
            raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
        order_no = str(rows[0]["order_no"])
    _require_consumer_self_order(caller, str(decision.platform), order_no)
    _require_visible_order(context, caller, order_no, order_authorization_fn=hooks.order_authorization)

    turn_no = hooks.begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    try:
        qa = context.runtime.start_chat_order_qa(
            caller,
            payload.question,
            order_no=order_no,
            language=language,
            conversation=conversation,
            conversation_turn_no=turn_no,
            binding=binding,
        )
    except Exception:
        hooks.release_conversation_turn(context, conversation, turn_no)
        raise
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        headers={"Cache-Control": "no-store"},
        content={
            **hooks.assistant_question_response(qa, language),
            **(
                {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                if conversation
                else {}
            ),
        },
    )


def _consumer_intent_clarification(code, language):
    from aiops_diagnostics.consumer.consumer_intent import message

    return JSONResponse(
        status_code=200,
        headers={"Cache-Control": "no-store"},
        content={
            "type": "clarification",
            "code": "CONSUMER_" + code.upper(),
            "missing_fields": ["order_no"] if code == "order" else [],
            "message": message(code, language),
            "language": language,
        },
    )


def authenticated_chat_order_reader(
    context,
    caller,
    request,
    *,
    authenticate_caller_fn: Callable[..., Any],
):
    _require_consumer_self_order(caller, request.headers.get("X-Business-Entry"), "pending")
    reader = authenticate_caller_fn(
        context.caller_resolver,
        request.headers.get("Authorization"),
        request.headers.get("X-Third-Session"),
        platform_entry="consumer",
        source_key=request.headers.get(SOURCE_KEY_HEADER),
        required_scope=STANDARD_ORDER_READ_SCOPE,
    )
    if reader.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(403, "CONSUMER_SCOPE_REQUIRED", "order read identity changed")


def _consumer_order_chat(
    context,
    caller,
    decision,
    payload,
    conversation,
    language,
    request,
    *,
    hooks: ChatDispatchHooks,
):
    order_no = payload.order_no or extract_order_identifier(payload.question)
    authenticated_chat_order_reader(
        context, caller, request, authenticate_caller_fn=hooks.authenticate_caller
    )
    if order_no is None and not asks_latest_order(payload.question):
        return hooks.clarification(
            context,
            caller,
            decision,
            code=CLARIFY_ORDER_REQUIRED,
            question=payload.question,
            language=language,
        )
    try:
        binding = chat_execution.select_binding(context.settings, caller)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is not None:
        return _consumer_order_chat_via_node(
            context, caller, decision, payload, conversation, language, order_no, binding, hooks=hooks
        )

    charging_snapshot_fn = hooks.authorized_charging_snapshot_fn or (
        lambda s, c, o: _authorized_charging_snapshot(
            s, c, o, read_charging_status_fn=hooks.read_charging_status_fn
        )
    )
    turn_no = hooks.begin_conversation_turn(context, caller, conversation, "qa", payload.question)
    qa = None
    try:
        if order_no is None:
            request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_LOOKUP)
            try:
                scope = resolve_query_scope(caller)
                with hooks.scoped_mysql_source_fn(
                    Settings.from_config(context.settings.server_config_file), scope
                ) as source:
                    rows = source.list_orders(page=1, page_size=1)
            except (SourceError, ScopeError, ValueError) as exc:
                raise StandardAPIError(
                    503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order lookup unavailable", retryable=True
                ) from exc
            if not rows:
                raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")
            order_no = str(rows[0]["order_no"])
        _require_consumer_self_order(caller, str(decision.platform), order_no)
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_AUTH)
        _require_visible_order(context, caller, order_no, order_authorization_fn=hooks.order_authorization)
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_CPMS)
        try:
            snapshot = charging_snapshot_fn(context.settings, caller, order_no)
        except ChargingStatusError as exc:
            raise StandardAPIError(
                502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable", retryable=True
            ) from exc
        _require_visible_order(context, caller, order_no, order_authorization_fn=hooks.order_authorization)
        answer = status_answer(snapshot, language)
        qa = context.store.create_assistant_question(
            caller.scope_fingerprint,
            payload.question,
            tenant_id=caller.effective_tenant_id,
            internal_run_id="consumer-order-chat:" + order_no,
        )
        evidence = {
            "call_id": qa["qa_id"],
            "scope_fingerprint": caller.scope_fingerprint,
            "source": snapshot["source"],
            "answer_sha256": hashlib.sha256(json.dumps(answer, sort_keys=True).encode()).hexdigest(),
            "snapshot": snapshot,
            "result_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        }
        request_observation.mark_qa(request, qa["qa_id"])
        request_observation.mark_stage(request, request_observation.STAGE_ORDER_CHAT_EVIDENCE)
        try:
            hooks.write_private_text_fn(
                Path(context.settings.data_home) / "consumer-order-chat" / (qa["qa_id"] + ".json"),
                json.dumps(evidence, ensure_ascii=False),
            )
            _require_visible_order(
                context, caller, order_no, order_authorization_fn=hooks.order_authorization
            )
            if conversation is not None:
                context.conversation_store.complete_turn(
                    conversation["conversation_id"],
                    caller.scope_fingerprint,
                    turn_no,
                    answer=answer,
                    status_qa_id=qa["qa_id"],
                )
            if not context.store.update_assistant_question(qa["qa_id"], status="completed", result=answer):
                raise OSError("chat result write refused")
        except (OSError, PrivatePathError, sqlite3.Error, ConversationError) as exc:
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable", retryable=True
            ) from exc
        hooks.record_route_metric(
            context,
            caller,
            route_type=metrics_dims.ROUTE_QA,
            outcome="completed",
            conversation_id=conversation.get("conversation_id") if conversation else None,
        )
        qa.update(status="completed", result=answer)
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store"},
            content={
                **hooks.assistant_question_response(qa, language),
                **(
                    {"conversation_id": conversation["conversation_id"], "turn_no": turn_no}
                    if conversation
                    else {}
                ),
            },
        )
    except Exception as exc:
        if qa is not None:
            with contextlib.suppress(Exception):
                context.store.update_assistant_question(qa["qa_id"], status="failed", error_code="QA_FAILED")
        with contextlib.suppress(Exception):
            hooks.release_conversation_turn(context, conversation, turn_no)
        if isinstance(exc, (OSError, PrivatePathError, sqlite3.Error, ConversationError)):
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable", retryable=True
            ) from exc
        raise


def _version_moved(binding: Any, run: Any) -> bool:
    """这条绑定是不是已经指向**另一版**了。"""
    frozen = int(getattr(run, "agent_version_no", 0) or 0)
    current = int(getattr(binding, "agent_version_no", 0) or 0)
    revision = int(getattr(run, "route_revision", 0) or 0)
    return (bool(frozen) and frozen != current) or (
        bool(revision) and revision != int(getattr(binding, "route_revision", 0) or 0)
    )


def _route_binding_state(context: Any, key: str | None) -> str:
    """这条 key 在管理表里的状态（`active` / `retracted`），没有管理行时返回空串。"""
    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database or not key:
        return ""
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    try:
        row = ChatRouteBindingStore(Path(database).expanduser().absolute()).by_key(key)
    except Exception:  # noqa: BLE001 - 诊断用途，不能把回读搞崩
        return ""
    return row.state if row is not None else ""


def _require_trial_source_route(context: Any, run) -> None:
    """试用的源路由是否仍是签发时那一条。"""
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    database = str(getattr(context.settings, "chat_route_bindings_file", "") or "")
    if not database:
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable")
    try:
        current = ChatRouteBindingStore(Path(database).expanduser().absolute()).by_key(run.binding_key)
    except Exception as exc:  # noqa: BLE001 - 读不出来就不能放行
        raise StandardAPIError(503, "ROUTE_BINDING_UNAVAILABLE", "route state unavailable") from exc
    if current is None or not current.active:
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "trial source route is gone")
    frozen = int(getattr(run, "route_revision", 0) or 0)
    if frozen and current.revision != frozen:
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "trial source route changed")


def _require_dify_binding_current(context: Any, run) -> None:
    """那条 Dify 绑定**此刻**仍然成立，且仍是签发时那一条。"""
    if getattr(run, "kind", "chat") == "trial":
        _require_trial_source_route(context, run)
        return

    try:
        binding = chat_execution.binding_for_key(context.settings, run.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if (
        binding is None
        or binding.engine != chat_execution.ENGINE_DIFY
        or binding.workflow_id != run.workflow_id
    ):
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "dify binding is no longer active")
    if _version_moved(binding, run):
        raise StandardAPIError(403, "CHARGING_STATUS_DISABLED", "dify binding moved to another version")


def _same_tenant_as_grant(record_tenant: object, grant_tenant: object) -> bool:
    """记录上的租户与凭据上的租户是不是同一个 —— 用共享规则判，不就地写比较。"""
    from aiops_diagnostics.identity.order_visibility import (
        TenantVisibility,
        normalize_tenant,
        visible_orders,
    )

    allowed = normalize_tenant(grant_tenant)
    if allowed is None:
        return False
    visibility = TenantVisibility(allowed=frozenset({allowed}))
    return bool(visible_orders([{"tenant_id": record_tenant}], visibility).rows)


def _require_workflow_trial_result(
    context: Any,
    caller: ScopeContext,
    trial: dict,
    *,
    order_authorization_fn: Callable[[Any, ScopeContext, str], str],
) -> None:
    """管理 Workflow 试用的缓存正文与执行时采用同一授权/路由/有效期边界。"""
    result = trial.get("result") or {}
    report = result.get("report") or {}
    if trial.get("internal_run_id") == "node-sop-trial":
        run = context.agent_run_store.by_qa(trial["diagnosis_id"])
        if run is None or run.order_no != trial["order_no"]:
            raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "node trial evidence unavailable")
        _require_visible_order(context, caller, run.order_no, order_authorization_fn=order_authorization_fn)
        try:
            released = chat_execution.final_release(
                store=context.agent_run_store, settings=context.settings, caller=caller, run_id=run.run_id
            )
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc)) from exc
        expected = chat_execution.answer_from_run(released, language=result.get("language", "zh"))
        if (
            report.get("summary") != expected["blocks"][0]["text"]
            or report.get("node_run_id") != released.run_id
            or report.get("manifest_sha256") != released.observed_agent_version
        ):
            raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "node trial evidence differs")
        return
    located = context.dify_run_store.by_qa(trial["diagnosis_id"])
    if (
        trial.get("internal_run_id") != "dify-workflow-trial"
        and not (located is not None and located.kind == "trial")
        and not report.get("workflow_run_id")
    ):
        return
    if located is not None and located.kind == "service_trial":
        if located.data_kind == "agent":
            from aiops_diagnostics.consumer.consumer_agent import order_ids

            for order in order_ids(context.dify_run_store.context(located.run_id)):
                _require_visible_order(context, caller, order, order_authorization_fn=order_authorization_fn)
        if trial["order_no"]:
            _require_visible_order(
                context, caller, trial["order_no"], order_authorization_fn=order_authorization_fn
            )
        if not located.order_no and report.get("callback_version", 1) >= 3:
            try:
                consumer_knowledge_selection.require_owned(
                    report.get("knowledge_base_ids", []),
                    consumer_knowledge.owned_libraries(
                        context.runtime, caller.effective_tenant_id, located.app_id
                    ),
                )
            except ValueError as exc:
                raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "所选知识库授权已变化") from exc
        if trial.get("trial_managed_draft"):
            from aiops_diagnostics.dify.self_service_store import SelfServiceStore

            source = SelfServiceStore(context.settings.database_file).app(
                trial.get("trial_source_app_id", "")
            )
            if (
                not source
                or not _tenant_in_scope(source["tenant_id"], located.tenant_id)
                or source["entry"] != "consumer"
                or source["environment"] != located.environment
                or trial.get("trial_source_app_id") != source["app_id"]
            ):
                raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial source unavailable")
        else:
            _require_trial_source_route(context, located)
        latest = context.dify_run_store.get(located.run_id)
        snapshot = context.dify_run_store.snapshot(located.run_id)
        if (
            located.scope_fingerprint != caller.scope_fingerprint
            or latest is None
            or not deliverable(latest)
            or not snapshot
            or report.get("summary")
            != (
                "\n\n".join(b["text"] for b in snapshot["blocks"] if b.get("kind") == "text")
                if located.data_kind == "agent"
                else snapshot["blocks"][0]["text"]
            )
            or any(
                report.get(k) != getattr(latest, k)
                for k in ("workflow_run_id", "task_id", "workflow_id", "app_id")
            )
        ):
            raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial result unavailable")
        return
    if located is None or located.kind != "trial" or located.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial evidence unavailable")
    if located.order_no != trial.get("order_no"):
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial order evidence mismatch")
    _require_visible_order(context, caller, located.order_no, order_authorization_fn=order_authorization_fn)
    _require_trial_source_route(context, located)
    latest = context.dify_run_store.get(located.run_id)
    if latest is None or not deliverable(latest):
        raise StandardAPIError(410, "TRIAL_RESULT_UNAVAILABLE", "trial result unavailable")
    snapshot = context.dify_run_store.snapshot(latest.run_id)
    if not snapshot or snapshot.get("order_no") != latest.order_no:
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial snapshot unavailable")
    expected = status_answer(snapshot, result.get("language", "zh"))["blocks"][0]["text"]
    if report.get("summary") != expected or any(
        report.get(field) != getattr(latest, field)
        for field in ("workflow_run_id", "task_id", "workflow_id", "app_id")
    ):
        raise StandardAPIError(503, "TRIAL_EVIDENCE_UNAVAILABLE", "trial result evidence mismatch")


def _require_dify_grant_visible(
    context: Any,
    run,
    *,
    order_authorization_fn: Callable[[Any, ScopeContext, str], str],
) -> None:
    """Dify 取数回调的**当前**本人授权判定。"""
    try:
        frozen = scope_from_json(run.scope_json)
    except (ValueError, TypeError) as exc:
        raise StandardAPIError(403, "AGENT_RUN_SCOPE_INVALID", "agent run refused") from exc
    verdict = order_authorization_fn(context, frozen, run.order_no)
    if verdict == UNAVAILABLE:
        raise StandardAPIError(
            503, "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
        )
    if verdict == NOT_OWNED:
        raise StandardAPIError(404, "ORDER_NOT_FOUND", "order not found")


def _require_current_chat_node_order(context, caller, qa, request, *, hooks: ChatDispatchHooks):
    """节点路线的读取守卫：**按存储关联**找回那条 run，再复用它自己的放行判据。"""
    internal = str(qa.get("internal_run_id") or "")
    authenticated_chat_order_reader(
        context, caller, request, authenticate_caller_fn=hooks.authenticate_caller
    )
    order_no = internal.removeprefix(chat_execution.CHAT_RUN_PREFIX)
    if not order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    _require_consumer_self_order(caller, "consumer", order_no)
    if qa["status"] in {"queued", "running", "failed", "cancelled", "expired"}:
        return
    if qa["status"] != "completed":
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    store = context.agent_run_store
    located = store.by_qa(qa["qa_id"]) if store is not None else None
    if located is None:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if located.scope_fingerprint != caller.scope_fingerprint:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    try:
        binding = chat_execution.binding_for_key(context.settings, located.chat_binding)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is None:
        if _route_binding_state(context, located.chat_binding) == "retracted":
            raise StandardAPIError(410, "CHAT_ROUTE_RETRACTED", "this version was retracted")
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")

    _require_visible_order(context, caller, order_no, order_authorization_fn=hooks.order_authorization)
    try:
        released = chat_execution.final_release(
            store=store,
            settings=context.settings,
            caller=caller,
            run_id=located.run_id,
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if not chat_execution.answer_matches_snapshot(
        qa.get("result"),
        released.snapshot or {},
        language=released.language or DEFAULT_LANGUAGE,
    ):
        store.refuse(located.run_id, "qa_answer_mismatch")
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")


def _require_current_chat_dify_order(context, caller, qa, request, *, hooks: ChatDispatchHooks):
    """Dify 路线的读取守卫。判据与节点路线同形，证据来源不同。"""
    internal = str(qa.get("internal_run_id") or "")
    authenticated_chat_order_reader(
        context, caller, request, authenticate_caller_fn=hooks.authenticate_caller
    )
    order_no = internal.removeprefix(chat_execution.DIFY_RUN_PREFIX)
    if not order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    _require_consumer_self_order(caller, "consumer", order_no)
    if qa["status"] in {"queued", "running", "failed", "cancelled", "expired"}:
        return
    if qa["status"] != "completed":
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    runs = context.dify_run_store
    located = runs.by_qa(qa["qa_id"])
    if located is None:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if located.scope_fingerprint != caller.scope_fingerprint or located.order_no != order_no:
        raise StandardAPIError(404, "QA_NOT_FOUND", "assistant question not found")
    try:
        binding = chat_execution.binding_for_key(context.settings, located.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if binding is None or binding.workflow_id != located.workflow_id or (_version_moved(binding, located)):
        if _route_binding_state(context, located.binding_key) == "retracted":
            raise StandardAPIError(410, "CHAT_ROUTE_RETRACTED", "this version was retracted")
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
    _require_visible_order(context, caller, order_no, order_authorization_fn=hooks.order_authorization)
    try:
        binding_after = chat_execution.binding_for_key(context.settings, located.binding_key)
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if (
        binding_after is None
        or binding_after.engine != chat_execution.ENGINE_DIFY
        or binding_after.workflow_id != located.workflow_id
        or _version_moved(binding_after, located)
    ):
        raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
    latest = runs.get(located.run_id)
    if latest is None or latest.state != "succeeded":
        raise StandardAPIError(404, "AGENT_RUN_NO_RESULT", "dify run produced no result")
    if not deliverable(latest):
        raise StandardAPIError(410, "AGENT_RUN_EXPIRED", "dify run has expired")
    snapshot = runs.snapshot(latest.run_id)
    if snapshot is None or snapshot.get("order_no") != order_no:
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable")
    if not chat_execution.answer_matches_snapshot(
        qa.get("result"), snapshot, language=latest.language or DEFAULT_LANGUAGE
    ):
        raise StandardAPIError(503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence differs")


def _is_node_chat_qa(qa) -> bool:
    return str(qa.get("internal_run_id") or "").startswith(chat_execution.CHAT_RUN_PREFIX)


def _is_consumer_run(context: Any, run_id: str) -> bool:
    """这条 run 是 C 端运行还是管家运行。"""
    run = context.agent_run_store.peek(run_id)
    return run is not None and run.operation not in {
        "operator_order_diagnosis",
        "operator_conversation",
        "operator_agent",
    }


def _require_current_chat_order(context, caller, qa, request, *, hooks: ChatDispatchHooks):
    from aiops_diagnostics.operator import operator_cpms

    if str(qa.get("internal_run_id") or "").startswith(operator_cpms.PREFIX):
        if qa["status"] == "completed":
            hooks.require_operator_company_token(request)
            expected = context.operator_cpms.authorize_readback(
                caller, qa["qa_id"], request.headers.get("Authorization", "").removeprefix("Bearer ")
            )
            if expected != qa.get("result"):
                raise operator_cpms.OperatorRefused("OPERATOR_RESULT_MISMATCH")
        return
    from aiops_diagnostics.consumer import consumer_chat_service

    if str(qa.get("internal_run_id") or "").startswith(consumer_chat_service.PREFIX):
        run = context.dify_run_store.by_qa(qa["qa_id"])
        if run and run.scope_fingerprint == caller.scope_fingerprint:
            qa["language"] = run.language
            result = qa.get("result") or {}
            evidence = context.dify_run_store.context(run.run_id) or {}
            if qa["status"] == "completed" and (
                (
                    evidence.get("candidates")
                    and evidence.get("mode") != "documents"
                    and result.get("retrieval_status") == "found"
                    and not evidence.get("live_snapshot")
                )
                or (evidence.get("live_snapshot") and evidence.get("answer_language_version") != 2)
            ):
                qa["language"] = rendering_language(COMPOSED_LANGUAGES, run.language)
                if run.language == "zh-Hant" and run.data_kind == "status" and evidence.get("live_snapshot"):
                    qa["language"] = "zh-Hant"
        if run and (run.order_no or run.data_kind == "agent"):
            authenticated_chat_order_reader(
                context, caller, request, authenticate_caller_fn=hooks.authenticate_caller
            )
        try:
            return consumer_chat_service.require_result(context.runtime, caller, qa)
        except chat_execution.ChatRunUnavailable as exc:
            raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    if _is_node_chat_qa(qa):
        return _require_current_chat_node_order(context, caller, qa, request, hooks=hooks)
    internal = str(qa.get("internal_run_id") or "")
    if internal.startswith(chat_execution.DIFY_RUN_PREFIX):
        return _require_current_chat_dify_order(context, caller, qa, request, hooks=hooks)
    if internal.startswith("consumer-order-chat:"):
        authenticated_chat_order_reader(
            context, caller, request, authenticate_caller_fn=hooks.authenticate_caller
        )
        order_no = internal.removeprefix("consumer-order-chat:")
        _require_consumer_self_order(caller, "consumer", order_no)
        _require_visible_order(context, caller, order_no, order_authorization_fn=hooks.order_authorization)
        if not context.settings.consumer_order_chat_enabled:
            raise StandardAPIError(404, "CHARGING_STATUS_DISABLED", "charging status disabled")
        try:
            path = Path(context.settings.data_home) / "consumer-order-chat" / (qa["qa_id"] + ".json")
            validate_private_file(path)
            evidence = json.loads(path.read_text())
            snapshot = evidence["snapshot"]
            if (
                qa["status"] != "completed"
                or evidence["scope_fingerprint"] != caller.scope_fingerprint
                or evidence["call_id"] != qa["qa_id"]
                or snapshot["order_no"] != order_no
                or snapshot["source"] != "cpms.chargingInfo"
                or evidence["result_sha256"]
                != hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
                or evidence["answer_sha256"]
                != hashlib.sha256(json.dumps(qa["result"], sort_keys=True).encode()).hexdigest()
            ):
                raise ValueError("evidence differs")
        except (OSError, PrivatePathError, ValueError, KeyError, TypeError) as exc:
            raise StandardAPIError(
                503, "CHARGING_STATUS_EVIDENCE_UNAVAILABLE", "evidence unavailable"
            ) from exc


def _consumer_mall_payment(
    settings,
    caller,
    order_no,
    snapshot,
    *,
    scoped_mysql_source_fn: Callable[..., Any] = scoped_mysql_source,
):
    """商城侧订单/税项/支付证据；任何缺口只降级为标注，不打断账务读取本身。"""
    from aiops_diagnostics.consumer.billing import mall_payment_evidence

    try:
        with scoped_mysql_source_fn(
            Settings.from_config(settings.server_config_file), resolve_query_scope(caller)
        ) as source:
            return mall_payment_evidence(source.mall_billing_records(order_no), snapshot)
    except Exception as exc:  # noqa: BLE001 - 证据段是降级契约：任何意外都不能让账务答复 5xx
        _LOGGER.warning("consumer_mall_payment degraded: %s", exc.__class__.__name__)
        return mall_payment_evidence(None, snapshot)


def _authorized_charging_snapshot(
    settings: GatewayServerSettings,
    caller: ScopeContext,
    order_no: str,
    *,
    read_charging_status_fn: Callable[..., dict[str, Any]] = read_charging_status,
) -> dict[str, Any]:
    """读业务：**只**在有授权保证的路径上调用 ``read_charging_status``。"""
    from aiops_diagnostics.consumer.knowledge_access import consumer_endpoint

    try:
        endpoint = consumer_endpoint(settings, caller.effective_tenant_id)
    except ValueError as exc:
        raise StandardAPIError(
            404, "TENANT_ENDPOINT_UNRESOLVED", "business endpoint is not registered"
        ) from exc
    return read_charging_status_fn(
        endpoint,
        order_no,
        tenant_id=caller.effective_tenant_id,
        user_id=caller.subject.c_user_id or "",
    )


def _authorized_consumer_snapshot(
    settings,
    caller,
    order_no,
    data_kind,
    *,
    include_composition=False,
    authorized_charging_snapshot_fn: Callable[..., dict[str, Any]] = _authorized_charging_snapshot,
    consumer_mall_payment_fn: Callable[..., Any] = _consumer_mall_payment,
):
    if data_kind == "status":
        return authorized_charging_snapshot_fn(settings, caller, order_no)
    from aiops_diagnostics.consumer.billing import read_billing_record
    from aiops_diagnostics.consumer.knowledge_access import consumer_endpoint

    if data_kind != "billing":
        raise ValueError("unsupported consumer data kind")
    snapshot = read_billing_record(
        consumer_endpoint(settings, caller.effective_tenant_id),
        order_no,
        tenant_id=caller.effective_tenant_id,
        user_id=caller.subject.c_user_id or "",
        include_composition=include_composition,
    )
    if include_composition:
        snapshot["mall_payment"] = consumer_mall_payment_fn(settings, caller, order_no, snapshot)
    return snapshot


def _charged_snapshot_response(
    settings: GatewayServerSettings,
    context: Any,
    caller: ScopeContext,
    order_no: str,
    *,
    authorized_charging_snapshot_fn: Callable[..., dict[str, Any]] = _authorized_charging_snapshot,
) -> dict[str, Any]:
    del context
    try:
        snapshot = authorized_charging_snapshot_fn(settings, caller, order_no)
    except ChargingStatusError as exc:
        raise StandardAPIError(502, "CHARGING_STATUS_UNAVAILABLE", "charging status unavailable") from exc
    return {
        **snapshot,
        "environment": settings.execution_environment,
        "gateway_version": __version__,
    }


def _consumer_charging_result(
    settings: GatewayServerSettings,
    context: Any,
    caller: ScopeContext,
    issued: AgentRun,
    *,
    order_authorization_fn: Callable[[Any, ScopeContext, str], str],
) -> dict[str, Any]:
    """委派入口的回执：**授权 → 复用共享放行判据 → 回执 + 同一快照**。"""
    try:
        _require_visible_order(
            context, caller, issued.order_no, order_authorization_fn=order_authorization_fn
        )
    except StandardAPIError as exc:
        context.agent_run_store.refuse(
            issued.run_id, "order_not_visible" if exc.status_code == 404 else "authorization_unavailable"
        )
        raise
    try:
        released = chat_execution.final_release(
            store=context.agent_run_store,
            settings=settings,
            caller=caller,
            run_id=issued.run_id,
        )
    except chat_execution.ChatRunUnavailable as exc:
        raise StandardAPIError(exc.http_status, exc.code, str(exc), retryable=exc.retryable) from exc
    return {**released.receipt(), "charging_status": released.snapshot}


def _claim_payload(run: AgentRun) -> dict[str, Any]:
    """回给执行节点的**唯一合法载荷**：7 字段快照 + 服务端观察到的执行位置。"""
    snapshot = run.snapshot or {}
    return {
        **snapshot,
        "environment": run.environment,
        "gateway_version": run.gateway_version,
        "actual_node_id": run.actual_node_id,
        "run_state": run.state,
    }
