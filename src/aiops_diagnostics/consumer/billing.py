"""本人订单账务记录与商城侧税项/支付证据投影。

合并原 ``consumer_billing``（CPMS 订单详情与账务答复）与 ``consumer_mall_billing``
（商城订单/税项/支付记录一致性核对）：两者共享金额解析规则与账务边界声明——
业务金额与退款标记不等同支付渠道回执，且只做原值投影与逐项相等核对，不推断税费关系。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    RequestSpec,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.common.endpoint_policy import canonical_gateway_url
from aiops_diagnostics.common.i18n import rendering_language
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.consumer.presentation import COPY, WORDS, billing_text, live_answer
from aiops_diagnostics.data.charging_status import ChargingStatusError
from aiops_diagnostics.data.sources import _format_time

BILLING = re.compile(r"退款|支付|付款|账单|账务|refund|payment|billing", re.I)
QUERY = re.compile(r"查|状态|进度|记录|退了|到账|扣款|付了|status|progress|record|check|paid", re.I)
GUIDANCE = re.compile(r"怎么|如何|怎样|申请|规则|政策|how|apply|policy", re.I)


def is_billing_question(question: str) -> bool:
    return bool(BILLING.search(question) and QUERY.search(question) and not GUIDANCE.search(question))


def _unavailable(_: object) -> ChargingStatusError:
    return ChargingStatusError("billing records unavailable")


def _strict_decimal_amount(value: Any) -> str | None:
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


def decode_billing_number(value: str) -> Decimal:
    """账务 JSON 的 ``parse_float`` 回调：将 Decimal 构造异常映射为领域不可用。"""
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ChargingStatusError("invalid billing number") from exc


def read_billing_record(
    base_url: str,
    order_no: str,
    *,
    tenant_id: str,
    user_id: str,
    include_composition: bool = True,
) -> dict[str, Any]:
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
    snapshot: dict[str, Any] = {
        "order_no": order_no,
        "order_amount": _strict_decimal_amount(row.get("totalAmount")),
        "recorded_payment_amount": _strict_decimal_amount(row.get("payAmount")),
        "refund_status": str(refund) if refund is not None else None,
        "recorded_refund_amount": _strict_decimal_amount(row.get("refundAmount")),
        "observed_at": format_stored_time(now_utc()),
        "source": "cpms.orderDetail",
    }
    if include_composition:
        snapshot.update(_composition(row, body.get("periodFeeDetail")))
    return snapshot


def _source_time(value: Any, *, period: bool = False) -> str | None:
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


def _fields(
    row: dict[str, Any],
    amounts: tuple[str, ...],
    texts: tuple[str, ...] = (),
    *,
    period: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for key in (*amounts, *texts):
        value = row.get(key)
        try:
            result[key] = (
                _source_time(value, period=period) if key in texts else _strict_decimal_amount(value)
            )
            if value is None:
                missing.append(key)
        except ChargingStatusError:
            result[key] = None
            invalid.append(key)
    return {**result, "missing_fields": missing, "invalid_fields": invalid}


def _composition(row: dict[str, Any], raw_periods: Any) -> dict[str, Any]:
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
    periods: dict[str, Any] = {"status": "missing", "records": [], "source_count": None, "omitted_count": 0}
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


def billing_answer(
    snapshot: dict[str, Any],
    language: str,
    *,
    localized: bool = True,
    pack: dict[str, str] | None = None,
) -> dict[str, Any]:
    """本人这张单的账务记录（整段模板或组合式人话渲染）。"""
    if localized and language in COPY:
        return live_answer(snapshot, language, "billing", pack=pack)
    language = rendering_language(WORDS, language)
    return {
        "blocks": [{"kind": "text", "text": billing_text(language, snapshot, pack)}],
        "retrieval_status": "not_found",
    }


# ---------------------------------------------------------------------------
# 商城侧订单/税项/支付记录证据投影（原 consumer_mall_billing）
# ---------------------------------------------------------------------------

#: 商城业务合同：增值税明细行的 sku_code（调查文档已核实）。
TAX_SKU_CODE = "9"

LIMITATIONS = [
    "商城父单paid_amount存在被旧值覆盖的实例，不作支付依据且不投影；支付结果以成功支付记录（state=1）为准。",
    "payment_includes_tax=confirmed仅当当前详情与单子单、单税项、唯一成功支付逐项一致且处于已核实的无优惠退款场景；其余只能并列来源值，不推断税费关系或差额成因。",
    "商城订单与支付是业务记录，不等于银行扣款或到账回执；status/state按来源值说明，不自行解释状态码。",
    "refund_amount大于0表示存在退款记录，按原值说明，不推算净收或到账；refund_present=unknown表示存在无法解析的退款值，不能报无退款。",
    "refund_present只统计本商城链上的退款记录，与充电订单refund_status、HTTP详情的退款字段是不同来源，口径不同可同时存在。",
    "confirmed只证明支付记录包含税项，不证明两数差额等于税、税前/税后完整口径、收费正确或没有多扣款。",
]


def _dec(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _money(value: Any) -> Decimal | None:
    """与 :func:`_strict_decimal_amount` 同一套金额校验，返回 Decimal 供相等核对。"""
    try:
        text = _strict_decimal_amount(value)
    except ChargingStatusError:
        return None
    return _dec(text)


def _mall_row(row: dict[str, Any], amounts: tuple[str, ...], texts: tuple[str, ...]) -> dict[str, Any]:
    """把一行来源记录投影成原值字段；缺项与无效值单独列出，不补零、不猜测。"""
    out: dict[str, Any] = {}
    missing, invalid = [], []
    for key in (*amounts, *texts):
        value = row.get(key)
        if value is None:
            out[key] = None
            missing.append(key)
            continue
        if key in amounts:
            try:
                out[key] = _strict_decimal_amount(value)
            except ChargingStatusError:
                out[key] = None
                invalid.append(key)
        elif isinstance(value, str) and len(value) <= 128:
            out[key] = value
        elif isinstance(value, (datetime, date)):
            out[key] = _format_time(value) if isinstance(value, datetime) else str(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
        else:
            out[key] = None
            invalid.append(key)
    out["missing_fields"] = missing
    out["invalid_fields"] = invalid
    return out


def _equal(*values: Decimal | None) -> str:
    if any(v is None for v in values):
        return "unknown"
    return "match" if len(set(values)) == 1 else "mismatch"


def _mall_status(reason: str, *, status: str = "unavailable") -> dict[str, Any]:
    return {
        "source": "mall.order_info+order_item+pay_record_info",
        "status": status,
        "reason": reason,
        "excluded_sources": [],
        "limitations": list(LIMITATIONS),
    }


def mall_payment_evidence(payload: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    """把 ``mall_billing_records`` 的原始行投影成账务快照的 ``mall_payment`` 段。"""
    if not payload:
        return _mall_status("read_failed")
    if payload.get("disabled"):
        return _mall_status("not_configured", status="disabled")
    charging = payload.get("charging")
    if charging is None:
        return _mall_status(
            "multiple_charging_rows" if payload.get("charging_rows", 0) > 1 else "scoped_order_missing",
            status="ambiguous" if payload.get("charging_rows", 0) > 1 else "unavailable",
        )
    if not charging.get("out_trade_no"):
        return _mall_status("no_out_trade_no", status="not_linked")
    parents = payload.get("parents") or []
    if not parents:
        return _mall_status("no_mall_parent", status="not_linked")
    if len(parents) > 1:
        return _mall_status("multiple_mall_parents", status="ambiguous")
    parent = parents[0]
    children = payload.get("orders") or []
    items = payload.get("items") or []
    payments = payload.get("payments") or []
    tax_items = [item for item in items if str(item.get("sku_code")) == TAX_SKU_CODE]

    tax_order_id = tax_items[0].get("order_id") if len(tax_items) == 1 else None
    base_child = next(
        (row for row in children if tax_order_id is not None and row.get("id") == tax_order_id),
        children[0] if len(children) == 1 else None,
    )
    parent_no = parent.get("order_no")
    linked_payments = [
        row for row in payments if parent_no and parent_no in (row.get("order_no"), row.get("out_trade_no"))
    ]
    unlinked_payment = len(linked_payments) != len(payments)
    payments = linked_payments
    successful = [row for row in payments if type(row.get("state")) is int and row["state"] == 1]
    payment = (
        successful[0] if len(successful) == 1 and "payments" not in payload.get("truncated", []) else None
    )

    tax_verdicts = [
        verdict
        for item in tax_items
        for verdict in (
            _equal(_money(charging.get("vat_fee")), _money(item.get("payment_price"))),
            _equal(_money(charging.get("vat")), _money(item.get("tax_rate"))),
        )
    ]
    components = snapshot.get("fee_components")
    if not isinstance(components, dict):
        components = {}
    displayed = [
        _equal(_money(snapshot.get(key)), _money(charging.get(column)))
        for key, column in (("order_amount", "total_amount"), ("recorded_payment_amount", "pay_amount"))
    ]
    displayed.extend(
        _equal(_money(components.get(key)), _money(charging.get(column)))
        for key, column in (("vat", "vat"), ("vatFee", "vat_fee"))
    )
    checks = {
        "displayed_snapshot": "mismatch"
        if "mismatch" in displayed
        else "unknown"
        if "unknown" in displayed
        else "match",
        "fee_base": _equal(
            _money(charging.get("total_amount")),
            _money(base_child.get("total_amount")) if base_child else None,
        ),
        "payment_amount": _equal(
            _money(charging.get("pay_amount")),
            _money(parent.get("payment_price")),
            _money(base_child.get("payment_price")) if base_child else None,
            _money(payment.get("amount")) if payment else None,
        ),
        "tax": (
            "match"
            if tax_verdicts and all(v == "match" for v in tax_verdicts)
            else "mismatch"
            if any(v == "mismatch" for v in tax_verdicts)
            else "unknown"
        ),
    }
    confirmed = (
        len(children) == len(tax_items) == len(payments) == 1
        and base_child is not None
        and str(base_child.get("order_type")) == "11"
        and base_child.get("order_no") == charging.get("order_no") == snapshot.get("order_no")
        and base_child.get("parent_id") == parent.get("id")
        and tax_items[0].get("order_id") == base_child.get("id")
        and parent.get("order_no") == charging.get("out_trade_no")
        and payment is not None
        and payment.get("scene") == "ORDER"
        and parent.get("order_no") in (payment.get("order_no"), payment.get("out_trade_no"))
        and charging.get("is_pay") == 1
        and charging.get("refund_status") == 0
        and snapshot.get("refund_status") == "0"
        and _money(snapshot.get("recorded_refund_amount")) == 0
        and all(_money(row.get("refund_amount")) == 0 for row in (*children, *items, *payments))
        and _money(payment.get("sub_amount")) == 0
        and all(_money(charging.get(key)) == 0 for key in ("market_amount", "reduce_balance"))
        and _money(charging.get("vat")) is not None
        and _money(charging["vat"]) <= 100
        and not unlinked_payment
        and not payload.get("truncated")
        and all(value == "match" for value in checks.values())
    )
    parent_paid = _money(parent.get("paid_amount"))
    missing = []
    if not children:
        missing.append("orders")
    if not tax_items:
        missing.append("tax_items")
    if not payments:
        missing.append("payments")
    if unlinked_payment:
        missing.append("unlinked_payment")
    if payload.get("truncated"):
        missing.append("truncated:" + ",".join(payload["truncated"]))
    return {
        "source": "mall.order_info+order_item+pay_record_info",
        "status": "linked",
        "observed_at": format_stored_time(now_utc()),
        "parent_order": _mall_row(
            parent,
            ("sales_price", "payment_price"),
            ("payment_time", "create_time"),
        ),
        "orders": [
            _mall_row(
                row,
                ("sales_price", "total_amount", "payment_price", "paid_amount", "refund_amount"),
                ("order_type", "payment_time", "create_time"),
            )
            for row in children
        ],
        "tax_items": [
            _mall_row(
                row,
                ("tax_rate", "sales_price", "payment_price", "refund_amount"),
                (),
            )
            for row in tax_items
        ],
        "payments": [
            _mall_row(
                row,
                ("amount", "sub_amount", "refund_amount"),
                ("state", "scene", "payment_type", "pay_time", "create_time"),
            )
            for row in payments
        ],
        "successful_payment_count": len(successful),
        "checks": checks,
        "payment_includes_tax": "confirmed" if confirmed else "unconfirmed",
        "parent_paid_consistency": (
            "unknown"
            if payment is None or parent_paid is None or _money(payment.get("amount")) is None
            else "consistent"
            if parent_paid == _money(payment.get("amount"))
            else "conflicting"
        ),
        "refund_present": (
            True
            if any(
                (amount := _money(row.get("refund_amount"))) is not None and amount > 0
                for row in (*children, *items, *payments)
            )
            else "unknown"
            if unlinked_payment
            or payload.get("truncated")
            or not children
            or not items
            or not payments
            or any(_money(row.get("refund_amount")) is None for row in (*children, *items, *payments))
            else False
        ),
        "excluded_sources": ["mall.order_info_parent.paid_amount"],
        "missing": missing,
        "limitations": list(LIMITATIONS),
    }
