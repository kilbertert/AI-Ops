"""本人订单账务记录；业务金额与退款标记不等同支付渠道回执。"""

from __future__ import annotations

import re
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    RequestSpec,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
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
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 32:
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
        or number.as_tuple().exponent > 12
    ):
        raise ChargingStatusError("invalid billing amount")
    return format(number, "f")


def decode_billing_number(value):
    """账务JSON的parse_float回调：将Decimal构造异常映射为领域不可用。"""
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ChargingStatusError("invalid billing number") from exc


def read_billing_record(base_url, order_no, *, tenant_id, user_id, include_composition=True):
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
        parse_float=decode_billing_number,
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
    snapshot = {
        "order_no": order_no,
        "order_amount": _amount(row.get("totalAmount")),
        "recorded_payment_amount": _amount(row.get("payAmount")),
        "refund_status": str(refund) if refund is not None else None,
        "recorded_refund_amount": _amount(row.get("refundAmount")),
        "observed_at": format_stored_time(now_utc()),
        "source": "cpms.orderDetail",
    }
    if include_composition:
        snapshot.update(_composition(row, body.get("periodFeeDetail")))
    return snapshot


def _source_time(value, *, period=False):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ChargingStatusError("invalid billing time")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?", value):
            datetime.fromisoformat(value)
        elif period and re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
            if value not in {"24:00", "24:00:00"}:
                time.fromisoformat(value)
        else:
            raise ValueError("unsupported time format")
    except ValueError as exc:
        raise ChargingStatusError("invalid billing time") from exc
    return value


def _fields(row, amounts, texts=(), *, period=False):
    result, missing, invalid = {}, [], []
    for key in (*amounts, *texts):
        value = row.get(key)
        try:
            result[key] = _source_time(value, period=period) if key in texts else _amount(value)
            if value is None:
                missing.append(key)
        except ChargingStatusError:
            result[key] = None
            invalid.append(key)
    return {**result, "missing_fields": missing, "invalid_fields": invalid}


def _composition(row, raw_periods):
    summary = _fields(row, ("electricity", "time"), ("createdTime", "stopTime"))
    fees = _fields(
        row,
        (
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
        ),
    )
    if fees["vat"] is not None and Decimal(fees["vat"]) > 100:
        fees["vat"] = None
        fees["invalid_fields"].append("vat")
    currencies = [row[k] for k in ("currency", "currencyCode") if row.get(k) is not None]
    currency, currency_status = None, "missing"
    if currencies:
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Z]{3}", v) for v in currencies):
            currency_status = "invalid"
        elif len(set(currencies)) > 1:
            currency_status = "conflicting"
        else:
            currency, currency_status = currencies[0], "provided"
    periods = {"status": "missing", "records": [], "source_count": None, "omitted_count": 0}
    if isinstance(raw_periods, list):
        periods.update(source_count=len(raw_periods), status="provided" if raw_periods else "empty")
        periods["omitted_count"] = max(0, len(raw_periods) - 24)
        for item in raw_periods[:24]:
            if not isinstance(item, dict):
                periods["records"].append({"invalid_fields": ["record"]})
                continue
            record = _fields(
                item,
                ("electricFee", "serviceFee", "totalFee", "electricity", "feeRate"),
                ("startTime", "endTime"),
                period=True,
            )
            code = item.get("type")
            record["type"] = code if type(code) is int and 0 <= code <= 999999 else None
            if record["type"] is None:
                record["missing_fields" if code is None else "invalid_fields"].append("type")
            periods["records"].append(record)
        if periods["omitted_count"] or any(
            r.get("invalid_fields") or r.get("missing_fields") for r in periods["records"]
        ):
            periods["status"] = "partial"
    elif raw_periods is not None:
        periods["status"] = "invalid"
    return {
        "order_summary": summary,
        "fee_components": fees,
        "currency": currency,
        "currency_status": currency_status,
        "periods": periods,
        "limitations": [
            "null及missing_fields为上游未提供，invalid_fields为无效值；均不能当作0或未收费。",
            "electricity单位kWh，time单位分钟；createdTime为创建时间，stopTime为充电停止时间，时区按来源，不推断计费开始。",
            "分时electricity是接口展示电量，可能由费用反推，不是独立计量证据。",
            "feeRate为来源组合费率值；custom计费单位与构成未核定，不能称纯电价或无条件标每kWh。",
            "分时最多展示24行，omitted_count为未展示行数；缺行或缺项不能当完整明细。",
            "币种仅以currency为准；missing/invalid/conflicting均不能猜币种或换算，多币种不能合计。",
            "marketAmount是记录优惠；总额减支付不是优惠。分项不保证闭合，不推断差额原因或计费正确。",
        ],
    }


def billing_answer(snapshot, language, *, localized=True, pack: dict[str, str] | None = None):
    """本人这张单的账务记录。

    措辞来自 `consumer_plain`：先把「这单多少钱、付了多少、退没退」说成一句，
    再逐项解释术语。已有的免责语义逐字保留 —— 把「不证明已经扣款」简化成
    「不证明已到账」会让读者以为钱可能到了（上一版被测试拦下过一次）。
    """
    from aiops_diagnostics.common.i18n import rendering_language
    from aiops_diagnostics.consumer.consumer_language import COPY, live_answer
    from aiops_diagnostics.consumer.consumer_plain import WORDS, billing_text

    if localized and language in COPY:
        return live_answer(snapshot, language, "billing", pack=pack)
    # 组合面只能整句输出 `WORDS` 覆盖的语言，与 `status_answer` 同一条收口。
    language = rendering_language(WORDS, language)
    return {
        "blocks": [{"kind": "text", "text": billing_text(language, snapshot, pack)}],
        "retrieval_status": "not_found",
    }
