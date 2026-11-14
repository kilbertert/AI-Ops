"""客服 Agent 的受控工具与会话上下文；自然语言决策由 Dify 执行。"""

from __future__ import annotations

import json
from datetime import date, datetime

from aiops_diagnostics.consumer import consumer_knowledge
from aiops_diagnostics.consumer.consumer_binding import deliverable_binding, identity, require_pinned
from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available
from aiops_diagnostics.dify.dify_run_store import DifyRunError, still_valid
from aiops_diagnostics.execution.agent_runs import scope_from_json
from aiops_diagnostics.execution.chat_data_grant import DataGrantStore, usable
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable
from aiops_diagnostics.execution.run_deliverability import deliverable

RESOURCES = {"knowledge", "orders", "status", "billing", "support"}


def order_ids(context):
    conversation = (context or {}).get("conversation", {})
    return (
        ({conversation["selected_order"]} if conversation.get("selected_order") else set())
        | {order for turn in conversation.get("history", []) for order in turn.get("order_ids", [])}
        | {
            order
            for call in (context or {}).get("calls", [])
            for order in (call.get("result") or {}).get("order_ids", [])
        }
    )


def used_knowledge(context):
    return any(
        c["tool"] == "knowledge" and (c.get("result") or {}).get("status") != "refused"
        for c in (context or {}).get("calls", [])
    ) or any(t.get("used_knowledge") for t in (context or {}).get("conversation", {}).get("history", []))


def require_evidence(runtime, caller, binding, context):
    """历史和交付都重新核权，读取历史不重新冒充实时取数。"""
    if not runtime.gateway_settings.consumer_chatflow_enabled:
        raise ChatRunUnavailable("CHATFLOW_DISABLED", "客服未启用", http_status=404)
    for order in order_ids(context):
        runtime._require_chat_order_visible(caller, order)
    if used_knowledge(context):
        require_available(runtime, caller, binding)


def conversation_context(runtime, caller, conversation, before, selected_order=""):
    history = []
    for turn in runtime.conversation_store.completed_context(
        conversation["conversation_id"], caller.scope_fingerprint, before=before
    ):
        qa = runtime.store.get_assistant_question(turn.get("status_qa_id"), caller.scope_fingerprint)
        run = runtime.dify_run_store.by_qa(qa["qa_id"]) if qa else None
        if run is None or not deliverable(run) or qa["status"] != "completed":
            continue
        binding = deliverable_binding(runtime, caller, run)
        require_pinned(runtime, caller, binding)
        evidence = runtime.dify_run_store.context(run.run_id) or {}
        require_evidence(runtime, caller, binding, evidence)
        if run.order_no:
            runtime._require_chat_order_visible(caller, run.order_no)
        if runtime.dify_run_store.snapshot(run.run_id) != qa.get("result"):
            raise ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "历史答案证据不一致")
        answer = "\n".join(b["text"] for b in qa["result"].get("blocks", []) if b.get("kind") == "text")
        history.append(
            {
                "question": qa["question"][:2000],
                "answer": answer[:4000],
                "order_ids": sorted(order_ids(evidence) | ({run.order_no} if run.order_no else set())),
                "answered_at": str(turn["created_at"]),
                "used_knowledge": used_knowledge(evidence),
            }
        )
    while history and len(json.dumps(history, ensure_ascii=False)) > 32000:
        history.pop(0)
    return {"history": history, "selected_order": selected_order, "historical_results_are_not_current": True}


def initialize(runtime, run_id, context):
    if not runtime.dify_run_store.store_context(
        run_id, {"mode": "agent", "conversation": context, "calls": []}
    ):
        raise ChatRunUnavailable("CHATFLOW_NOT_ACTIVE", "客服执行已结束")


