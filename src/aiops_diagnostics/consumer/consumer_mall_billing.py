"""商城侧订单/税项/支付记录证据投影：保留来源原值，不推断税费关系。

数据面由 ``data.sources.MySQLSource.mall_billing_records`` 在同一一致性快照内读出；
这里只做口径标注与逐项相等核对——核对是「读到相等」，不是把差额算成税。
调查依据：docs/validation/2026-11-12-consumer-mall-payment.md。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.consumer.consumer_billing import _amount
from aiops_diagnostics.data.charging_status import ChargingStatusError
from aiops_diagnostics.data.sources import _format_time

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
    """与 ``consumer_billing._amount`` 同一套金额校验，返回 Decimal 供相等核对。"""
    try:
        text = _amount(value)
    except ChargingStatusError:
        return None
    return _dec(text)


def _row(row: dict[str, Any], amounts: tuple[str, ...], texts: tuple[str, ...]) -> dict[str, Any]:
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
                out[key] = _amount(value)
            except ChargingStatusError:
                out[key] = None
                invalid.append(key)
        elif isinstance(value, str) and len(value) <= 128:
            out[key] = value
        elif isinstance(value, (datetime, date)):
            # 与 sources 同一投影：库内墙钟原值，不盖时区结论。
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


def _status(reason: str, *, status: str = "unavailable") -> dict[str, Any]:
    return {
        "source": "mall.order_info+order_item+pay_record_info",
        "status": status,
        "reason": reason,
        "excluded_sources": [],
        "limitations": list(LIMITATIONS),
    }


def mall_payment_evidence(payload: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    """把 ``mall_billing_records`` 的原始行投影成账务快照的 ``mall_payment`` 段。

    ``None`` / 行数不合 / 缺关联都如实标注，不返回半成品「看似正常」的证据；
    只有订单、税项、成功支付记录逐项一致时才报 ``payment_includes_tax=confirmed``。
    """
    if not payload:
        return _status("read_failed")
    if payload.get("disabled"):
        return _status("not_configured", status="disabled")
    charging = payload.get("charging")
    if charging is None:
        return _status(
            "multiple_charging_rows" if payload.get("charging_rows", 0) > 1 else "scoped_order_missing",
            status="ambiguous" if payload.get("charging_rows", 0) > 1 else "unavailable",
        )
    if not charging.get("out_trade_no"):
        return _status("no_out_trade_no", status="not_linked")
    parents = payload.get("parents") or []
    if not parents:
        return _status("no_mall_parent", status="not_linked")
    if len(parents) > 1:
        return _status("multiple_mall_parents", status="ambiguous")
    parent = parents[0]
    children = payload.get("orders") or []
    items = payload.get("items") or []
    payments = payload.get("payments") or []
    tax_items = [item for item in items if str(item.get("sku_code")) == TAX_SKU_CODE]

    # 税项归属的子单是费用基数的对照方；没有税项时只有唯一子单可当对照。
    tax_order_id = tax_items[0].get("order_id") if len(tax_items) == 1 else None
    base_child = next(
        (row for row in children if tax_order_id is not None and row.get("id") == tax_order_id),
        children[0] if len(children) == 1 else None,
    )
    # 与SQL同一归属：父单号必须非空，不把相似单号或空值当关联证据。
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
        # 充电订单费用记录 == 商城子单费用记录（同一口径的两个来源）。
        "fee_base": _equal(
            _money(charging.get("total_amount")),
            _money(base_child.get("total_amount")) if base_child else None,
        ),
        # 记录支付：充电 pay_amount、商城父/子单 payment_price、成功支付 amount 四者同值。
        "payment_amount": _equal(
            _money(charging.get("pay_amount")),
            _money(parent.get("payment_price")),
            _money(base_child.get("payment_price")) if base_child else None,
            _money(payment.get("amount")) if payment else None,
        ),
        # 税费：充电侧回写的 vat_fee/vat 与商城税项明细逐项一致（读到的相等，不是算出来的）。
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
        "parent_order": _row(
            parent,
            ("sales_price", "payment_price"),
            ("payment_time", "create_time"),
        ),
        "orders": [
            _row(
                row,
                ("sales_price", "total_amount", "payment_price", "paid_amount", "refund_amount"),
                ("order_type", "payment_time", "create_time"),
            )
            for row in children
        ],
        "tax_items": [
            _row(
                row,
                ("tax_rate", "sales_price", "payment_price", "refund_amount"),
                (),
            )
            for row in tax_items
        ],
        "payments": [
            _row(
                row,
                ("amount", "sub_amount", "refund_amount"),
                ("state", "scene", "payment_type", "pay_time", "create_time"),
            )
            for row in payments
        ],
        "successful_payment_count": len(successful),
        "checks": checks,
        "payment_includes_tax": "confirmed" if confirmed else "unconfirmed",
        # 父单 paid_amount 与成功支付记录的对照只报结论，不投影该字段值（可被旧值覆盖）。
        "parent_paid_consistency": (
            "unknown"
            if payment is None or parent_paid is None or _money(payment.get("amount")) is None
            else "consistent"
            if parent_paid == _money(payment.get("amount"))
            else "conflicting"
        ),
        # 税项也带 refund_amount；值存在但读不出时不能报「无退款」。
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
