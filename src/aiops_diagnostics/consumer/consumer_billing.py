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
    """本人这张单的账务记录。

    措辞来自 `consumer_plain`：先把「这单多少钱、付了多少、退没退」说成一句，
    再逐项解释术语。已有的免责语义逐字保留 —— 把「不证明已经扣款」简化成
    「不证明已到账」会让读者以为钱可能到了（上一版被测试拦下过一次）。
    """
    from aiops_diagnostics.consumer.consumer_language import COPY, live_answer
    from aiops_diagnostics.consumer.consumer_plain import billing_text

    if language in COPY:
        return live_answer(snapshot, language, "billing")
    return {
        "blocks": [{"kind": "text", "text": billing_text(language, snapshot)}],
        "retrieval_status": "not_found",
    }
