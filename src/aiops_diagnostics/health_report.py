from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.rules import classify_stop_reason

HEALTH_RULE_VERSION = "health-v1"


class HealthReportError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OrderSource(Protocol):
    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]: ...


def build_minimal_health_report(
    sources: OrderSource,
    order_no: str,
    safety: SafetySettings,
) -> dict[str, Any]:
    orders = sources.get_orders(order_no)
    if not orders:
        raise HealthReportError("order not found", code="ORDER_NOT_FOUND")
    if len(orders) != 1:
        raise HealthReportError("order is ambiguous", code="ORDER_AMBIGUOUS")

    order = orders[0]
    if _integer(order.get("status")) == 0:
        raise HealthReportError("order has not ended", code="ORDER_NOT_ENDED")
    if not (order.get("child_device_code") or order.get("device_code")):
        raise HealthReportError("order device is missing", code="DEVICE_MISSING")

    started_at = _datetime(order.get("created_time"))
    stopped_at = _datetime(order.get("stop_time"))
    if started_at is None or stopped_at is None or stopped_at <= started_at:
        raise HealthReportError("order time window is invalid", code="ORDER_WINDOW_INVALID")
    if (stopped_at - started_at).total_seconds() > safety.max_order_window_hours * 3600:
        raise HealthReportError("order time window is too large", code="ORDER_WINDOW_TOO_LARGE")

    content = str(order.get("stopped_reason_content") or "").strip()
    code = order.get("stopped_reason_code")
    if not content and code in (None, ""):
        indicator = _indicator(
            status="unavailable",
            value=None,
            reason_code="SOURCE_DATA_MISSING",
        )
    else:
        stop = classify_stop_reason(
            str(order.get("device_protocol") or ""),
            code,
            content,
        )
        status = "abnormal" if stop.abnormal is True else "normal" if stop.abnormal is False else "attention"
        indicator = _indicator(status=status, value=content or stop.description)

    completeness = 0.0 if indicator["status"] == "unavailable" else 1.0
    summary = (
        "本次充电健康报告包含 1 项正常指标"
        if indicator["status"] == "normal"
        else "本次充电健康报告有 1 项指标需关注"
        if indicator["status"] in {"attention", "abnormal"}
        else "本次充电健康报告有 1 项指标因数据不足无法评估"
    )
    return {
        "order_no": order_no,
        "order_window": {
            "started_at": started_at.astimezone(UTC).isoformat(),
            "stopped_at": stopped_at.astimezone(UTC).isoformat(),
        },
        "summary": summary,
        "indicators": [indicator],
        "completeness": completeness,
        "rule_version": HEALTH_RULE_VERSION,
        "data_as_of": datetime.now(UTC).isoformat(),
    }


def _indicator(
    *,
    status: str,
    value: object,
    reason_code: str | None = None,
) -> dict[str, Any]:
    return {
        "code": "stop_reason",
        "status": status,
        "value": value,
        "unit": None,
        "reference": None,
        "reason_code": reason_code,
    }


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
