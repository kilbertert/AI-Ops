"""诊断未完成时交付本轮已授权证据，不使用被拒的模型正文。"""

import json
import re
from html import escape

from aiops_diagnostics.common.i18n import localized_pack
from aiops_diagnostics.common.redaction import sanitize_data
from aiops_diagnostics.operator.operator_agent import _SOURCE_LABELS, render

COPY = {
    "zh": {
        "notice": "诊断结论未完成。以下仅为本轮已取得的记录，不能据此认定充电正常、故障根因或收费正确。",
        "missing": "未提供",
        "orderNo": "订单号",
        "status": "订单状态原值",
        "createdTime": "创建时间记录",
        "stopTime": "停止时间记录",
        "stoppedReasonContent": "停止原因记录",
        "electricity": "充电量记录",
        "time": "时长记录（分钟）",
        "totalAmount": "订单费用记录",
        "payAmount": "支付金额记录",
        "refundAmount": "退款金额记录",
        "vatFee": "税费记录",
        "currency": "金额未附币种；字段原值不合计、不推算。",
        "raw": "来源记录摘录（字段保留原名；不属于诊断结论）",
        "computed": "核验工具已有输出（含工具计算，并非原始账单；本次呈现不重算）",
        "omitted": "部分记录因展示长度限制未列出；不能把摘录当完整过程。",
        "candidate": "通讯及事件仅为设备时间窗口候选，未关联本单交易；无记录不代表无故障，不可用不代表为空。",
    },
    "en": {
        "notice": (
            "Diagnosis is incomplete. These are retrieved records only; normal charging, "
            "fault causes and billing correctness have not been verified."
        ),
        "missing": "Not supplied",
        "orderNo": "Order number",
        "status": "Recorded order status code",
        "createdTime": "Recorded creation time",
        "stopTime": "Recorded stop time",
        "stoppedReasonContent": "Recorded stop reason",
        "electricity": "Recorded energy",
        "time": "Recorded duration (minutes)",
        "totalAmount": "Order fee record",
        "payAmount": "Payment amount record",
        "refundAmount": "Refund amount record",
        "vatFee": "Tax fee record",
        "currency": "Currency is not supplied. Values are not added or inferred.",
        "raw": "Source excerpts (original field names; not diagnostic conclusions)",
        "computed": (
            "Existing analysis-tool output (includes tool calculations, not raw billing; "
            "not recalculated here)"
        ),
        "omitted": (
            "Some records were omitted for display limits; excerpts do not represent the complete process."
        ),
        "candidate": (
            "Communication and event records are device-window candidates, not linked order transactions. "
            "Empty records do not prove absence of faults; unavailable does not mean empty."
        ),
    },
}


def render_evidence(evidence, language, *, secrets=()):
    if not evidence:
        return None
    words, sources = localized_pack(COPY, language), localized_pack(_SOURCE_LABELS, language)
    order = evidence.get("cpms_order_detail", {}).get("data", {}).get("orderNo")
    lines = [words["notice"]]
    omitted = False

    def bounded(value):
        nonlocal omitted
        if isinstance(value, list):
            omitted |= len(value) > 6
            return [bounded(item) for item in value[:6]]
        if isinstance(value, dict):
            return {key: bounded(item) for key, item in value.items()}
        if isinstance(value, str):
            for secret in secrets:
                if secret and len(secret) >= 4:
                    value = value.replace(secret, "REDACTED")
        return value

    for tool in sorted(evidence, key=lambda name: name != "cpms_order_detail"):
        item = evidence[tool]
        # 只交付执行层的 data 投影，不展示授权参数、原响应或离线权限引用。
        data = bounded(sanitize_data(item["data"], preserve=(order,) if order else ()))
        if tool == "cpms_order_detail":
            lines.append("\n" + sources[tool])
            for key in (
                "orderNo",
                "status",
                "createdTime",
                "stopTime",
                "stoppedReasonContent",
                "electricity",
                "time",
                "totalAmount",
                "payAmount",
                "refundAmount",
                "vatFee",
            ):
                value = data.get(key)
                value = words["missing"] if value is None else str(value)
                value = re.sub(r"([\\`*_{}\[\]()#!|>])", r"\\\1", escape(value))
                line = f"{words[key]}: {value.replace(chr(10), ' ').replace(chr(13), ' ')}"
                if sum(map(len, lines)) + len(line) > 8000:
                    omitted = True
                else:
                    lines.append(line)
            lines.append(words["currency"])
            continue
        excerpt = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        excerpt = excerpt.replace("`", "\\u0060").replace("<", "\\u003c")
        if sum(map(len, lines)) + len(excerpt) > 9500:
            omitted = True
            lines.append(f"\n{sources.get(tool, tool)}: {words['omitted']}")
            continue
        kind = (
            "computed"
            if tool in {"cpms_order_amount_check", "cpms_order_process_stats", "cpms_station_plan"}
            else "raw"
        )
        lines.extend(["\n" + sources.get(tool, tool), words[kind]])
        if tool == "cpms_order_communications":
            lines.append(words["candidate"])
        lines.append("```json\n" + excerpt + "\n```")
    if omitted:
        lines.append(words["omitted"])
    answer = "\n".join(lines)
    return render(answer, evidence, language)
