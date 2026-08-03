from __future__ import annotations

import re

from aiops_diagnostics.models import DiagnosticRequest, Intent

SAFE_ORDER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
LABELED_ORDER_PATTERN = re.compile(
    r"(?:订单(?:号|编号)?|order\s*(?:no|id)?)\s*[:：#]?\s*([A-Za-z0-9_-]{6,64})",
    re.IGNORECASE,
)
NUMERIC_ORDER_PATTERN = re.compile(r"(?<![A-Za-z0-9])([0-9]{10,32})(?![A-Za-z0-9])")

INTENT_KEYWORDS: list[tuple[Intent, tuple[str, ...]]] = [
    (Intent.SYNC, ("同步", "商城", "对账", "下游", "消息丢失", "队列")),
    (Intent.START_FAILURE, ("启动失败", "无法启动", "启动不了", "扫码失败", "刷卡失败")),
    (Intent.OFFLINE, ("离线", "通讯中断", "断网", "无报文", "连接中断")),
    (Intent.AMOUNT, ("金额", "费用", "电量", "计费", "服务费", "电费", "负数")),
    (Intent.ABNORMAL_STOP, ("中途停止", "中途停机", "异常停止", "异常停机", "自动停止", "拔枪")),
]


def parse_request(text: str, order_no: str | None = None, tenant_id: str | None = None) -> DiagnosticRequest:
    normalized = text.strip()
    resolved_order_no = order_no.strip() if order_no else extract_order_no(normalized)
    if not resolved_order_no:
        raise ValueError("未识别到订单号，请使用 --order-no 明确指定")
    if not SAFE_ORDER_PATTERN.fullmatch(resolved_order_no):
        raise ValueError("订单号只能包含字母、数字、下划线或连字符，长度为 6-64")
    return DiagnosticRequest(
        order_no=resolved_order_no,
        problem=normalized,
        intent=detect_intent(normalized),
        tenant_id=tenant_id.strip() if tenant_id else None,
    )


def extract_order_no(text: str) -> str | None:
    labeled = LABELED_ORDER_PATTERN.search(text)
    if labeled:
        return labeled.group(1)
    numeric = NUMERIC_ORDER_PATTERN.search(text)
    return numeric.group(1) if numeric else None


def detect_intent(text: str) -> Intent:
    lowered = text.lower()
    for intent, keywords in INTENT_KEYWORDS:
        if any(keyword.lower() in lowered for keyword in keywords):
            return intent
    return Intent.GENERAL