def callback(runtime, token, body, *, read_snapshot, list_orders):
    grants = DataGrantStore(runtime.store.path)
    grant = usable(grants.by_token(token))
    runs = runtime.dify_run_store
    run = runs.by_qa(grant.qa_id)
    if (
        run is None
        or run.data_kind != "agent"
        or run.kind not in {"service", "service_trial"}
        or (
            run.grant_id,
            run.scope_fingerprint,
            run.order_no,
            run.tenant_id,
            run.environment,
            run.workflow_id,
        )
        != (
            grant.grant_id,
            grant.scope_fingerprint,
            grant.order_no,
            grant.tenant_id,
            grant.environment,
            grant.workflow_id,
        )
    ):
        raise ChatRunUnavailable("GRANT_SCOPE_MISMATCH", "客服授权不匹配", http_status=403)
    caller = scope_from_json(run.scope_json)
    if run.kind == "service" and (
        caller.delegated or caller.data_scope.type != "self" or not caller.subject.c_user_id
    ):
        raise ChatRunUnavailable("CONSUMER_SCOPE_REQUIRED", "仅允许本人查询", http_status=403)
    trial = None
    if run.kind == "service_trial":
        entry = runtime._trial_contexts.get(run.qa_id)
        trial = entry[1] if entry else None
        binding = trial.binding if trial else None
    else:
        binding = deliverable_binding(runtime, caller, run)
    if binding is None or binding.callback_version != 6:
        raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "客服发布版本已失效", http_status=403)

    def valid():
        from aiops_diagnostics.gateway.environment_registry import RegistryError, require_registered

        try:
            require_registered(runtime, caller.effective_tenant_id)
        except RegistryError as exc:
            raise ChatRunUnavailable(
                "TENANT_ENVIRONMENT_NOT_ENABLED", "当前租户接入已停用或变化", http_status=403
            ) from exc
        usable(grants.by_id(grant.grant_id))
        latest = runs.get(run.run_id)
        qa = (runtime.store.get_standard_diagnosis if trial else runtime.store.get_assistant_question)(
            run.qa_id, caller.scope_fingerprint
        )
        if (
            latest is None
            or latest.state != "running"
            or not still_valid(latest)
            or not qa
            or qa["status"] != "running"
        ):
            raise ChatRunUnavailable("CHATFLOW_NOT_ACTIVE", "客服执行已结束", http_status=403)
        if trial:
            trial.require_current(runtime.gateway_settings)
        else:
            require_pinned(runtime, caller, binding)
        if identity(binding) != run.binding_digest:
            raise ChatRunUnavailable("CHATFLOW_BINDING_CHANGED", "客服授权已改变", http_status=403)

    valid()
    if not isinstance(body, dict) or set(body) - {"resource", "query", "order_no", "knowledge_base_ids"}:
        raise ValueError("invalid consumer tool arguments")
    resource = body.get("resource")
    query, order = body.get("query", ""), body.get("order_no", "")
    if (
        not isinstance(resource, str)
        or resource not in RESOURCES
        or not isinstance(query, str)
        or len(query) > 2000
        or not isinstance(order, str)
        or len(order) > 128
    ):
        raise ValueError("invalid consumer tool arguments")
    try:
        index = runs.reserve_consumer_tool(run.run_id, resource, {"query": query, "order_no": order})
    except DifyRunError as exc:
        if exc.code == "CONSUMER_TOOL_BUDGET":
            return {"status": "refused", "code": exc.code, "message": str(exc)}
        raise ChatRunUnavailable(exc.code, str(exc), http_status=403) from exc
    from aiops_diagnostics.data.charging_status import ChargingStatusError

    try:
        result = _query(
            runtime,
            caller,
            binding,
            run,
            resource,
            query,
            order,
            body.get("knowledge_base_ids"),
            read_snapshot,
            list_orders,
        )
    except ChargingStatusError:
        result = {
            "status": "unavailable",
            "code": "CONSUMER_DATA_UNAVAILABLE",
            "message": "暂时无法读取业务记录，不能推断订单状态或支付结果",
        }
    except ChatRunUnavailable as exc:
        if exc.code not in {
            "ORDER_NOT_VISIBLE",
            "CHATFLOW_LIVE_DISABLED",
            "CONSUMER_ORDER_REQUIRED",
            "KNOWLEDGE_SELECTION_UNAVAILABLE",
        }:
            raise
        result = {"status": "refused", "code": exc.code, "message": str(exc)}
    valid()
    for order in result.get("order_ids", []):
        runtime._require_chat_order_visible(caller, order)
    if resource == "knowledge" and result.get("status") != "refused":
        require_available(runtime, caller, binding)
    if not runs.finish_consumer_tool(run.run_id, index, result):
        raise ChatRunUnavailable("CHATFLOW_NOT_ACTIVE", "客服执行已结束", http_status=403)
    return result


