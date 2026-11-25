"""运维分析异步作业执行器（失败率卡与离线/故障码/支付卡）。

从 `gateway_runtime.GatewayRuntime` 拆出，使网关运行时只保留线程池装配、作业登记与
委托入口，领域聚合、范围交集与叙述轮组装收敛在 `operator/` 目录内。
"""

from __future__ import annotations

import contextlib
import time
from datetime import UTC
from types import SimpleNamespace
from typing import Any

from aiops_diagnostics.common.redaction import ERROR_TEXT_LIMIT, redact_text
from aiops_diagnostics.common.scalar_rules import now_utc, parse_stored_time
from aiops_diagnostics.data.sources import scoped_mysql_source
from aiops_diagnostics.gateway import metrics_store as metrics_dims
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.operator.ops_analysis import (
    OPERATIONS_CARD_OFFLINE,
    OPERATIONS_CARD_PAYMENT,
    REASON_NO_FAULTS,
    REASON_NO_INCIDENTS,
    REASON_NO_ORDERS,
    REASON_SCOPE_EMPTY,
    AnalysisAggregate,
    FaultCodeAggregate,
    OfflineAggregate,
    PaymentAggregate,
    build_failure_rate_contract,
    build_fault_code_contract,
    build_offline_contract,
    build_payment_contract,
)


