from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aiops_diagnostics.health_report_copy import stop_reason_fallback, ykc_stop_reason
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE

ORDER_STATUS = {
    0: "充电中",
    1: "充电结束",
    2: "不可控异常",
    3: "异常已人工处理",
    5: "可控异常结束",
}

# The YKC code table moved to `health_report_copy.YKC_STOP_REASON_MESSAGES`,
# which carries it in every language. It is NOT kept here as well: the two
# copies were byte-identical, classification read only the new one, and a
# duplicated catalog whose second copy nobody reads drifts the first time
# someone edits one of them. (Review finding on #539.)


@dataclass(slots=True)
class StopReason:
    classification: str
    description: str
    abnormal: bool | None


def status_label(status: object) -> str:
    try:
        numeric = int(status)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "未知"
    return ORDER_STATUS.get(numeric, f"未定义状态({numeric})")


def _fallback(language: str, classification: str) -> str:
    """Our own stop-reason text for a classification the upstream left blank.

    Kept as a named lookup so the table stays the single source; the value is
    never empty (``stop_reason_fallback`` falls back to the default language).
    If even that misses, the CLASSIFICATION NAME is returned — an identifier,
    never prose, so a reader gets a stable code rather than nothing.
    """
    resolved = stop_reason_fallback(language, classification)
    return resolved if resolved else classification


def _report_prose(classification: str, language: str) -> str:
    """The LOCALIZED stop-reason text — never the upstream's words.

    The stop reason has two parts with different owners, and they used to be
    conflated into one field: the upstream's own sentence (its words, its
    language, not ours to rewrite) and our classification of it (our words,
    therefore ours to localize).

    Measured on 41 across all 30,955 orders: **66.3% of the upstream sentences
    are Chinese**, and the classifications that carried one straight into the
    reader-facing value covered **59.6%** of orders — so a report requested in
    English showed 「拔出断电」 in most cases (#566 found it, #599 named it).
    The upstream's sentence has not become translatable; it moves to
    `reported_value`, where it is evidence. This function decides only the
    localized half.

    One case deserves its own wording: ``reported_stop_reason`` is what we
    return when nothing matched, so the only thing we know is that the upstream
    DID report something. We say exactly that and do not describe it —
    paraphrasing an unclassifiable sentence would be re-interpreting the report,
    which is the line this project does not cross.
    """
    return _fallback(language, classification)


