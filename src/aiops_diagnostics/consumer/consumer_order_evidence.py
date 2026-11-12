"""客服订单事实直接来自本轮授权记录；自由模型正文不参与拼装。"""

from aiops_diagnostics.common.i18n import localized_pack, rendering_language
from aiops_diagnostics.common.redaction import redact_text
from aiops_diagnostics.consumer.consumer_language import status_word
from aiops_diagnostics.consumer.consumer_plain import refund_text

LABELS = {
    "zh": {
        "order": "订单号",
        "order_amount": "订单费用记录",
        "recorded_payment_amount": "支付金额记录",
        "recorded_refund_amount": "退款金额记录",
        "refund_status": "退款状态记录",
        "source": "来源",
        "cpms_source": "充电订单记录",
        "mall_source": "商城账务记录",
        "period_energy": "分时展示电量（非独立计量）",
        "currency": "金额币种",
        "missing": "未提供或无效",
        "status": "充电订单状态记录",
        "observed_at": "查询时间",
        "electricity": "充电量（kWh）",
        "time": "时长记录（分钟）",
        "createdTime": "创建时间",
        "stopTime": "停止时间",
        "electricityFee": "电费",
        "serviceFee": "服务费",
        "launchFee": "启动费",
        "dsElectricFee": "电损电费",
        "dsServiceFee": "电损服务费",
        "parkFee": "停车费",
        "occupyFee": "占位费",
        "marketAmount": "优惠记录",
        "vat": "税率（%）",
        "vatFee": "税费",
        "period": "分时记录",
        "startTime": "开始",
        "endTime": "结束",
        "electricFee": "电费",
        "totalFee": "小计",
        "feeRate": "来源组合费率（单位未核实）",
        "type": "来源类型",
        "tax_rate": "税率（%）",
        "payment_price": "商城税项金额",
        "amount": "商城支付记录金额",
        "refund_amount": "商城支付退款记录金额",
        "child_refund": "商城子单退款记录金额",
        "tax_refund": "商城税项退款记录金额",
        "refund_unknown": "商城退款证据不完整，不能确认是否存在退款。",
        "omitted_count": "未展示分时行数",
        "pay_time": "支付记录时间",
        "state": "支付记录状态",
        "pay_1": "成功",
        "pay_other": "非成功或未核实",
        "created_time": "创建时间",
        "station_name": "站点",
        "limit": "以上为查询记录，无法确认充电过程是否异常，也未核验收费是否正确或银行扣款、到账。",
        "no_data": "本轮未取得可展示的订单记录，无法确认订单金额或状态。请确认订单后重新查询。",
        "no_status": "本轮未取得充电状态记录。",
        "currency_missing": "金额币种未提供或未能确认，以下金额不附币种单位。",
        "period_missing": "分时记录未提供或不完整。",
        "period_empty": "接口返回空分时记录。",
        "period_limit": "分时电量可能由费用反推，不能作为独立计量；不求和、不重算费用。",
        "tax_confirmed": "商城支付记录包含所列增值税项；这不证明金额差额等于税或不存在多扣款。",
        "tax_unknown": "现有商城记录不足以确认支付与税项关系。",
        "mall_missing": "本轮商城税项或支付证据未取得，不能据此确认税费关系。",
        "parent_conflict": "商城父单已付字段与成功支付记录不一致，本次未采用父单已付字段作为支付依据。",
        "omitted": "部分记录因展示长度限制未列出，请按具体订单或分项继续查询。",
        "knowledge": "相关资料原文摘录（不代表本单故障结论）",
        "quote_omitted": "（摘录，后续内容省略）",
        "support": "联系指引（未代为转接）",
        "quote_unavailable": "部分资料含无法安全展示的链接，本次未展示其摘录；已取得的订单记录不受影响。",
        "no_answer": "请说明需要查询的订单或具体问题，以便取得有依据的资料。",
    },
    "en": {
        "order": "Order number",
        "order_amount": "Order fee record",
        "recorded_payment_amount": "Payment amount record",
        "recorded_refund_amount": "Refund amount record",
        "refund_status": "Refund status record",
        "source": "Source",
        "cpms_source": "Charging order records",
        "mall_source": "Mall billing records",
        "period_energy": "Displayed period energy (not independent metering)",
        "currency": "Currency",
        "missing": "Not supplied or invalid",
        "status": "Charging order status record",
        "observed_at": "Observed at",
        "electricity": "Energy (kWh)",
        "time": "Recorded duration (minutes)",
        "createdTime": "Created at",
        "stopTime": "Stopped at",
        "electricityFee": "Electricity fee",
        "serviceFee": "Service fee",
        "launchFee": "Start fee",
        "dsElectricFee": "Electricity loss fee",
        "dsServiceFee": "Service loss fee",
        "parkFee": "Parking fee",
        "occupyFee": "Occupancy fee",
        "marketAmount": "Discount record",
        "vat": "Tax rate (%)",
        "vatFee": "Tax fee",
        "period": "Period record",
        "startTime": "Start",
        "endTime": "End",
        "electricFee": "Electricity fee",
        "totalFee": "Subtotal",
        "feeRate": "Source combined rate (unit unverified)",
        "type": "Source type",
        "tax_rate": "Tax rate (%)",
        "payment_price": "Mall tax item amount",
        "amount": "Mall payment record amount",
        "refund_amount": "Mall payment refund amount",
        "child_refund": "Mall child order refund amount",
        "tax_refund": "Mall tax item refund amount",
        "refund_unknown": "Mall refund evidence is incomplete; refund presence is unconfirmed.",
        "omitted_count": "Omitted period rows",
        "pay_time": "Payment record time",
        "state": "Payment record status",
        "pay_1": "Successful",
        "pay_other": "Not successful or unverified",
        "created_time": "Created at",
        "station_name": "Station",
        "limit": (
            "These are retrieved records. Charging faults, billing correctness and bank "
            "debits or receipts have not been verified."
        ),
        "no_data": (
            "No order records were retrieved for display in this turn. Confirm the order and try again."
        ),
        "no_status": "No charging status record was retrieved in this turn.",
        "currency_missing": "Currency is missing or unconfirmed; amounts below have no currency unit.",
        "period_missing": "Period records are missing or incomplete.",
        "period_empty": "The source returned no period rows.",
        "period_limit": (
            "Period energy may be derived from fees and is not independent metering. No "
            "totals or fees are recalculated."
        ),
        "tax_confirmed": (
            "The mall payment record includes the listed tax item. This does not establish "
            "the difference between amounts or rule out overcharging."
        ),
        "tax_unknown": "The mall records do not establish the relationship between payment and tax.",
        "mall_missing": (
            "Mall tax or payment evidence was not retrieved; the tax relationship cannot be confirmed."
        ),
        "parent_conflict": (
            "The mall parent paid field conflicts with the successful payment record and "
            "was excluded as payment evidence."
        ),
        "omitted": "Some records were omitted due to display length. Ask about a specific order or item.",
        "knowledge": "Relevant source excerpt (not a diagnosis of this order)",
        "quote_omitted": "(excerpt; remaining text omitted)",
        "support": "Contact information (no transfer made)",
        "quote_unavailable": (
            "A source excerpt could not be safely displayed. Retrieved order facts remain available."
        ),
        "no_answer": "Please specify an order or question so that relevant evidence can be retrieved.",
    },
}
FEES = (
    "electricityFee",
    "serviceFee",
    "launchFee",
    "dsElectricFee",
    "dsServiceFee",
    "parkFee",
    "occupyFee",
    "marketAmount",
    "vat",
    "vatFee",
)


