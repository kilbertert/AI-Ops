"""本人订单账务记录；业务金额与退款标记不等同支付渠道回执。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    RequestSpec,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.data.charging_status import ChargingStatusError
from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

BILLING = re.compile(r"退款|支付|付款|账单|账务|refund|payment|billing", re.I)
QUERY = re.compile(r"查|状态|进度|记录|退了|到账|扣款|付了|status|progress|record|check|paid", re.I)
GUIDANCE = re.compile(r"怎么|如何|怎样|申请|规则|政策|how|apply|policy", re.I)


def is_billing_question(question):
    return bool(BILLING.search(question) and QUERY.search(question) and not GUIDANCE.search(question))


def _unavailable(_):
    return ChargingStatusError("billing records unavailable")


def _amount(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 32:
        raise ChargingStatusError("invalid billing amount")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ChargingStatusError("invalid billing amount") from exc
    if (
        not number.is_finite()
        or number < 0
        or number > Decimal("999999999999.99")
        or number.as_tuple().exponent < -8
    ):
        raise ChargingStatusError("invalid billing amount")
    return format(number, "f")


def read_billing_record(base_url, order_no, *, tenant_id, user_id):
    base = canonical_gateway_url(base_url)
    body = request_json(
        RequestSpec(
            url=f"{base}/chOrderInfo/getOrderInfoDetail/{quote(order_no, safe='')}",
            headers={"Accept": "application/json", "tenant-id": tenant_id, "user-id": user_id},
            timeout=10,
            max_read_bytes=131072,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=_unavailable,
            http_error=_unavailable,
            unavailable=_unavailable,
            invalid_body=_unavailable,
            invalid_envelope=_unavailable,
        ),
        envelope=parse_code_data_envelope,
    )
    row = body.get("chOrderInfoModel") if isinstance(body, dict) else None
    if (
        not isinstance(row, dict)
        or row.get("orderNo") != order_no
        or body.get("orderNo", order_no) != order_no
    ):
        raise ChargingStatusError("billing order does not match")
    refund = row.get("refundStatus")
    if refund is not None and (
        isinstance(refund, bool) or not isinstance(refund, (str, int)) or len(str(refund)) > 32
    ):
        raise ChargingStatusError("invalid refund status")
    return {
        "order_no": order_no,
        "order_amount": _amount(row.get("totalAmount")),
        "recorded_payment_amount": _amount(row.get("payAmount")),
        "refund_status": str(refund) if refund is not None else None,
        "recorded_refund_amount": _amount(row.get("refundAmount")),
        "observed_at": datetime.now(UTC).isoformat(),
        "source": "cpms.orderDetail",
    }


def billing_answer(snapshot, language):
    from aiops_diagnostics.consumer.consumer_language import COPY, live_answer

    if language in COPY:
        return live_answer(snapshot, language, "billing")
    zh = language.startswith("zh")
    missing = "未提供" if zh else "not supplied"
    status = (
        {"0": "未退款", "1": "全额退款记录", "2": "部分退款记录"}
        if zh
        else {
            "0": "no refund recorded",
            "1": "full refund recorded",
            "2": "partial refund recorded",
        }
    )
    refund = status.get(snapshot["refund_status"], "状态待核实" if zh else "status unverified")
    if zh:
        text = (
            f"订单 {snapshot['order_no']} 的账务记录：\n"
            f"订单金额：{snapshot['order_amount'] or missing}；"
            f"支付金额字段：{snapshot['recorded_payment_amount'] or missing}；\n"
            f"退款记录：{refund}；记录的退款金额：{snapshot['recorded_refund_amount'] or missing}。\n"
            "接口未提供币种，请与原订单页面核对。支付金额字段不证明已经扣款，退款记录不证明银行到账。"
            "本客服未发起付款或退款。\n"
            f"来源：{snapshot['source']}；查询时间（UTC）：{snapshot['observed_at']}。"
        )
    else:
        text = (
            f"Billing record for order {snapshot['order_no']}:\n"
            f"Order amount: {snapshot['order_amount'] or missing}; "
            f"payment amount field: {snapshot['recorded_payment_amount'] or missing};\n"
            f"Refund record: {refund}; "
            f"recorded refund amount: {snapshot['recorded_refund_amount'] or missing}.\n"
            "No currency was supplied; check the original order page. The payment amount does not prove "
            "a debit, and the refund record does not prove bank receipt. "
            "No payment or refund was initiated.\n"
            f"Source: {snapshot['source']}; observed at (UTC): {snapshot['observed_at']}."
        )
    return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "not_found"}
