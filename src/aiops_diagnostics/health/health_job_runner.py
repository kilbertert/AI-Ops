"""充电健康报告异步作业执行器。

从 `gateway_runtime.GatewayRuntime` 拆出，使健康报告的曲线/指标装配与统一终态写入
收敛在 `health/` 目录内。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.common.scalar_rules import as_datetime
from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.health.health_curves import build_curves
from aiops_diagnostics.health.health_metrics import enrich_report
from aiops_diagnostics.health.health_report import HealthReportError, build_minimal_health_report
from aiops_diagnostics.identity.scope_context import ScopeContext

_LOGGER = logging.getLogger("aiops.gateway_runtime")


def finish_health_job(runtime: Any, job_id: str, **fields: Any) -> bool:
    """写入健康报告作业终态；若行已终态被拒，记录告警日志并返回 ``False``。"""
    if not runtime.store.update_health_job(job_id, **fields):
        _LOGGER.warning(
            "health job terminal write refused: job=%s status=%s",
            job_id,
            fields.get("status"),
        )
        return False
    return True


def execute_health_report(
    runtime: Any,
    job_id: str,
    context: ScopeContext,
    order_no: str,
    *,
    runtime_mod: Any,
) -> None:
    """执行健康报告作业：取订单与枪采样 → 建曲线/指标 → 经 ``finish_health_job`` 收敛终态。"""
    if not runtime.store.update_health_job(job_id, status="running"):
        return
    started = time.monotonic()
    try:
        scope = runtime_mod.resolve_query_scope(context)
        with runtime_mod.scoped_live_sources(runtime.diagnostic_settings, scope=scope) as sources:
            job_language = str(
                runtime.store.get_health_job(job_id, context.scope_fingerprint).get("language")
                or DEFAULT_LANGUAGE
            )
            report = build_minimal_health_report(
                sources,
                order_no,
                runtime.diagnostic_settings.safety,
                language=job_language,
            )
            order = sources.get_orders(order_no)[0]
            device = str(order.get("child_device_code") or order.get("device_code"))
            try:
                created_at = as_datetime(order.get("created_time"))
                stopped_at = as_datetime(order.get("stop_time"))
                if created_at is None or stopped_at is None:
                    raise SourceError("order timestamps unavailable")
                samples = sources.get_gun_samples(device, created_at, stopped_at, None)
            except SourceError:
                samples = []
                report["source_summary"]["telemetry"] = "unavailable"
            else:
                report["source_summary"]["telemetry"] = "available" if samples else "unavailable"
            report["curves"] = build_curves(samples, job_language)
            report = enrich_report(report, samples, order)
        if time.monotonic() - started > 30:
            finish_health_job(
                runtime,
                job_id,
                status="failed",
                error_code="REPORT_TIMEOUT",
                error_message="health report timed out",
            )
            return
        finish_health_job(runtime, job_id, status="completed", report=report)
    except HealthReportError as exc:
        finish_health_job(
            runtime,
            job_id,
            status="failed",
            error_code=exc.code,
            error_message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - 逃逸会导致作业长期滞留 running
        finish_health_job(
            runtime,
            job_id,
            status="failed",
            error_code="SOURCE_UNAVAILABLE",
            error_message=(
                f"{exc.__class__.__name__}: {exc}"
                if isinstance(exc, (SourceError, ValueError))
                else exc.__class__.__name__
            ),
        )