def intersect_sites(
    requested: tuple[str, ...] | None,
    allowed: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """计算作业实际读取的站点集合：请求站点 ∩ 权限可见站点。

    遵循下游 ``sources._bounded_sites`` 的三值约定：
    - ``None`` 表示该层不收窄站点（仍受租户谓词约束）；
    - ``()`` 表示交集为空，不得查库。
    """
    if requested is None:
        return None if allowed is None else tuple(allowed)
    if allowed is None:
        return tuple(requested)
    return tuple(sorted(set(requested) & set(allowed)))


def db_window_bound(value: str) -> str:
    """将时间窗边界统一折算为 UTC SQL 字符串，消除不同时区偏移差异。"""
    return parse_stored_time(value).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _internal_error_message(error: Exception, order_no: str | None) -> str:
    message = redact_text(str(error), preserve=(order_no or "",))
    return message[:ERROR_TEXT_LIMIT] if message else error.__class__.__name__


def end_analysis_job(
    runtime: Any,
    analysis_id: str,
    tenant_id: str,
    *,
    result: dict[str, Any] | None,
    started_ms: float,
    error_code: str | None = None,
    error_message: str = "",
) -> None:
    """收敛运维分析作业终态，确保收尾写异常不会让作业滞留在 ``running``。"""
    status = "failed" if error_code else "completed"
    try:
        stored = runtime.store.update_standard_diagnosis(
            analysis_id,
            status=status,
            result=result,
            error_code=error_code,
            error_message=error_message or None,
        )
    except Exception:  # noqa: BLE001 - 收尾写不能逃逸出 worker
        stored = False
    if not stored:
        with contextlib.suppress(Exception):
            runtime.store.update_standard_diagnosis(
                analysis_id,
                status="failed",
                error_code=error_code or "ANALYSIS_FAILED",
                error_message=error_message or "analysis job could not record its outcome",
            )
        return
    runtime._record_metric(
        tenant_id=tenant_id,
        route_type=metrics_dims.ROUTE_OPERATIONS_ANALYSIS,
        outcome="completed" if status == "completed" else "failed",
        error_code=error_code,
        duration_ms=int((time.monotonic() - started_ms) * 1000),
    )


def empty_card_result(
    card: str,
    language: str,
    site_ids: tuple[str, ...] | None,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """权限交集为空时返回诚实的空卡结果（``SCOPE_EMPTY``）。"""
    kwargs = dict(
        language=language,
        site_ids=site_ids,
        window_start=parse_stored_time(window_start),
        window_end=parse_stored_time(window_end),
        reason_code=REASON_SCOPE_EMPTY,
    )
    if card == OPERATIONS_CARD_OFFLINE:
        return build_offline_contract(OfflineAggregate(0, 0, 0, 0), None, **kwargs)
    if card == OPERATIONS_CARD_PAYMENT:
        return build_payment_contract(PaymentAggregate(0, 0, 0, 0, 0), None, **kwargs)
    return build_fault_code_contract(FaultCodeAggregate(0, 0, ()), None, **kwargs)


def narrative_for(
    runtime: Any,
    card: str,
    aggregate: Any,
    window: dict[str, Any],
    *,
    runtime_mod: Any,
) -> Any:
    """对已定型的聚合数字调用叙述轮生成说明与建议（不生成数字）。"""
    from aiops_diagnostics.operator.ops_analysis_narrative import run_narrative_turn

    if card == OPERATIONS_CARD_OFFLINE:
        facts = build_offline_contract(aggregate, None, **window)
    elif card == OPERATIONS_CARD_PAYMENT:
        facts = build_payment_contract(aggregate, None, **window)
    else:
        facts = build_fault_code_contract(aggregate, None, **window)
    settings = runtime_mod.Settings.from_config(runtime.gateway_settings.server_config_file)
    selected = runtime.diagnostic_settings.agent.select_provider(None)
    return run_narrative_turn(
        facts,
        settings.agent,
        provider=selected,
        key_slot=selected.resolved_key_slot(),
        language=str(window["language"]),
    )


def _invoke_narrative(
    runtime: Any,
    card: str,
    aggregate: Any,
    window: dict[str, Any],
    *,
    runtime_mod: Any,
) -> Any:
    hook = getattr(runtime, "_narrative_for", None)
    if callable(hook):
        return hook(card, aggregate, window)
    return narrative_for(runtime, card, aggregate, window, runtime_mod=runtime_mod)


def compute_card(
    runtime: Any,
    card: str,
    sources: Any,
    effective_sites: tuple[str, ...] | None,
    window_start: str,
    window_end: str,
    language: str,
    site_ids: tuple[str, ...] | None,
    *,
    runtime_mod: Any,
) -> dict[str, Any]:
    """端到端计算单张运维分析卡：聚合 → 可选叙述轮 → 组装响应契约。"""
    db_start, db_end = db_window_bound(window_start), db_window_bound(window_end)
    offline_details_available = False
    if card == OPERATIONS_CARD_OFFLINE and runtime.diagnostic_settings.mysql.timezone:
        from aiops_diagnostics.operator import device_offline

        try:
            device_offline.window(
                window_start, window_end, now_utc(), runtime.diagnostic_settings.mysql.timezone
            )
            offline_details_available = True
        except ValueError:
            pass  # 旧卡仍支持原窗口，新明细单独声明不可用。
    devices_total = (
        None
        if card == OPERATIONS_CARD_OFFLINE and runtime.diagnostic_settings.mysql.timezone
        else sources.mysql.count_devices(effective_sites)
    )
    window = dict(
        language=language,
        site_ids=site_ids,
        window_start=parse_stored_time(window_start),
        window_end=parse_stored_time(window_end),
    )
    if card == OPERATIONS_CARD_OFFLINE:
        details, references = None, None
        if runtime.diagnostic_settings.mysql.timezone:
            from aiops_diagnostics.data import offline_source

            raw, details = offline_source.read(
                sources.mysql,
                effective_sites,
                window_start,
                window_end,
                now_utc(),
                include_legacy=True,
                summary_only=not offline_details_available,
            )
            devices_total = raw.pop("devices_total")
            references = details.pop("references")
            offline_source.require_references(sources.mysql, references)
        else:
            raw = sources.mysql.aggregate_offline_incidents(effective_sites, db_start, db_end)
        if raw is None:
            return empty_card_result(card, language, site_ids, window_start, window_end)
        aggregate = OfflineAggregate(
            incidents=int(raw["incidents"]),
            offline_devices=int(raw["offline_devices"]),
            open_incidents=int(raw["open_incidents"]),
            offline_minutes=int(raw["offline_minutes"]),
            devices_total=devices_total,
            details=details,
        )
        if aggregate.incidents == 0:
            result = build_offline_contract(aggregate, None, reason_code=REASON_NO_INCIDENTS, **window)
        else:
            payload = _invoke_narrative(runtime, card, aggregate, window, runtime_mod=runtime_mod)
            result = build_offline_contract(aggregate, payload, **window)
        if references is not None:
            result["_offline_references"] = references
        elif runtime.diagnostic_settings.mysql.timezone:
            result["device_offline"] = {"status": "unavailable", "reason": "DETAIL_WINDOW_UNSUPPORTED"}
        return result
    if card == OPERATIONS_CARD_PAYMENT:
        raw_payment = sources.mysql.aggregate_payment_split(effective_sites, db_start, db_end)
        if raw_payment is None:
            return empty_card_result(card, language, site_ids, window_start, window_end)
        payment = PaymentAggregate(
            total=int(raw_payment["total"]),
            paid=int(raw_payment["paid"]),
            paid_refunded=int(raw_payment["paid_refunded"]),
            paid_net=int(raw_payment["paid_net"]),
            unpaid=int(raw_payment["unpaid"]),
        )
        if payment.total == 0:
            return build_payment_contract(payment, None, reason_code=REASON_NO_ORDERS, **window)
        payload = _invoke_narrative(runtime, card, payment, window, runtime_mod=runtime_mod)
        return build_payment_contract(payment, payload, **window)
    raw = sources.mysql.aggregate_fault_codes(effective_sites, db_start, db_end)
    if raw is None:
        return empty_card_result(card, language, site_ids, window_start, window_end)
    aggregate = FaultCodeAggregate(
        total_faults=int(raw["total_faults"]),
        faulty_devices=int(raw["faulty_devices"]),
        top_codes=tuple(raw.get("top_codes") or ()),
        devices_total=devices_total,
    )
    if aggregate.total_faults == 0:
        return build_fault_code_contract(aggregate, None, reason_code=REASON_NO_FAULTS, **window)
    payload = _invoke_narrative(runtime, card, aggregate, window, runtime_mod=runtime_mod)
    return build_fault_code_contract(aggregate, payload, **window)


def analyze_with_narrative(
    runtime: Any,
    aggregate: Any,
    context: ScopeContext,
    site_ids: tuple[str, ...] | None,
    window_start: str,
    window_end: str,
    language: str,
    *,
    runtime_mod: Any,
) -> dict[str, Any]:
    """失败率卡的叙述轮与契约组装。"""
    del context
    from aiops_diagnostics.operator.ops_analysis_narrative import run_narrative_turn

    selected_provider = runtime.diagnostic_settings.agent.select_provider(None)
    settings = runtime_mod.Settings.from_config(runtime.gateway_settings.server_config_file)
    facts = build_failure_rate_contract(
        aggregate,
        None,
        language=language,
        site_ids=site_ids,
        window_start=parse_stored_time(window_start),
        window_end=parse_stored_time(window_end),
    )
    payload = run_narrative_turn(
        facts,
        settings.agent,
        provider=selected_provider,
        key_slot=selected_provider.resolved_key_slot(),
        language=language,
    )
    return build_failure_rate_contract(
        aggregate,
        payload,
        language=language,
        site_ids=site_ids,
        window_start=parse_stored_time(window_start),
        window_end=parse_stored_time(window_end),
    )


def execute_operations_card(
    runtime: Any,
    analysis_id: str,
    context: ScopeContext,
    card: str,
    site_ids: tuple[str, ...] | None,
    window_start: str,
    window_end: str,
    language: str,
    *,
    runtime_mod: Any,
) -> None:
    """执行离线/故障码/支付运维分析卡作业，任何异常均收敛为 ``failed`` 终态。"""
    if not runtime.store.update_standard_diagnosis(analysis_id, status="running"):
        return  # 行已终态：没有生成任何东西，直接退出
    started_ms = time.monotonic()
    try:
        scope = runtime_mod.resolve_query_scope(context)
        effective_sites = intersect_sites(site_ids, scope.site_ids)
        if effective_sites == ():
            result = empty_card_result(card, language, site_ids, window_start, window_end)
        elif card == OPERATIONS_CARD_OFFLINE and runtime.diagnostic_settings.mysql.timezone:
            with scoped_mysql_source(runtime.diagnostic_settings, scope) as mysql:
                result = compute_card(
                    runtime,
                    card,
                    SimpleNamespace(mysql=mysql),
                    effective_sites,
                    window_start,
                    window_end,
                    language,
                    site_ids,
                    runtime_mod=runtime_mod,
                )
        else:
            with runtime_mod.scoped_live_sources(runtime.diagnostic_settings, scope=scope) as sources:
                result = compute_card(
                    runtime,
                    card,
                    sources,
                    effective_sites,
                    window_start,
                    window_end,
                    language,
                    site_ids,
                    runtime_mod=runtime_mod,
                )
    except Exception as exc:  # noqa: BLE001 - 逃逸会导致作业长期滞留 running
        ok = runtime.store.update_standard_diagnosis(
            analysis_id,
            status="failed",
            error_code="ANALYSIS_FAILED",
            error_message=_internal_error_message(exc, ""),
        )
        if ok:
            runtime._record_metric(
                tenant_id=context.effective_tenant_id,
                route_type=metrics_dims.ROUTE_OPERATIONS_ANALYSIS,
                outcome="failed",
                error_code="ANALYSIS_FAILED",
                duration_ms=int((time.monotonic() - started_ms) * 1000),
            )
        return
    runtime._end_analysis_job(
        analysis_id,
        context.effective_tenant_id,
        result=result,
        started_ms=started_ms,
    )


def execute_operations_analysis(
    runtime: Any,
    analysis_id: str,
    context: ScopeContext,
    site_ids: tuple[str, ...] | None,
    window_start: str,
    window_end: str,
    language: str,
    *,
    runtime_mod: Any,
) -> None:
    """执行订单失败率运维分析作业，任何异常均收敛为 ``failed`` 终态。"""
    if not runtime.store.update_standard_diagnosis(analysis_id, status="running"):
        return  # 行已终态（过期/被另一条路径收敛）：没有生成任何东西，直接退出
    started_ms = time.monotonic()
    try:
        scope = runtime_mod.resolve_query_scope(context)
        effective_sites = intersect_sites(site_ids, scope.site_ids)
        if effective_sites == ():
            result = build_failure_rate_contract(
                AnalysisAggregate(0, 0, 0, ()),
                None,
                language=language,
                site_ids=site_ids,
                window_start=parse_stored_time(window_start),
                window_end=parse_stored_time(window_end),
                reason_code=REASON_SCOPE_EMPTY,
            )
        else:
            with runtime_mod.scoped_live_sources(runtime.diagnostic_settings, scope=scope) as sources:
                raw = sources.mysql.aggregate_order_statuses(
                    effective_sites,
                    db_window_bound(window_start),
                    db_window_bound(window_end),
                )
            if raw is None:
                result = build_failure_rate_contract(
                    AnalysisAggregate(0, 0, 0, ()),
                    None,
                    language=language,
                    site_ids=site_ids,
                    window_start=parse_stored_time(window_start),
                    window_end=parse_stored_time(window_end),
                    reason_code=REASON_SCOPE_EMPTY,
                )
            else:
                aggregate = AnalysisAggregate(
                    total=int(raw["total"]),
                    abnormal_ended=int(raw["abnormal_ended"]),
                    normal_ended=int(raw["normal_ended"]),
                    top_sites=tuple(raw.get("top_sites") or ()),
                )
                if aggregate.total == 0:
                    result = build_failure_rate_contract(
                        aggregate,
                        None,
                        language=language,
                        site_ids=site_ids,
                        window_start=parse_stored_time(window_start),
                        window_end=parse_stored_time(window_end),
                        reason_code=REASON_NO_ORDERS,
                    )
                else:
                    result = analyze_with_narrative(
                        runtime,
                        aggregate,
                        context,
                        site_ids,
                        window_start,
                        window_end,
                        language,
                        runtime_mod=runtime_mod,
                    )
    except Exception as exc:  # noqa: BLE001 - 逃逸会导致作业长期滞留 running
        ok = runtime.store.update_standard_diagnosis(
            analysis_id,
            status="failed",
            error_code="ANALYSIS_FAILED",
            error_message=_internal_error_message(exc, ""),
        )
        if ok:
            runtime._record_metric(
                tenant_id=context.effective_tenant_id,
                route_type=metrics_dims.ROUTE_OPERATIONS_ANALYSIS,
                outcome="failed",
                error_code="ANALYSIS_FAILED",
                duration_ms=int((time.monotonic() - started_ms) * 1000),
            )
        return
    runtime._end_analysis_job(
        analysis_id,
        context.effective_tenant_id,
        result=result,
        started_ms=started_ms,
    )
