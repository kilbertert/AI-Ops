from __future__ import annotations

from dataclasses import dataclass

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


def _parse_stop_code(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    except ValueError:
        return None
