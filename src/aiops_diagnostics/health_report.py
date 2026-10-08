from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.health_metrics import RULE_VERSION as HEALTH_RULE_VERSION
from aiops_diagnostics.health_report_copy import health_summary
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.rules import classify_stop_reason

#: Re-exported from `health_metrics` so there is exactly ONE rule version.
#: It used to be a second constant here with a different value
#: (`health-v1`), while `enrich_report` stamped `health-v2` over the body —
#: so one response carried two answers and nothing compared them (#602).
#: Kept under this name because it is the one callers already import.


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
    language: str = DEFAULT_LANGUAGE,
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
            language=language,
        )
        status = "abnormal" if stop.abnormal is True else "normal" if stop.abnormal is False else "attention"
        # `value` is the LOCALIZED text and `reported_value` is the upstream's
        # own sentence. They were one field until #599: the value preferred the
        # upstream sentence, so an English report showed 66.3%-of-orders' worth
        # of Chinese. The two are now separate because they answer different
        # questions — what happened (ours, translated) vs what the charger said
        # (its words, language not guaranteed).
        indicator = _indicator(
            status=status,
            value=stop.description,
            reported_value=content or None,
        )

    completeness = 0.0 if indicator["status"] == "unavailable" else 1.0
    summary_key = (
        "normal"
        if indicator["status"] == "normal"
        else "attention"
        if indicator["status"] in {"attention", "abnormal"}
        else "insufficient"
    )
    summary = health_summary(language, summary_key)
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
        "source_summary": {
            "order_snapshot": "available",
            "telemetry": "not_requested",
            "protocol": "not_requested",
            "vehicle_capacity": "not_requested",
        },
    }


def _indicator(
    *,
    status: str,
    value: object,
    reported_value: str | None = None,
    reported_language: str | None = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    return {
        "code": "stop_reason",
        "status": status,
        "value": value,
        # The upstream's own words, verbatim. Its language is NOT the report's
        # and is not guaranteed to be any of ours, which is exactly why it is a
        # separate field: a client can show it as a quotation and knows not to
        # treat it as localized copy (#599).
        "reported_value": reported_value,
        # Null until the source actually tells us what language it wrote in —
        # which today it never does. A guess here would be a claim we cannot
        # support; the field exists so the shape does not change when one day
        # a source does declare it.
        "reported_language": reported_language,
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
