"""充电健康报告异步作业执行器。

从 `gateway_runtime.GatewayRuntime` 拆出，使健康报告的曲线/指标装配与统一终态写入
收敛在 `health/` 目录内。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiops_diagnostics.common.bounded_http import (
    ENVELOPE_SUCCESS_CODES,
    ErrorMapping,
    RequestSpec,
    json_body,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.common.scalar_rules import as_datetime
from aiops_diagnostics.data.sources import (
    COMM_FRAME_CODES,
    COMM_STREAM_CODES,
    COMM_SUMMARY_CODES,
    GUN_COLUMN_NAMES,
    SAFE_VALUE,
    SourceError,
)
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
            # TDengine charging-gun_property.power 单位恒为 W，无 V/I 时同样统一折算为 kW
            item["power"] = round(power / 1000.0, 3)
        normalized.append(item)
    return normalized


_TSDATA_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_TSDATA_MAX_ROWS = 2000
_ALLOWED_GUN_ROW_KEYS = frozenset({"_ts", "batteryMinTemperature", *GUN_COLUMN_NAMES})
_ALLOWED_COMM_ROW_KEYS = frozenset({"_ts", "code", "direction", "raw"})
_ALLOWED_COMM_CODES = frozenset(code.lower() for code in COMM_FRAME_CODES)


class _TsdataInvalidColumnError(SourceError):
    """TDengine 门面因缺少可选列拒绝 ``fields`` 列表，仅允许单次无 ``fields`` 回退。"""


def _format_tsdata_time(value: datetime, timezone_name: str) -> str:
    if value.tzinfo is not None:
        try:
            value = value.astimezone(ZoneInfo(timezone_name))
        except Exception:  # noqa: BLE001 - 未知时区时退回本地 naive 表达
            value = value.replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _parse_tsdata_health_envelope(
    payload: object,
    mapping: ErrorMapping[SourceError],
) -> Any:
    if isinstance(payload, Mapping) and payload.get("code") not in ENVELOPE_SUCCESS_CODES:
        detail = payload.get("msg") if payload.get("msg") is not None else payload.get("message")
        if "Invalid column name" in str(detail or ""):
            raise _TsdataInvalidColumnError("tsdata query rejected optional column")
    return parse_code_data_envelope(payload, mapping)


def _post_tsdata_query(base_url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    base = base_url.strip().rstrip("/")
    if not base:
        raise SourceError("tsdata URL is not configured")

    def _unavailable(failure: Any) -> SourceError:
        return SourceError(f"tsdata query failed: {failure.detail}")

    data = request_json(
        RequestSpec(
            url=f"{base}/tdadmin/data/query",
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=json_body(payload),
            timeout=min(max(1.0, float(timeout)), 15.0),
            max_read_bytes=_TSDATA_MAX_RESPONSE_BYTES,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=_unavailable,
            http_error=_unavailable,
            unavailable=_unavailable,
            invalid_body=_unavailable,
            invalid_envelope=_unavailable,
        ),
        envelope=_parse_tsdata_health_envelope,
    )
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise SourceError("tsdata query returned invalid records payload")
    return data


def fetch_tsdata_gun_samples(
    base_url: str,
    device: str,
    start_time: datetime,
    end_time: datetime,
    *,
    timezone_name: str = "Asia/Shanghai",
    limit: int = _TSDATA_MAX_ROWS,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """经公司 ``cloud-tsdata-biz`` 门面只读拉取枪遥测样本（缺列时单次回退全列再白名单投影）。"""
    if not SAFE_VALUE.fullmatch(device):
        raise SourceError("tsdata gun device contains invalid characters")
    page_size = min(max(1, int(limit)), _TSDATA_MAX_ROWS)
    query = {
        "table": "charging-gun_property",
        "deviceId": device,
        "start": _format_tsdata_time(start_time, timezone_name),
        "end": _format_tsdata_time(end_time, timezone_name),
        "fields": ["_ts", *GUN_COLUMN_NAMES],
        "orderBy": "_ts",
        "order": "ASC",
        "page": 1,
        "pageSize": page_size,
    }
    try:
        data = _post_tsdata_query(base_url, query, timeout=timeout)
    except _TsdataInvalidColumnError:
        fallback_query = {key: value for key, value in query.items() if key != "fields"}
        data = _post_tsdata_query(base_url, fallback_query, timeout=timeout)

    return [
        {key: row[key] for key in row if key in _ALLOWED_GUN_ROW_KEYS}
        for row in data["records"]
        if isinstance(row, Mapping)
    ]


def fetch_tsdata_comm_frames(
    base_url: str,
    device: str,
    start_time: datetime,
    end_time: datetime,
    *,
    timezone_name: str = "Asia/Shanghai",
    limit: int = _TSDATA_MAX_ROWS,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """经公司 ``cloud-tsdata-biz`` 门面只读拉取五类 YKC BMS 协议帧，过滤后仅保留允许列。"""
    if not SAFE_VALUE.fullmatch(device):
        raise SourceError("tsdata pile device contains invalid characters")
    page_size = min(max(1, int(limit)), _TSDATA_MAX_ROWS)
    query = {
        "table": "charging-pile_comm",
        "deviceId": device,
        "start": _format_tsdata_time(start_time, timezone_name),
        "end": _format_tsdata_time(end_time, timezone_name),
        "fields": ["_ts", "code", "direction", "raw"],
        "orderBy": "_ts",
        "order": "ASC",
        "page": 1,
        "pageSize": page_size,
    }
    data = _post_tsdata_query(base_url, query, timeout=timeout)
    return [
        {key: row[key] for key in row if key in _ALLOWED_COMM_ROW_KEYS}
        for row in data["records"]
        if isinstance(row, Mapping) and str(row.get("code") or "").strip().lower() in _ALLOWED_COMM_CODES
    ]


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
    tsdata_url = str(
        getattr(getattr(runtime, "gateway_settings", None), "operator_tsdata_url", "") or ""
    ).strip()
    safety = runtime.diagnostic_settings.safety
    tz_name = str(
        getattr(getattr(runtime.diagnostic_settings, "mysql", None), "timezone", "") or "Asia/Shanghai"
    )
    td_limit = int(safety.tdengine_max_rows)
    td_timeout = float(safety.query_timeout_seconds)
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
                safety,
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
                try:
                    raw_samples = sources.get_gun_samples(gun_device, created_at, stopped_at, None)
                except SourceError:
                    if not tsdata_url:
                        raise
                    # 最小接入客户机未配置直连 TDengine，通过受管 tsdata 门面只读兜底。
                    raw_samples = fetch_tsdata_gun_samples(
                        tsdata_url,
                        gun_device,
                        created_at,
                        stopped_at,
                        timezone_name=tz_name,
                        limit=td_limit,
                        timeout=td_timeout,
                    )
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
                    try:
                        # 拆分低频关键帧（0x15/17/19）与高频周期帧（0x23/25），防止长充电会话
                        # 周期报文占满 LIMIT 2000 导致末段 0x19 结算帧丢失。
                        summary_frames = sources.get_pile_comm_frames(
                            pile_device, created_at, stopped_at, codes=COMM_SUMMARY_CODES
                        )
                        stream_frames = sources.get_pile_comm_frames(
                            pile_device, created_at, stopped_at, codes=COMM_STREAM_CODES
                        )
                        raw_frames = [*summary_frames, *stream_frames]
                    except SourceError:
                        if not tsdata_url:
                            raise
                        raw_frames = fetch_tsdata_comm_frames(
                            tsdata_url,
                            pile_device,
                            created_at,
                            stopped_at,
                            timezone_name=tz_name,
                            limit=td_limit,
                            timeout=td_timeout,
                        )
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