def _query(runtime, caller, binding, run, resource, query, order, libraries, read_snapshot, list_orders):
    if resource == "knowledge":
        if libraries != list(binding.knowledge_base_ids):
            raise ChatRunUnavailable("KNOWLEDGE_SELECTION_UNAVAILABLE", "知识库选择与发布版不一致")
        require_available(runtime, caller, binding)
        if not query.strip():
            return {"status": "refused", "message": "请提供需要检索的具体问题"}
        return consumer_knowledge.select_context(runtime, caller, binding, query, run.language, run.qa_id)
    if resource == "support":
        content = consumer_knowledge.binding_content(binding, caller)
        return {
            "status": "found",
            "support": content.get("support", {}),
            "limitation": "仅提供联系指引，不代表已转接人工",
        }
    if run is not None and run.kind == "service_trial":
        if resource == "orders":
            return {"status": "refused", "message": "运营预览请在页面选择订单，不列出消费者订单"}
        if not run.order_no or order != run.order_no:
            raise ChatRunUnavailable("CONSUMER_ORDER_REQUIRED", "只能查询预览中明确选定的订单")
    if not (
        binding.live_order_status
        if resource == "status"
        else binding.live_billing_record
        if resource == "billing"
        else binding.live_order_status or binding.live_billing_record
    ):
        raise ChatRunUnavailable("CHATFLOW_LIVE_DISABLED", "当前客服未启用这项查询")
    if resource == "orders":
        rows = list_orders(caller)
        orders = []
        for row in rows[:5]:
            number = str(row["order_no"])
            runtime._require_chat_order_visible(caller, number)
            orders.append(
                {
                    k: row[k].isoformat() if isinstance(row[k], (date, datetime)) else row[k]
                    for k in ("order_no", "created_time", "start_time", "end_time", "station_name")
                    if k in row
                }
            )
        return {
            "status": "found" if orders else "not_found",
            "orders": orders,
            "latest_order_no": str(orders[0]["order_no"]) if orders else None,
            "order_ids": [str(r["order_no"]) for r in orders],
            "source": "cpms.own_orders",
            "ordering": "newest_first",
        }
    if not order:
        raise ChatRunUnavailable("CONSUMER_ORDER_REQUIRED", "请先确认订单或查询本人最近订单")
    from aiops_diagnostics.identity.order_identity import is_order_identifier

    if not is_order_identifier(order):
        raise ChatRunUnavailable("CONSUMER_ORDER_REQUIRED", "订单号格式不正确")
    runtime._require_chat_order_visible(caller, order)
    snapshot = read_snapshot(caller, order, resource)
    if snapshot.get("order_no") != order:
        raise ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "订单证据不匹配")
    return {
        "status": "found",
        "snapshot": snapshot,
        "order_ids": [order],
        "limitation": "账务记录不等于支付渠道扣款或到账回执；没有币种时不得猜测币种。"
        if resource == "billing"
        else "仅代表本次读取时的业务状态，不代表故障诊断。",
    }


def final_answer(raw, context, *, language="zh", question=""):
    """正文由 Agent 表达，来源与媒体由本轮工具记录附加。"""
    from aiops_diagnostics.consumer.consumer_documents import render_sources_answer

    calls = (context or {}).get("calls", [])
    if any(c.get("result") is None for c in calls):
        raise ChatRunUnavailable("CHATFLOW_EVIDENCE_UNAVAILABLE", "工具记录尚未完成")
    if consumer_knowledge.SAFETY.search(question):
        return consumer_knowledge.fallback("safety", language)
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 8000:
        raise ChatRunUnavailable("CHATFLOW_ANSWER_INVALID", "客服未返回有效正文")
    sources = []
    support = []
    support_urls = []
    seen = set()
    for call in calls:
        result = call.get("result") or {}
        if result.get("status") != "found":
            continue
        for source in result.get("sources", []):
            identity = source["retrieved_reference_id"]
            if identity not in seen:
                seen.add(identity)
                sources.append(source)
        if call["tool"] == "support":
            line = consumer_knowledge._support_line(language, result)
            if line:
                support.append(line)
            for route in result.get("support", {}).values():
                support_urls.append(route["url"])
    answer = render_sources_answer(raw, sources, preserve=order_ids(context), support_urls=support_urls)
    answer["retrieval_status"] = (
        "found"
        if sources
        else "unavailable"
        if any(
            call["tool"] == "knowledge"
            and (call.get("result") or {}).get("status") in {"unavailable", "limited", "refused"}
            for call in calls
        )
        else "not_found"
    )
    if support:
        answer["blocks"].append({"kind": "text", "text": "\n".join(support)})
    return answer
