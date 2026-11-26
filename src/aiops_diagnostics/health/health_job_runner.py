"""充电健康报告异步作业执行器。

从 `gateway_runtime.GatewayRuntime` 拆出，使健康报告的曲线/指标装配与统一终态写入
收敛在 `health/` 目录内。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.common.scalar_rules import as_datetime
from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.health.health_curves import build_curves
from aiops_diagnostics.health.health_metrics import enrich_report
from aiops_diagnostics.health.health_report import HealthReportError, build_minimal_health_report
from aiops_diagnostics.health.ykc_frames import (
    YkcProtocolEvidence,
    extract_gun_no,
    parse_ykc_comm_frames,
    read_tx_serial_no,
)
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


def normalize_tdengine_gun_samples(samples: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """将 TDengine ``charging-gun_property`` 行归一化为健康报告口径。

    41 生产库实测 ``power`` 单位为 W（``outputVoltage * outputCurrent ≈ power``），
    规格 §5 定义功率曲线单位为 kW（``V × I / 1000``），因此在此统一换算为 kW，
    避免把 50,000 W 原样标成 50,000 kW。
    """
    normalized: list[dict[str, Any]] = []
    for row in samples:
        item = dict(row)
        voltage = _number(item.get("outputVoltage"))
        current = _number(item.get("outputCurrent"))
        power = _number(item.get("power"))
        if voltage is not None and current is not None and voltage > 0 and current >= 0:
            item["power"] = round(voltage * current / 1000.0, 3)
        elif power is not None:
            # 无 V/I 时按 W -> kW 折算（单桩实时功率不会 > 2000 kW，> 1000 必为 W）
            item["power"] = round(power / 1000.0, 3) if power > 1000.0 else round(power, 3)
        normalized.append(item)
    return normalized


def execute_health_report(
    runtime: Any,
    job_id: str,
    context: ScopeContext,
    order_no: str,
    *,
    runtime_mod: Any,
) -> None:
    """执行健康报告作业：取订单、枪遥测与 YKC 协议帧 → 建曲线/指标 → 收敛终态。"""
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
            gun_device = str(order.get("child_device_code") or order.get("device_code"))
            pile_device = str(order.get("device_code") or "").strip()
            created_at = as_datetime(order.get("created_time"))
            stopped_at = as_datetime(order.get("stop_time"))

            try:
                if created_at is None or stopped_at is None:
                    raise SourceError("order timestamps unavailable")
                raw_samples = sources.get_gun_samples(gun_device, created_at, stopped_at, None)
            except SourceError:
                samples: list[dict[str, Any]] = []
                report["source_summary"]["telemetry"] = "unavailable"
            else:
                samples = normalize_tdengine_gun_samples(raw_samples)
                report["source_summary"]["telemetry"] = "available" if samples else "unavailable"

            protocol = YkcProtocolEvidence()
            if (
                pile_device
                and created_at is not None
                and stopped_at is not None
                and hasattr(sources, "get_pile_comm_frames")
            ):
                try:
                    raw_frames = sources.get_pile_comm_frames(pile_device, created_at, stopped_at)
                except SourceError:
                    report["source_summary"]["protocol"] = "unavailable"
                else:
                    protocol = parse_ykc_comm_frames(
                        raw_frames,
                        gun_no=extract_gun_no(pile_device, str(order.get("child_device_code") or "")),
                        tx_serial_no=read_tx_serial_no(order),
                    )
                    report["source_summary"]["protocol"] = "available" if protocol.has_data else "unavailable"

            report["curves"] = build_curves(samples, job_language, protocol=protocol)
            report = enrich_report(
                report,
                samples,
                order,
                protocol=protocol,
                language=job_language,
            )
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


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
