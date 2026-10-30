"""现有聊天入口的本人状态查询；不调用模型或使用运维服务身份。"""

from __future__ import annotations

import re
from typing import Any

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


def status_answer(
    snapshot: dict[str, Any],
    language: str,
    *,
    localized=False,
    pack: dict[str, str] | None = None,
) -> dict[str, Any]:
    """本人这次查询到的业务状态。

    正文的措辞来自 `consumer_plain`：**先一句结论，再讲这句结论能拿去核对什么**。
    这里只决定「用哪条渲染路」——本地化语种走 JSON 模板，其余（zh/zh-Hant/en）走代码。
    """
    from aiops_diagnostics.consumer.consumer_language import COPY, live_answer
    from aiops_diagnostics.consumer.consumer_plain import status_text

    if localized and language in COPY:
        return live_answer(snapshot, language, "status", pack=pack)
    # 前端按 blocks[] 分派渲染、不解析 Markdown（frontend-api-brief §9），
    # 所以层级只能靠分段与短行表达。
    text = status_text(language, snapshot, pack)
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}
