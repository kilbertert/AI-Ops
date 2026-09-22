from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

ORDER_STATUS = {
    0: "充电中",
    1: "充电结束",
    2: "不可控异常",
    3: "异常已人工处理",
    5: "可控异常结束",
}

YKC_STOP_REASONS = {
    64: "APP 远程停止",
    65: "SOC 达到 100%",
    66: "充电电量满足设定条件",
    67: "充电金额满足设定条件",
    68: "充电时间满足设定条件",
    69: "手动停止充电",
    70: "车端正常主动停止",
    74: "启动失败：充电桩控制系统故障",
    75: "启动失败：控制导引断开",
    76: "启动失败：断路器跳位",
    77: "启动失败：电表通信中断",
    78: "启动失败：余额不足",
    79: "启动失败：充电模块故障",
    80: "启动失败：急停开入",
    83: "启动失败：温度异常",
    85: "启动失败：电子锁异常",
    87: "启动失败：绝缘异常",
    88: "启动失败：枪故障",
    106: "异常中止：系统闭锁",
    107: "异常中止：导引断开",
    108: "异常中止：断路器跳位",
    109: "异常中止：电表通信中断",
    110: "异常中止：余额不足",
    113: "异常中止：充电模块故障",
    114: "异常中止：急停开入",
    116: "异常中止：温度异常",
    119: "异常中止：电子锁异常",
    124: "异常中止：电池组过温",
    131: "异常中止：充电桩断电",
    138: "异常中止：设备故障",
    141: "异常中止：枪故障",
    142: "异常中止：充电数据异常",
    144: "未知原因停止",
}


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


def classify_stop_reason(protocol: str | None, code: object, content: str | None) -> StopReason:
    normalized_protocol = (protocol or "").upper()
    code_text = "" if code is None else str(code).strip()
    content_text = (content or "").strip()

    if code_text == "-1":
        return StopReason("manual_stop", content_text or "平台或用户主动停止", False)
    if code_text == "-2":
        return StopReason("package_exhausted", content_text or "套餐耗尽", False)
    if code_text == "-3":
        return StopReason("start_failure", content_text or "启动失败", True)

    if normalized_protocol.startswith("YKC"):
        numeric = _parse_stop_code(code_text)
        if numeric is not None:
            description = YKC_STOP_REASONS.get(numeric, content_text or f"YKC 停止码 {numeric}")
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
        return StopReason("balance_insufficient", content_text, True)
    if any(word in lowered for word in ("温度", "过温", "高温")):
        return StopReason("over_temperature", content_text, True)
    if any(word in lowered for word in ("拔枪", "手动停止", "主动停止", "充满")):
        return StopReason("user_or_normal_stop", content_text, False)
    if "正常" in lowered:
        return StopReason("normal_stop", content_text, False)
    if any(word in lowered for word in ("断网", "离线", "通讯", "通信", "断电")):
        return StopReason("communication_or_power_loss", content_text, True)
    if content_text:
        return StopReason("reported_stop_reason", content_text, None)
    return StopReason("unknown_stop_reason", code_text or "未提供停止原因", None)


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