def classify_stop_reason(
    protocol: str | None,
    code: object,
    content: str | None,
    *,
    language: str = DEFAULT_LANGUAGE,
) -> StopReason:
    normalized_protocol = (protocol or "").upper()
    code_text = "" if code is None else str(code).strip()
    content_text = (content or "").strip()

    # Every branch below returns OUR words for `description`; the upstream's
    # sentence is never echoed into it. `reported_value` is where it goes.
    if code_text == "-1":
        return StopReason("manual_stop", _report_prose("manual_stop", language), False)
    if code_text == "-2":
        return StopReason("package_exhausted", _report_prose("package_exhausted", language), False)
    if code_text == "-3":
        return StopReason("start_failure", _report_prose("start_failure", language), True)

    if normalized_protocol.startswith("YKC"):
        numeric = _parse_stop_code(code_text)
        if numeric is not None:
            # `content_text` is the LAST resort here, and that is the point:
            # the YKC table is ours and covers the code, so it wins. The
            # upstream sentence only stands in when the code is not in the table
            # — and it is still the upstream's sentence, so `reported_value`
            # carries it as well.
            description = ykc_stop_reason(
                language,
                numeric,
                _fallback(language, "ykc_unknown_code").format(code=numeric),
            )
            if numeric in {78, 110}:
                return StopReason("balance_insufficient", description, True)
            if 74 <= numeric <= 102:
                return StopReason("start_failure", description, True)
            if 64 <= numeric <= 73:
                return StopReason("normal_stop", description, False)
            if numeric in {116, 124, 126, 128}:
                return StopReason("over_temperature", description, True)
            if numeric == 131:
                return StopReason("power_loss", description, True)
            if numeric >= 106:
                return StopReason("device_or_vehicle_fault", description, True)

    lowered = content_text.lower()
    if any(word in lowered for word in ("余额不足", "欠费", "余额限制")):
        return StopReason("balance_insufficient", _report_prose("balance_insufficient", language), True)
    if any(word in lowered for word in ("温度", "过温", "高温")):
        return StopReason("over_temperature", _report_prose("over_temperature", language), True)
    if any(word in lowered for word in ("拔枪", "手动停止", "主动停止", "充满")):
        return StopReason("user_or_normal_stop", _report_prose("user_or_normal_stop", language), False)
    if "正常" in lowered:
        return StopReason("normal_stop", _report_prose("normal_stop", language), False)
    if any(word in lowered for word in ("断网", "离线", "通讯", "通信", "断电")):
        return StopReason(
            "communication_or_power_loss",
            _report_prose("communication_or_power_loss", language),
            True,
        )
    if content_text:
        # We could not classify it. Say THAT, rather than echoing the upstream's
        # sentence into a field the contract tells clients to render (#566/#599:
        # 23.5% of real orders take this branch, and 66.3% of upstream sentences
        # are Chinese, so echoing it leaked Chinese into non-Chinese reports).
        # The upstream's sentence is not lost — the report carries it separately
        # as `reported_value`; this `description` is the LOCALIZED half only.
        return StopReason("reported_stop_reason", _report_prose("reported_stop_reason", language), None)
    # `code_text` wins when present: it is the upstream's CODE — an identifier,
    # not the sentence describing the stop. Reporting a code is honest; saying
    # "no stop reason provided" while holding one would not be.
    return StopReason(
        "unknown_stop_reason",
        code_text or _report_prose("unknown_stop_reason", language),
        None,
    )


def is_server_billing(protocol: str | None) -> bool:
    normalized = (protocol or "").upper()
    return normalized.startswith("OCPP") or normalized.startswith("AYK")


#: Context minutes padded onto each side of an order before querying time-series
#: sources: the gun/comm evidence for an order begins just before `created_time`
#: and ends just after `stop_time`, so the query window has to include both edges.
ORDER_WINDOW_PADDING_MINUTES = 5


def order_window(
    created_time: object,
    stop_time: object,
    max_hours: int,
) -> tuple[datetime, datetime, bool] | None:
    """The one definition of the time range one diagnosis may read.

    Both diagnosis paths call this and must agree: the deterministic engine and
    the agent tool layer query the same TDengine tables for the same order, so a
    different window in either would report a different slice of the same
    evidence. It is also the enforcement point for the bounded-read safety rule
    (ADR-0001): an order whose real span exceeds ``max_hours`` is **clamped**,
    never queried unbounded, and the returned ``clamped`` flag is what lets a
    caller say so in its report instead of silently returning a partial answer.

    Returns ``(start, end, clamped)``, or ``None`` when the order carries no
    usable ``created_time`` — a caller that cannot bound a window must not
    invent one. Naive/aware datetimes are reconciled rather than rejected,
    because the two columns arrive from different writers with different
    tzinfo completeness; when one side is naive it adopts the other's zone, and
    when both are aware the later one is converted to the earlier one's zone.
    """
    created = _as_datetime(created_time)
    if not created:
        return None
    stopped = _as_datetime(stop_time) or datetime.now(tz=created.tzinfo)
    if created.tzinfo is None and stopped.tzinfo is not None:
        created = created.replace(tzinfo=stopped.tzinfo)
    elif created.tzinfo is not None and stopped.tzinfo is None:
        stopped = stopped.replace(tzinfo=created.tzinfo)
    elif created.tzinfo is not None and stopped.tzinfo is not None:
        stopped = stopped.astimezone(created.tzinfo)
    padding = timedelta(minutes=ORDER_WINDOW_PADDING_MINUTES)
    start = created - padding
    end = stopped + padding
    maximum_end = start + timedelta(hours=max_hours)
    clamped = end > maximum_end
    return start, min(end, maximum_end), clamped


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_stop_code(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    except ValueError:
        return None