def render(context, language):
    """固定句子与原值组合，单行超限整行省略，不截断金额。"""
    words = localized_pack(LABELS, language)
    language = rendering_language(LABELS, language)
    records = {}
    for call in (context or {}).get("calls", []):
        tool, result = call["tool"], call.get("result") or {}
        if tool not in {"orders", "status", "billing"}:
            continue
        key = (tool, call.get("arguments", {}).get("order_no", ""))
        if result.get("snapshot", {}).get("order_no"):
            key = (tool, result["snapshot"]["order_no"])
        records[key] = result
    lines = [words["limit"]]
    found = False

    def fields(row, keys):
        for key in keys:
            value = row.get(key)
            if value is None or key in row.get("invalid_fields", []):
                value = words["missing"]
            if key == "station_name":
                value = redact_text(str(value))
            lines.append(f"{words[key]}: {value}")

    for (tool, _), result in records.items():
        if result.get("status") != "found":
            continue
        if tool == "orders":
            for order in result.get("orders", []):
                found = True
                lines.append(f"{words['order']}: {order['order_no']}")
                fields(order, [k for k in ("created_time", "station_name") if k in order])
            continue
        snap = result.get("snapshot", {})
        if not snap.get("order_no"):
            continue
        if snap.get("source") != {"billing": "cpms.orderDetail", "status": "cpms.chargingInfo"}.get(tool):
            continue
        found = True
        lines.append(f"\n{words['order']}: {snap['order_no']}")
        lines.append(f"{words['source']}: {words['cpms_source']}")
        if tool == "status" and snap.get("source") == "cpms.chargingInfo":
            lines.append(f"{words['status']}: {status_word(language, snap.get('status_role', 'unknown'))}")
        elif tool == "billing" and snap.get("source") == "cpms.orderDetail":
            lines.append(
                f"{words['currency']}: {snap['currency']}"
                if snap.get("currency_status") == "provided" and snap.get("currency")
                else words["currency_missing"]
            )
            fields(snap, ("order_amount", "recorded_payment_amount", "recorded_refund_amount"))
            lines.append(f"{words['refund_status']}: {refund_text(language, snap.get('refund_status'))}")
            if snap.get("order_summary"):
                fields(snap["order_summary"], ("electricity", "time", "createdTime", "stopTime"))
            if snap.get("fee_components"):
                fields(snap["fee_components"], FEES)
            mall = snap.get("mall_payment", {})
            if mall.get("status") == "linked":
                lines.append(f"{words['source']}: {words['mall_source']}")
                for child in mall.get("orders", []):
                    fields({"child_refund": child.get("refund_amount")}, ("child_refund",))
                for tax in mall.get("tax_items", []):
                    fields(tax, ("tax_rate", "payment_price"))
                    fields({"tax_refund": tax.get("refund_amount")}, ("tax_refund",))
                if mall.get("refund_present") == "unknown":
                    lines.append(words["refund_unknown"])
                for pay in mall.get("payments", []):
                    label = "pay_1" if type(pay.get("state")) is int and pay["state"] == 1 else "pay_other"
                    lines.append(f"{words['state']}: {words[label]}")
                    fields(pay, ("amount", "refund_amount", "pay_time"))
                lines.append(
                    words["tax_confirmed"]
                    if mall.get("payment_includes_tax") == "confirmed"
                    else words["tax_unknown"]
                )
                if mall.get("parent_paid_consistency") == "conflicting":
                    lines.append(words["parent_conflict"])
            else:
                lines.append(words["mall_missing"])
            periods = snap.get("periods", {})
            if periods.get("omitted_count"):
                fields(periods, ("omitted_count",))
            for row in periods.get("records", []):
                lines.append(words["period"])
                fields(row, ("startTime", "endTime", "electricFee", "serviceFee", "totalFee", "feeRate"))
                fields({"period_energy": row.get("electricity")}, ("period_energy",))
            if periods.get("status") == "empty":
                lines.append(words["period_empty"])
            elif periods.get("status") != "provided":
                lines.append(words["period_missing"])
            if periods.get("records"):
                lines.append(words["period_limit"])
        fields(snap, ("observed_at",))
    if not found:
        lines.append(words["no_data"])
    elif not any(
        tool == "status"
        and result.get("status") == "found"
        and result.get("snapshot", {}).get("source") == "cpms.chargingInfo"
        for (tool, _), result in records.items()
    ):
        lines.append(words["no_status"])
    bounded = []
    size = 0
    for line in lines:
        if size + len(line) > 7400:
            bounded.append(words["omitted"])
            break
        bounded.append(line)
        size += len(line) + 1
    return "\n".join(bounded)
