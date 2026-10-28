"""现有聊天入口的本人状态查询；不调用模型或使用运维服务身份。"""

from __future__ import annotations

import re
from typing import Any

from aiops_diagnostics.rules import status_label

_ORDER = re.compile(r"订单|訂單|一单|一單|\border\b", re.I)
_LATEST = re.compile(r"最新|最近|最后|最後|\b(?:latest|most recent|last)\b", re.I)
_STATUS = re.compile(r"状态|狀態|充电中|充電中|结束|結束|\b(?:status|charging|finished|ended)\b", re.I)
_QUERY = re.compile(r"查|查看|看看|\b(?:show|check|look up)\b", re.I)
_DIAGNOSIS = re.compile(
    r"异常|異常|故障|扣费|扣費|退款|金额|金額|投诉|投訴|原因|为什么|為什麼|检测|檢測|诊断|診斷"
    r"|支付|取消|删除|刪除|修改|停止|重算|补发|補發|政策|规则|規則"
    r"|\b(?:fault|refund|charged|fees?|cost|complaint|why|diagnos\w*|pay\w*|cancel|delete|change|stop|policy)\b",
    re.I,
)


def is_order_status_question(question: str) -> bool:
    """保守识别明确查询；未命中的措辞仍走原有路由。"""
    return bool(
        _ORDER.search(question)
        and (_STATUS.search(question) or (_LATEST.search(question) and _QUERY.search(question)))
        and not _DIAGNOSIS.search(question)
    )


def asks_latest_order(question: str) -> bool:
    return bool(_LATEST.search(question))


def status_answer(snapshot: dict[str, Any], language: str, *, localized=False) -> dict[str, Any]:
    from aiops_diagnostics.consumer_language import COPY, live_answer

    if localized and language in COPY:
        return live_answer(snapshot, language, "status")
    label = "未知" if snapshot["status_role"] == "unknown" else status_label(snapshot["status_code"])
    if language == "zh":
        text = (
            f"订单 {snapshot['order_no']} 本次查询状态：{label}（status_code={snapshot['status_code']}，"
            f"status_role={snapshot['status_role']}）。\n来源：{snapshot['source']}；"
            f"观察时间（UTC）：{snapshot['observed_at']}。这是本次业务状态观察，不是故障原因诊断。"
        )
    elif language == "zh-Hant":
        text = (
            f"訂單 {snapshot['order_no']}：狀態碼 {snapshot['status_code']}，"
            f"狀態 {snapshot['status_role']}。\n來源：{snapshot['source']}；"
            f"觀察時間（UTC）：{snapshot['observed_at']}。這是本次業務狀態觀察，不是故障原因診斷。"
        )
    else:
        text = (
            f"Order {snapshot['order_no']}: {snapshot['status_role'].replace('_', ' ')} "
            f"(status code {snapshot['status_code']}).\nSource: {snapshot['source']}; "
            f"observed at (UTC): {snapshot['observed_at']}. This is a business status observation, "
            "not a diagnosis of the cause."
        )
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}
