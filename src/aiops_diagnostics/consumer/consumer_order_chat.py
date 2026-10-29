"""现有聊天入口的本人状态查询；不调用模型或使用运维服务身份。"""

from __future__ import annotations

import re
from typing import Any

from aiops_diagnostics.consumer.consumer_time import readable_time
from aiops_diagnostics.health.rules import status_label

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


_SIMPLIFIED_WORDS = {
    "title": "订单状态",
    "order": "订单号",
    "time": "查询时间",
    "caveat": "这是本次查询到的业务状态，不是故障原因诊断。",
    "unknown_note": "这个状态码不在已知范围内，我无法确认它代表什么。",
    "technical": "（技术标识：status_code=",
    "source": "来源",
}

_TRADITIONAL_WORDS = {
    "title": "訂單狀態",
    "order": "訂單號",
    "time": "查詢時間",
    "caveat": "這是本次查詢到的業務狀態，不是故障原因診斷。",
    "unknown_note": "這個狀態碼不在已知範圍內，我無法確認它代表什麼。",
    "technical": "（技術標識：status_code=",
    "source": "來源",
}

#: 简体状态标签 → 繁体。只覆盖公司状态表里实际出现的词，不做通用转换：
#: 一个"看起来像繁体"的机械转换会改掉用户认得的术语。
_TRADITIONAL = {
    "充电中": "充電中",
    "充电结束": "充電結束",
    "未知": "未知",
}


def status_answer(snapshot: dict[str, Any], language: str, *, localized=False) -> dict[str, Any]:
    from aiops_diagnostics.consumer.consumer_language import COPY, live_answer

    if localized and language in COPY:
        return live_answer(snapshot, language, "status")
    label = "未知" if snapshot["status_role"] == "unknown" else status_label(snapshot["status_code"])
    if language == "zh-Hant":
        label = _TRADITIONAL.get(label, label)
    # 排版按**纯文本**来读：前端按 blocks[] 分派渲染，不解析 Markdown（frontend-api-brief
    # §9）。所以靠分段、缩进与短行区分信息层级，不靠 `**`／`#`。
    # 技术字段（status_code / status_role / 来源标识）留在括号里，不占正文第一句：
    # 用户要的是"这单怎么样了"，原始标识是给排查用的。
    when = readable_time(snapshot["observed_at"])
    if language in {"zh", "zh-Hant"}:
        traditional = language == "zh-Hant"
        words = _TRADITIONAL_WORDS if traditional else _SIMPLIFIED_WORDS
        unknown_note = ""
        if snapshot["status_role"] == "unknown":
            unknown_note = "\n\n" + words["unknown_note"]
        text = (
            f"{words['title']}：{label}\n\n"
            f"{words['order']}：{snapshot['order_no']}\n"
            f"{words['time']}：{when}\n\n"
            f"{words['caveat']}{unknown_note}\n"
            f"{words['technical']}{snapshot['status_code']}，"
            f"status_role={snapshot['status_role']}，{words['source']} {snapshot['source']}）"
        )
    else:
        text = (
            f"Order status: {snapshot['status_role'].replace('_', ' ')}\n\n"
            f"Order number: {snapshot['order_no']}\n"
            f"Checked at: {when}\n\n"
            "This is the business status observed just now, not a diagnosis of the cause.\n"
            f"(Technical: status code {snapshot['status_code']}, "
            f"status role {snapshot['status_role']}, source {snapshot['source']})"
        )
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}
