"""管家 CPMS 只读代理转发、站点方案计算与充电时序遥测取数。

从 `operator_cpms.py` 拆出，独立承载工具级 CPMS/tsdata 请求代理、缓存回放与证据组装。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.operator import (
    communication,
    cpms_sites,
    operator_agent,
    operator_tools,
    order_pricing,
    order_settlement,
)
from aiops_diagnostics.operator.cpms_evidence import (
    _TIMELINE_DEVICE,
    SITE_TOOL,
    TIMELINE_FIELDS,
    TIMELINE_LIMIT,
    TIMELINE_TOOL,
    TOOL_NAMES,
    TsdataInvalidColumn,
    _declared,
    _proxy_tool,
    _timeline_time,
    minimized_response,
    project_evidence,
    run_values,
    tool_inputs,
    tool_request,
    validate_body,
)
from aiops_diagnostics.operator.cpms_sites import (
    SiteKeywords,
    query_sites,
    read_site_tool,
    site_scope,
)
from aiops_diagnostics.operator.operator_conversation import OperatorRefused


def station_plan(
    service: Any,
    task: Any,
    run: Any,
    *,
    cpms_get_fn: Callable[..., Any],
) -> dict[str, Any]:
    from aiops_diagnostics.operator import station_pricing, station_tariff

    arguments = task.arguments.get("cpms_station_plan", {})
    missing = [
        label
        for key, label in (
            ("name", "站点名称"),
            ("date", "日期YYYY-MM-DD"),
            ("start", "最早开始HH:mm"),
            ("end", "最晚结束HH:mm"),
            ("energy", "所需电量kWh"),
            ("power", "可持续功率kW"),
            ("timezone", "计费时区如Asia/Shanghai"),
            ("basis", "确认普通按量、不用优惠、不预约、不买保险、充完离场"),
        )
        if not arguments.get(key)
    ]
    data: dict[str, Any] = {
        **{
            k: arguments.get(k, "")
            for k in ("name", "date", "start", "end", "energy", "power", "basis", "timezone")
        },
        "site_id": "",
        "text": "",
    }
    if missing:
        data["text"] = "请补充：" + "、".join(missing) + "。这些条件决定连续充电费用，不能用额定功率代替。"
        return data

    try:
        station_pricing.validate_conditions(arguments)
    except station_pricing.PlanUnavailable as exc:
        data["text"] = str(exc)
        return data

    def current() -> str:
        return service.authorize_execution(run.run_id, tool="cpms_station_plan")[0].caller.scope_fingerprint

    sites = query_sites(
        site_scope(task.caller),
        SiteKeywords(arguments["name"]),
        cpms_sites.cpms_site_reader(task.base_url, task.bearer, getter=cpms_get_fn),
        current,
    )
    if len(sites["sites"]) != 1 or sites["limited"]:
        data["text"] = "没有唯一匹配的可见站点，请补充完整站点名称。"
        return data
    data["site_id"] = sites["sites"][0]["id"]
    try:
        rules = station_tariff.read_rules(
            task.caller,
            data["site_id"],
            arguments["date"],
            lambda path: cpms_get_fn(task.base_url + path, task.bearer, task.caller.effective_tenant_id),
            current,
        )
    except OperatorRefused as exc:
        gaps = {
            "OPERATOR_PLAN_PENDING_RULE_CHANGE": "该站点已有待生效的费率变更，需核对生效规则后比较。",
            "OPERATOR_PLAN_SEASON_UNSUPPORTED": "季节时段配置不完整或跨年，需核对适用规则。",
            "OPERATOR_PLAN_SITE_UNSUPPORTED": "当前仅支持四轮充电站的方案比较。",
        }
        if str(exc) not in gaps:
            raise
        data["text"] = gaps[str(exc)]
        return data
    data["rules"] = rules
    try:
        data["text"] = station_pricing.plan_text(rules, arguments)
    except station_pricing.PlanUnavailable as exc:
        data["text"] = str(exc)
    return data


def charging_timeline(
    service: Any,
    task: Any,
    run: Any,
    *,
    tsdata_query_fn: Callable[..., Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """SOP §2.2 时序过程遥测：设备与窗口只取自已交付的订单详情证据。"""
    base = service.context.settings.operator_tsdata_url.rstrip("/")
    if not base:
        raise OperatorRefused("TSDATA_UNAVAILABLE")
    detail = task.evidence.get(TOOL_NAMES[0], {}).get("data") or {}
    device = detail.get("childDeviceCode") or detail.get("deviceCode") or ""
    if not device:
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    if not _TIMELINE_DEVICE.fullmatch(str(device)):
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    start = _timeline_time(detail.get("createdTime"))
    if start is None:
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    raw_end = detail.get("stopTime")
    end = _timeline_time(raw_end)
    if raw_end and end is None:
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    if end is None:
        end = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    query = {
        "table": "charging-gun_property",
        "deviceId": device,
        "start": start,
        "end": end,
        "fields": list(TIMELINE_FIELDS),
        "orderBy": "_ts",
        "order": "ASC",
        "page": 1,
        "pageSize": TIMELINE_LIMIT,
    }
    try:
        data = tsdata_query_fn(base + "/tdadmin/data/query", query)
    except TsdataInvalidColumn:
        # 不同客户环境的 charging-gun_property 列集合有差异（如缺 errorCode/errorReason/meterNow）；
        # 仅在门面明确报缺列时单次回退全列查询，仍由下方 shape_keys 白名单严格投影。
        fallback = {key: value for key, value in query.items() if key != "fields"}
        data = tsdata_query_fn(base + "/tdadmin/data/query", fallback)
    records = data.get("records") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or type(data.get("count")) is not int
        or not isinstance(records, list)
        or len(records) > TIMELINE_LIMIT
        or data["count"] < len(records)
    ):
        raise OperatorRefused("TSDATA_UNAVAILABLE")
    shape_keys = _declared(TIMELINE_TOOL).evidence_shape["records"][0]
    rows = []
    for row in records:
        if not isinstance(row, dict):
            raise OperatorRefused("TSDATA_UNAVAILABLE")
        picked = {}
        for key in shape_keys:
            source_key = "_ts" if key == "ts" else key
            if source_key not in row:
                continue
            value = row[source_key]
            if isinstance(value, (dict, list)) or (isinstance(value, str) and len(value) > 1000):
                raise OperatorRefused("TSDATA_UNAVAILABLE")
            picked[key] = value
        rows.append(picked)
    total = data["count"]
    value = {
        "records": rows,
        "total": total,
        "sample_limit": TIMELINE_LIMIT,
        "truncated": total > len(rows),
        "ordering_guaranteed": True,
        "complete_timeline": total <= len(rows),
        "samples_returned": len(rows),
        "absence": None if rows else "no_samples",
        "device": device,
        "window_start": start,
        "window_end": end,
    }
    response = {
        "code": 0,
        "data": {
            "orderNo": run.order_no,
            "siteId": task.order_site_id,
            "tenantId": task.caller.effective_tenant_id,
            "value": value,
        },
    }
    return value, response


def proxy(
    service: Any,
    run_id: str,
    grant: str,
    path: str,
    query: str,
    method: str = "GET",
    *,
    cpms_get_fn: Callable[..., Any],
    tsdata_query_fn: Callable[..., Any],
) -> dict[str, Any]:
    task = service.tasks.get(run_id)
    started = time.monotonic()
    try:
        result = proxy_inner(
            service,
            run_id,
            grant,
            path,
            query,
            method,
            cpms_get_fn=cpms_get_fn,
            tsdata_query_fn=tsdata_query_fn,
        )
        run = service.context.agent_run_store.peek(run_id)
        tool = _proxy_tool(task, run, method, path, query)
        service._trace(
            run_id, "tool", "succeeded", tool=tool, duration_ms=(time.monotonic() - started) * 1000
        )
        service._summarize_tool(run_id, task, tool)
        return result
    except OperatorRefused as exc:
        if task is not None:
            run = service.context.agent_run_store.get(run_id)
            tool = _proxy_tool(task, run, method, path, query) if run else None
            service._note_refusal(task, tool, exc)
            if tool and secrets.compare_digest(task.grant, grant):
                service._trace(
                    run_id,
                    "tool",
                    "refused",
                    tool=tool,
                    duration_ms=(time.monotonic() - started) * 1000,
                    detail=str(exc),
                )
                service._summarize_tool(run_id, task, tool, refusal=str(exc))
        raise


def proxy_inner(
    service: Any,
    run_id: str,
    grant: str,
    path: str,
    query: str,
    method: str,
    *,
    cpms_get_fn: Callable[..., Any],
    tsdata_query_fn: Callable[..., Any],
) -> dict[str, Any]:
    task = service.tasks.get(run_id)
    if task is None or not secrets.compare_digest(task.grant, grant):
        raise OperatorRefused("OPERATOR_GRANT_REJECTED")
    run = service.context.agent_run_store.get(run_id)
    if run is None:
        raise OperatorRefused("OPERATOR_RUN_INACTIVE")
    tool = _proxy_tool(task, run, method, path, query)
    if tool is None:
        raise OperatorRefused("OPERATOR_PATH_REJECTED")
    with task.lock:
        service.authorize_execution(run_id, tool=tool)
        service._trace(run_id, "tool", tool=tool)
        if tool in task.evidence:
            if tool == communication.NAME:
                communication.require_current(service, task, run)
            refs = task.evidence[tool].get("offline_references")
            if refs is not None:
                from aiops_diagnostics.operator import offline_tool

                try:
                    offline_tool.require_current(service, task, refs)
                except SourceError as exc:
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED") from exc
            return task.evidence[tool]["response"]
        if (
            tool
            in (
                TOOL_NAMES[1],
                "cpms_order_tariff",
                "cpms_order_billing",
                TIMELINE_TOOL,
                communication.NAME,
                order_settlement.SETTLEMENT_TOOL,
            )
            and TOOL_NAMES[0] not in task.delivered
        ):
            raise OperatorRefused("OPERATOR_DETAIL_REQUIRED")
        if tool == "cpms_order_billing" and "cpms_order_tariff" not in task.delivered:
            raise OperatorRefused("OPERATOR_TARIFF_REQUIRED")

        if tool in {
            "cpms_order_amount_check",
            "cpms_order_process_stats",
            order_settlement.SETTLEMENT_CHECK_TOOL,
        }:
            return _proxy_derived_analysis(service, task, run, tool)
        from aiops_diagnostics.operator import offline_tool

        if tool == offline_tool.NAME:
            return _proxy_offline_tool(service, task, run, tool, offline_tool)
        if tool == "cpms_station_plan":
            data = service._station_plan(task, run)
            service.authorize_execution(run_id, tool=tool)
            data = order_pricing.project(data, _declared(tool).evidence_shape)
            task.evidence[tool] = {
                "tool": tool,
                "inputs": tool_inputs(tool, task.arguments.get(tool, {})),
                "source": _declared(tool).source,
                "observed_at": format_stored_time(now_utc()),
                "data": data,
                "response": {
                    "code": 0,
                    "data": {"tenantId": task.caller.effective_tenant_id, "value": data},
                },
            }
            return task.evidence[tool]["response"]
        if tool in ("cpms_order_tariff", "cpms_order_billing", order_settlement.SETTLEMENT_TOOL):
            detail = cpms_get_fn(
                task.base_url + tool_request(TOOL_NAMES[0], {"order_no": run.order_no})["path"],
                task.bearer,
                task.caller.effective_tenant_id,
            )
            validate_body(detail, task.caller, run.order_no, TOOL_NAMES[0])
            if detail["data"]["siteId"] != task.order_site_id:
                raise OperatorRefused("CPMS_SCOPE_MISMATCH")
        if tool == order_settlement.SETTLEMENT_TOOL:
            data = order_settlement.query_settlement(service, task, run)
            task.evidence[tool] = {
                "tool": tool,
                "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                "source": _declared(tool).source,
                "observed_at": format_stored_time(now_utc()),
                "data": data,
                "response": {
                    "code": 0,
                    "data": {
                        "orderNo": run.order_no,
                        "siteId": task.order_site_id,
                        "tenantId": task.caller.effective_tenant_id,
                        "value": data,
                    },
                },
            }
            return task.evidence[tool]["response"]
        if tool == SITE_TOOL:

            def reauthorize() -> str:
                service.authorize_execution(run_id, tool=tool)
                return task.caller.scope_fingerprint

            data, response = read_site_tool(
                site_scope(task.caller),
                tool,
                task.arguments.get(tool) or {},
                cpms_sites.cpms_site_reader(task.base_url, task.bearer, getter=cpms_get_fn),
                reauthorize,
            )
            service.authorize_execution(run_id, tool=tool)
            task.evidence[tool] = {
                "tool": tool,
                "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                "source": operator_tools.tool(tool).source,
                "observed_at": format_stored_time(now_utc()),
                "data": data,
                "response": response,
            }
            return task.evidence[tool]["response"]
        if tool == communication.NAME:
            data = communication.query(service, task, run, tsdata_query_fn)
            task.evidence[tool] = {
                "tool": tool,
                "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                "source": _declared(tool).source,
                "observed_at": format_stored_time(now_utc()),
                "data": data,
                "response": {
                    "code": 0,
                    "data": {
                        "orderNo": run.order_no,
                        "siteId": task.order_site_id,
                        "tenantId": task.caller.effective_tenant_id,
                        "value": data,
                    },
                },
            }
            return task.evidence[tool]["response"]
        if tool == TIMELINE_TOOL:
            data, response = service._charging_timeline(task, run)
            service.authorize_execution(run_id, tool=tool)
            task.evidence[tool] = {
                "tool": tool,
                "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                "source": _declared(tool).source,
                "observed_at": format_stored_time(now_utc()),
                "data": data,
                "response": response,
            }
            return task.evidence[tool]["response"]
        body = cpms_get_fn(
            task.base_url + path + ("?" + query if query else ""),
            task.bearer,
            task.caller.effective_tenant_id,
        )
        if tool in ("cpms_order_tariff", "cpms_order_billing"):
            from aiops_diagnostics.operator.order_billing import read_billing

            data = (
                order_pricing.read_snapshot(body, task.caller, run.order_no)
                if tool == "cpms_order_tariff"
                else read_billing(body, task.caller, run.order_no, task.order_site_id)
            )
            service.authorize_execution(run_id, tool=tool)
            task.evidence[tool] = {
                "tool": tool,
                "inputs": {"order_no": run.order_no},
                "source": operator_tools.tool(tool).source,
                "observed_at": format_stored_time(now_utc()),
                "data": data,
                "response": {
                    "code": 0,
                    "data": {
                        "orderNo": run.order_no,
                        "tenantId": task.caller.effective_tenant_id,
                        "siteId": task.order_site_id,
                        "value": data,
                    },
                },
            }
            return task.evidence[tool]["response"]
        validate_body(body, task.caller, run.order_no, tool)
        rows = [body["data"]["chOrderInfoModel"]] if tool == TOOL_NAMES[0] else body["data"]["records"]
        if any(row["siteId"] != task.order_site_id for row in rows):
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
        service.authorize_execution(run_id, tool=tool)
        task.evidence[tool] = project_evidence(body, run.order_no, tool)
        task.evidence[tool]["response"] = minimized_response(body, tool)
        return task.evidence[tool]["response"]


def _proxy_derived_analysis(service: Any, task: Any, run: Any, tool: str) -> dict[str, Any]:
    if tool == "cpms_order_amount_check":
        required = {"cpms_order_detail", "cpms_order_tariff", "cpms_order_billing"}
        section = "amount"
    elif tool == order_settlement.SETTLEMENT_CHECK_TOOL:
        required = {"cpms_order_detail", order_settlement.SETTLEMENT_TOOL}
        section = "settlement"
    else:
        required = {"cpms_order_detail"}
        section = "process"
    if not required <= task.delivered or (
        tool == "cpms_order_process_stats"
        and not ({"cpms_charging_samples", "cpms_charging_timeline"} & task.delivered)
    ):
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    service._authorized_order(task, run.order_no, expected_site=task.order_site_id)
    data = operator_agent.analysis(task.evidence, section)
    data = order_pricing.project(data, _declared(tool).evidence_shape)
    task.evidence[tool] = {
        "tool": tool,
        "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments[tool])),
        "source": _declared(tool).source,
        "observed_at": format_stored_time(now_utc()),
        "data": data,
        "response": {
            "code": 0,
            "data": {
                "orderNo": run.order_no,
                "siteId": task.order_site_id,
                "tenantId": task.caller.effective_tenant_id,
                "value": data,
            },
        },
    }
    return task.evidence[tool]["response"]


def _proxy_offline_tool(service: Any, task: Any, run: Any, tool: str, offline_tool: Any) -> dict[str, Any]:
    try:
        data, refs = offline_tool.query(service, task, run)
    except (SourceError, ValueError) as exc:
        raise OperatorRefused("OPERATOR_OFFLINE_UNAVAILABLE") from exc
    data = order_pricing.project(data, _declared(tool).evidence_shape)
    task.evidence[tool] = {
        "tool": tool,
        "inputs": tool_inputs(tool, task.arguments.get(tool, {})),
        "source": _declared(tool).source,
        "observed_at": format_stored_time(now_utc()),
        "data": data,
        "offline_references": refs,
        "response": {
            "code": 0,
            "data": {"tenantId": task.caller.effective_tenant_id, "value": data},
        },
    }
    return task.evidence[tool]["response"]


def invoke(service: Any, grant: str, tool: str) -> dict[str, Any]:
    if tool not in TOOL_NAMES:
        raise OperatorRefused("OPERATOR_TOOL_DISABLED")
    with service.lock:
        run_id = next(
            (key for key, task in service.tasks.items() if secrets.compare_digest(task.grant, grant)), None
        )
    if run_id is None:
        raise OperatorRefused("OPERATOR_GRANT_REJECTED")
    run = service.context.agent_run_store.get(run_id)
    if run is None:
        raise OperatorRefused("OPERATOR_RUN_INACTIVE")
    request = tool_request(tool, run_values(tool, run.order_no))
    return service.proxy(run_id, grant, request["path"], request["query"], request["method"])
