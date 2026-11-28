"""管家端商城结算金额取证投影与 6 大规则确定性核验。

职责边界：
- 仅读取已授权场站订单对应的商城清分结算记录与店铺等级费率配置，严禁重算落库或发起写操作；
- 区分于核对车主应付账单的 ``order_billing.py``，本模块核对用户支付后平台与场站之间的
  结算金额、启动费服务费（含溢出扣减）、实付款一致性、支付通道费、平台总服务费与分佣基数；
- 全程使用 ``Decimal`` 与四舍五入到分（``ROUND_HALF_UP``），不得将账面算术一致说成银行已打款到账。
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from aiops_diagnostics.data.sources import SourceError, scoped_mysql_source
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.operator.operator_conversation import OperatorRefused
from aiops_diagnostics.operator.operator_tools import tool
from aiops_diagnostics.operator.order_analysis import number, shown
from aiops_diagnostics.operator.order_pricing import project

SETTLEMENT_TOOL = "cpms_order_settlement"
SETTLEMENT_CHECK_TOOL = "cpms_order_settlement_check"

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
_HUNDRED = Decimal("100")

_MODE_LABELS = {
    0: "电费单独结算",
    1: "整笔订单跟随",
    2: "电费跟随服务费结算",
    None: "待核对（子项分佣标记冲突）",
}


def _round2(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _dec0(value: Any) -> Decimal:
    parsed = number(value)
    return parsed if parsed is not None else _ZERO


def _dec_or_none(value: Any) -> Decimal | None:
    return number(value)


def _non_negative_decimal(row: dict[str, Any] | None, key: str) -> Decimal | None:
    if not isinstance(row, dict):
        return None
    value = number(row.get(key))
    return value if value is not None and value >= 0 else None


def _fmt2(value: Any) -> str:
    return shown(_dec0(value), 2)


def _fmt_rate(value: Any) -> str | None:
    parsed = number(value)
    if parsed is None:
        return None
    if parsed == 0:
        return "0"
    text = format(parsed.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _is_commission_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip() == "1"


def _parse_json_object(raw: Any, *, max_len: int = 8192) -> tuple[dict[str, Any] | None, bool]:
    """解析 JSON 对象字符串；空值返回 (None, False)，非法内容返回 (None, True)。"""
    if raw is None:
        return None, False
    if isinstance(raw, dict):
        return raw, False
    if not isinstance(raw, str):
        return None, True
    text = raw.strip()
    if not text:
        return None, False
    if len(text) > max_len:
        return None, True
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None, True
    if not isinstance(parsed, dict):
        return None, True
    return parsed, False


def _item_category(item: dict[str, Any]) -> str:
    sku_code = str(item.get("sku_code") or "").strip()
    spu_name = str(item.get("spu_name") or "")
    if sku_code == "2" or "电费" in spu_name:
        return "electric"
    if sku_code == "1" or "服务费" in spu_name:
        return "service"
    if sku_code == "4" or "插枪费" in spu_name or "启动费" in spu_name:
        return "start"
    if sku_code == "9" or "增值税" in spu_name:
        return "tax"
    if sku_code == "3" or "停车费" in spu_name:
        return "parking"
    if "占枪费" in spu_name:
        return "gun"
    return "other"


def _resolve_settlement_mode(
    active_items: list[dict[str, Any]],
    site_settlement_type: int | None,
    order_settlement_type: int | None,
) -> dict[str, Any]:
    configured = order_settlement_type if order_settlement_type is not None else site_settlement_type
    elec_items = [item for item in active_items if item["category"] == "electric"]
    tail_items = [
        item
        for item in active_items
        if item["category"] in {"start", "tax"}
        and max(_dec0(item.get("paymentPrice")), _dec0(item.get("salesPrice"))) > 0
    ]
    effective_mode: int | None
    mode_source = "order_item.is_commission"
    mode_conflict = False

    if elec_items:
        elec_flags = {bool(item["isCommission"]) for item in elec_items}
        if len(elec_flags) > 1:
            effective_mode = None
            mode_conflict = True
        elif elec_flags == {True}:
            if tail_items:
                tail_flags = {bool(item["isCommission"]) for item in tail_items}
                if tail_flags == {True}:
                    effective_mode = 1
                elif tail_flags == {False}:
                    effective_mode = 2
                else:
                    effective_mode = None
                    mode_conflict = True
            else:
                effective_mode = 1 if configured == 1 else 2
            if order_settlement_type == 0:
                mode_conflict = True
        else:
            if any(bool(item["isCommission"]) for item in tail_items):
                effective_mode = None
                mode_conflict = True
            else:
                effective_mode = 0
                if order_settlement_type == 1:
                    mode_conflict = True
    else:
        if tail_items:
            tail_flags = {bool(item["isCommission"]) for item in tail_items}
            if tail_flags == {True}:
                effective_mode = 1
            elif tail_flags == {False}:
                effective_mode = 0
            else:
                effective_mode = None
                mode_conflict = True
        else:
            effective_mode = 1 if configured == 1 else 0
            mode_source = "order_or_site_config"

    return {
        "effective_mode": effective_mode,
        "label": _MODE_LABELS[effective_mode],
        "site_settlement_type": site_settlement_type,
        "order_settlement_type": order_settlement_type,
        "mode_source": mode_source,
        "mode_conflict": mode_conflict,
    }


def _project_fee_config(shop: dict[str, Any] | None, shop_level: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(shop, dict) or not isinstance(shop_level, dict):
        return {
            "levelName": None,
            "settleFee": None,
            "rateOfSettleFeeForElect": None,
            "settleFeeOfElect": None,
            "electricityFee": None,
            "serviceFee": None,
            "insertGunFee": None,
            "parkFee": None,
            "anXinFee": None,
            "appointmentFee": None,
            "placeFee": None,
            "electricityLossRatio": None,
            "missing": True,
        }
    level_cfg, level_bad = _parse_json_object(shop_level.get("config"))
    shop_cfg, shop_bad = _parse_json_object(shop.get("configs"))
    settle_fee = _dec_or_none(shop_level.get("settle_fee"))
    rate_elect = _dec_or_none(shop_level.get("rate_of_settle_fee_for_elect"))
    per_kwh = _dec_or_none(shop_level.get("settle_fee_of_elect"))
    cfg = level_cfg or {}
    service_fee_rate = _dec_or_none(cfg.get("serviceFee"))
    if service_fee_rate is None:
        service_fee_rate = settle_fee if settle_fee is not None else _ZERO
    return {
        "levelName": str(shop_level.get("level_name") or "") or None,
        "settleFee": _fmt_rate(settle_fee if settle_fee is not None else _ZERO),
        "rateOfSettleFeeForElect": _fmt_rate(rate_elect if rate_elect is not None else _ZERO),
        "settleFeeOfElect": _fmt_rate(per_kwh if per_kwh is not None else _ZERO),
        "electricityFee": _fmt_rate(_dec_or_none(cfg.get("electricityFee")) or _ZERO),
        "serviceFee": _fmt_rate(service_fee_rate),
        "insertGunFee": _fmt_rate(_dec_or_none(cfg.get("insertGunFee")) or _ZERO),
        "parkFee": _fmt_rate(_dec_or_none(cfg.get("parkFee")) or _ZERO),
        "anXinFee": _fmt_rate(_dec_or_none(cfg.get("anXinFee")) or _ZERO),
        "appointmentFee": _fmt_rate(_dec_or_none(cfg.get("appointmentFee")) or _ZERO),
        "placeFee": _fmt_rate(_dec_or_none(cfg.get("placeFee")) or _ZERO),
        "electricityLossRatio": _fmt_rate(_dec_or_none((shop_cfg or {}).get("electricityLossRatio"))),
        "missing": bool(level_bad or shop_bad),
    }


def _not_synced_reason(charging: dict[str, Any]) -> str:
    is_pay = _optional_int(charging.get("is_pay"))
    pay_amount = _dec_or_none(charging.get("pay_amount"))
    if is_pay is not None and is_pay != 1:
        return "充电订单尚未完成支付，商城未生成结算单"
    if pay_amount is not None and pay_amount == 0:
        return "充电订单实付为 0，未生成商城清分结算单"
    return "未查询到对应的商城新能源结算单（可能为线下卡/套餐/白名单免结算单或尚未同步）"


def project_settlement(records: dict[str, Any]) -> dict[str, Any]:
    """将 MySQL 一致性快照行投影为脱敏的商城结算证据对象。"""
    with localcontext() as ctx:
        ctx.prec = 64
        charging = records["charging"]
        site = records.get("site") or {}
        mall_orders = records.get("mall_orders") or []
        mall_items = records.get("mall_items") or []
        site_type = _optional_int(site.get("settlement_type"))
        order_type = _optional_int(charging.get("settlement_type"))
        fee_config = _project_fee_config(records.get("shop"), records.get("shop_level"))
        truncated = bool(records.get("truncated"))

        if not mall_orders:
            return {
                "orderNo": str(charging.get("order_no") or ""),
                "siteId": str(charging.get("site_id") or ""),
                "settlement_state": "not_synced",
                "state_reason": _not_synced_reason(charging),
                "settlement_mode": {
                    "effective_mode": None,
                    "label": "未生成商城结算单",
                    "site_settlement_type": site_type,
                    "order_settlement_type": order_type,
                    "mode_source": "order_or_site_config",
                    "mode_conflict": False,
                },
                "settlement": None,
                "fee_config": fee_config,
                "items": [],
                "truncated": truncated,
            }

        mall_order = mall_orders[0]
        is_paid = str(mall_order.get("is_pay") or "").strip() == "1"
        settlement_state = "ready" if is_paid else "unpaid_mall_order"
        state_reason = "" if is_paid else "商城订单尚未标记已支付，清分结果未最终落定"

        projected_items: list[dict[str, Any]] = []
        active_items: list[dict[str, Any]] = []

        elec_amt = elec_srv = elec_rop = _ZERO
        srv_amt = srv_pay = srv_srv = srv_rop = _ZERO
        start_amt = start_srv = start_rop = _ZERO
        tax_amt = tax_rop = _ZERO
        park_amt = park_srv = _ZERO
        gun_amt = gun_srv = _ZERO
        other_amt = other_srv = _ZERO

        for raw in mall_items:
            category = _item_category(raw)
            status = str(raw.get("status") if raw.get("status") is not None else "0").strip()
            qty_parsed = _dec_or_none(raw.get("quantity"))
            qty = qty_parsed if qty_parsed is not None and qty_parsed > 0 else Decimal("1")
            raw_comm = raw.get("commission_flag")
            if raw_comm is None:
                raw_comm = raw.get("is_commission")
            is_comm = _is_commission_flag(raw_comm)
            projected = {
                "skuCode": str(raw.get("sku_code") or ""),
                "category": category,
                "status": status,
                "quantity": _fmt_rate(qty),
                "isCommission": is_comm,
                "salesPrice": _fmt2(raw.get("sales_price")),
                "paymentPrice": _fmt2(raw.get("payment_price")),
                "serviceFee": _fmt2(raw.get("service_fee")),
                "rateOfPayment": _fmt2(raw.get("rate_of_payment")),
                "commissionBaseAmount": _fmt2(raw.get("commission_base_amount")),
                "settlementAmount": _fmt2(raw.get("settlement_amount")),
            }
            projected_items.append(projected)
            if status != "0":
                continue
            active_items.append(projected)
            pay_price = _dec0(raw.get("payment_price"))
            item_pay = pay_price * qty
            item_gross = (
                item_pay + _dec0(raw.get("payment_coupon_price")) + _dec0(raw.get("platform_discount_amount"))
            )
            item_srv = _dec0(raw.get("service_fee")) * qty
            item_rop = _dec0(raw.get("rate_of_payment")) * qty
            if category == "electric":
                elec_amt += item_gross
                elec_srv += item_srv
                elec_rop += item_rop
            elif category == "service":
                srv_amt += item_gross
                srv_pay += item_pay
                srv_srv += item_srv
                srv_rop += item_rop
            elif category == "start":
                start_amt += item_gross
                start_srv += item_srv
                start_rop += item_rop
            elif category == "tax":
                tax_amt += item_pay
                tax_rop += item_rop
            elif category == "parking":
                park_amt += item_gross
                park_srv += item_srv
            elif category == "gun":
                gun_amt += item_gross
                gun_srv += item_srv
            else:
                other_amt += item_gross
                other_srv += item_srv

        settlement_mode = _resolve_settlement_mode(active_items, site_type, order_type)
        platform_discount = _dec0(mall_order.get("platform_discount_amount")) + _dec0(
            mall_order.get("platform_balance_reduce")
        )
        shop_discount = (
            _dec0(mall_order.get("payment_coupon_price"))
            + _dec0(mall_order.get("market_price"))
            + _dec0(mall_order.get("shop_balance_reduce"))
        )
        settlement_view = {
            "mallOrderStatus": str(mall_order.get("status") or ""),
            "settlementStatus": str(mall_order.get("settlement_status") or "")
            if mall_order.get("settlement_status") is not None
            else None,
            "paymentType": str(mall_order.get("payment_type") or "")
            if mall_order.get("payment_type") is not None
            else None,
            "orderAmount": _fmt2(mall_order.get("total_amount")),
            "platformDiscountAmount": _fmt2(platform_discount),
            "shopDiscountAmount": _fmt2(shop_discount),
            "refundAmount": _fmt2(mall_order.get("refund_amount")),
            "paymentAmount": _fmt2(mall_order.get("payment_price")),
            "rateOfPayment": _fmt_rate(_dec_or_none(mall_order.get("rate_of_payment")) or _ZERO),
            "feeOfPayChannel": _fmt2(mall_order.get("fee_of_pay_channel")),
            "orderAmountService": _fmt2(mall_order.get("service_fee")),
            "commissionBaseAmount": _fmt2(mall_order.get("commission_base_amount")),
            "actualCommission": _fmt2(mall_order.get("actual_commission")),
            "electricLoseAmount": _fmt2(mall_order.get("sliver_deduct_amount")),
            "settlementAmount": _fmt2(mall_order.get("settlement_amount")),
            "electricChargeAmount": _fmt2(elec_amt),
            "electricChargeServiceFee": _fmt2(elec_srv),
            "chargingRateOfPayment": _fmt2(elec_rop),
            "serviceAmount": _fmt2(srv_amt),
            "servicePaymentAmount": _fmt2(srv_pay),
            "serviceItemServiceFee": _fmt2(srv_srv),
            "serviceRateOfPayment": _fmt2(srv_rop),
            "startAmount": _fmt2(start_amt),
            "startAmountService": _fmt2(start_srv),
            "startRateOfPayment": _fmt2(start_rop),
            "addTaxAmount": _fmt2(tax_amt),
            "addTaxRateOfPayment": _fmt2(tax_rop),
            "parkingFeesAmount": _fmt2(park_amt),
            "parkingFeesAmountServiceFee": _fmt2(park_srv),
            "gunAmount": _fmt2(gun_amt),
            "gunAmountServiceFee": _fmt2(gun_srv),
            "otherAmount": _fmt2(other_amt),
            "otherAmountService": _fmt2(other_srv),
        }

        return {
            "orderNo": str(charging.get("order_no") or ""),
            "siteId": str(charging.get("site_id") or ""),
            "settlement_state": settlement_state,
            "state_reason": state_reason,
            "settlement_mode": settlement_mode,
            "settlement": settlement_view,
            "fee_config": fee_config,
            "items": projected_items,
            "truncated": truncated,
        }


def query_settlement(service: Any, task: Any, run: Any) -> dict[str, Any]:
    """在当前授权范围内从只读快照查询商城结算单并做白名单投影。"""
    caller = service.authorize_execution(run.run_id, tool=SETTLEMENT_TOOL)[0].caller
    scope = resolve_query_scope(caller)
    try:
        with scoped_mysql_source(service.context.runtime.diagnostic_settings, scope) as source:
            records = source.operator_settlement_records(run.order_no, expected_site_id=task.order_site_id)
    except (SourceError, ValueError) as exc:
        raise OperatorRefused("OPERATOR_SETTLEMENT_UNAVAILABLE") from exc
    service.authorize_execution(run.run_id, tool=SETTLEMENT_TOOL)
    if records is None:
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    if records.get("disabled"):
        raise OperatorRefused("OPERATOR_SETTLEMENT_UNAVAILABLE")
    if records.get("charging_rows", 0) == 0 or records.get("charging") is None:
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    if records.get("charging_rows", 0) > 1 or records.get("mall_order_rows", 0) > 1:
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    raw = project_settlement(records)
    return project(raw, tool(SETTLEMENT_TOOL).evidence_shape)


def _check(
    label: str,
    formula: str,
    expected: Decimal | None,
    recorded: Decimal | None,
    reason: str = "",
) -> dict[str, Any]:
    calc = _round2(expected) if expected is not None else None
    rec = _round2(recorded) if recorded is not None else None
    difference = rec - calc if calc is not None and rec is not None else None
    status = "待验证" if difference is None else "通过" if difference == 0 else "不一致"
    return {
        "label": label,
        "formula": formula,
        "calculated": calc,
        "recorded": rec,
        "difference": difference,
        "status": status,
        "reason": reason,
    }


def _mark_unverified(check: dict[str, Any], *reasons: str) -> None:
    check["status"] = "待验证"
    merged = [item for item in (check.get("reason", ""), *reasons) if item]
    check["reason"] = "；".join(dict.fromkeys(merged))


def verify(detail: dict[str, Any], settlement_data: dict[str, Any]) -> dict[str, Any]:
    """对已取证的商城结算单执行 6 类确定性清分规则核验。"""
    with localcontext() as context:
        context.prec = 64
        return _verify(detail, settlement_data)


def _verify(detail: dict[str, Any], settlement_data: dict[str, Any]) -> dict[str, Any]:
    mode_info = dict(settlement_data.get("settlement_mode") or {})
    mode_summary = {
        "effective_mode": mode_info.get("effective_mode"),
        "label": mode_info.get("label"),
        "site_settlement_type": mode_info.get("site_settlement_type"),
        "order_settlement_type": mode_info.get("order_settlement_type"),
        "mode_conflict": bool(mode_info.get("mode_conflict")),
    }
    base_limitations = [
        "平台抽成比例取自当前店铺等级配置（shop_level），商城未持久化订单结算时刻的等级费率历史快照；若结算后改过等级费率，重算值可能与落库值存在新旧配置差异",
        "各项核验仅覆盖商城结算单账面清分算术关系，不证明充电计量、实际银行打款到账或点位奖最终入账",
    ]

    state = str(settlement_data.get("settlement_state") or "")
    settlement = settlement_data.get("settlement")
    if state != "ready" or not isinstance(settlement, dict):
        reason = str(settlement_data.get("state_reason") or "商城结算单尚未就绪，暂不适用清分核验")
        placeholder_rules = (
            ("规则1 订单结算金额", "需已支付商城结算单"),
            ("规则2 启动费服务费", "需已支付商城结算单"),
            ("规则3-1 结算单实付款组成", "需已支付商城结算单"),
            ("规则3-2 充电单与结算单实付款一致", "需已支付商城结算单"),
            ("规则4 支付通道费分项与合计", "需已支付商城结算单"),
            ("规则5 平台服务费", "需已支付商城结算单"),
            ("规则6 订单分佣基数", "需已支付商城结算单"),
        )
        checks = [_check(label, formula, None, None, reason) for label, formula in placeholder_rules]
        return {
            "checks": checks,
            "settlement_mode": mode_summary,
            "limitations": [reason, *base_limitations],
            "conclusion": "该订单尚未生成已支付的商城结算单，不适用结算清分核验",
        }

    fee_cfg = settlement_data.get("fee_config") or {}
    config_missing = bool(fee_cfg.get("missing"))
    truncated = bool(settlement_data.get("truncated"))

    # 结算单字段提取
    electric = _dec0(settlement.get("electricChargeAmount"))
    start = _dec0(settlement.get("startAmount"))
    add_tax = _dec0(settlement.get("addTaxAmount"))
    charging_rop = _dec0(settlement.get("chargingRateOfPayment"))
    service_rop = _dec0(settlement.get("serviceRateOfPayment"))
    start_rop = _dec0(settlement.get("startRateOfPayment"))
    tax_rop = _dec0(settlement.get("addTaxRateOfPayment"))
    recorded_settlement = _dec0(settlement.get("settlementAmount"))
    recorded_start_srv = _dec0(settlement.get("startAmountService"))
    recorded_commission_base = _dec0(settlement.get("commissionBaseAmount"))
    electric_lose = _dec0(settlement.get("electricLoseAmount"))
    order_amount = _dec0(settlement.get("orderAmount"))
    platform_discount = _dec0(settlement.get("platformDiscountAmount"))
    shop_discount = _dec0(settlement.get("shopDiscountAmount"))
    refund_amount = _dec0(settlement.get("refundAmount"))
    payment_amount = _dec0(settlement.get("paymentAmount"))
    rate_of_payment = _dec0(settlement.get("rateOfPayment"))
    fee_of_pay_channel = _dec0(settlement.get("feeOfPayChannel"))
    order_amount_service = _dec0(settlement.get("orderAmountService"))
    parking_amount = _dec0(settlement.get("parkingFeesAmount"))
    gun_amount = _dec0(settlement.get("gunAmount"))
    other_amount = _dec0(settlement.get("otherAmount"))

    # 费率配置提取
    insert_gun_rate = _dec0(fee_cfg.get("insertGunFee"))
    elec_rate = _dec0(fee_cfg.get("electricityFee"))
    service_rate = _dec0(fee_cfg.get("serviceFee"))
    order_rate = _dec0(fee_cfg.get("rateOfSettleFeeForElect"))
    per_kwh_fee = _dec0(fee_cfg.get("settleFeeOfElect"))

    # 充电订单侧字段
    charge_pay = _non_negative_decimal(detail, "payAmount")
    charge_service = _non_negative_decimal(detail, "serviceFee")
    charge_ds_service = _non_negative_decimal(detail, "dsServiceFee") or _ZERO
    charge_market = _non_negative_decimal(detail, "marketAmount") or _ZERO
    first_service_fee = (
        charge_service + charge_ds_service
        if charge_service is not None
        else _dec0(settlement.get("serviceAmount"))
    )

    mode = mode_summary["effective_mode"]
    mode_conflict = mode_summary["mode_conflict"]
    has_refund = refund_amount > 0
    per_kwh_mode = per_kwh_fee > 0
    extra_fee_names = [
        name
        for name, amount in (
            ("停车费", parking_amount),
            ("占枪费", gun_amount),
            ("其他附加科目", other_amount),
        )
        if amount > 0
    ]

    # 规则2：启动费服务费（含通道费溢出调整；被规则5与规则6依赖，先算）
    raw_start_srv = _round2(start * insert_gun_rate / _HUNDRED)
    start_overflow = start - (raw_start_srv + start_rop)
    if start_overflow < 0:
        expected_start_srv = max(_ZERO, _round2(raw_start_srv + start_overflow))
        rule2_formula = (
            f"启动费 {shown(start)} × {_fmt_rate(insert_gun_rate)}% = {shown(raw_start_srv)}，"
            f"溢出调整 {shown(start)} - ({shown(raw_start_srv)} + {shown(start_rop)}) = "
            f"{shown(start_overflow)} < 0，调整后 = {shown(expected_start_srv)}"
        )
    else:
        expected_start_srv = raw_start_srv
        rule2_formula = f"启动费 {shown(start)} × {_fmt_rate(insert_gun_rate)}% = {shown(expected_start_srv)}"
    rule2_reason = ""
    if (
        start_overflow < 0
        and recorded_start_srv != expected_start_srv
        and recorded_start_srv == raw_start_srv
    ):
        rule2_reason = (
            "落库值未扣减通道费溢出，疑似商城结算溢出判断未换算通道费率百分比"
            "（百分数抽成比例与小数支付费率直接相加后比较100）"
        )
    check_rule2 = _check(
        "规则2 启动费服务费",
        rule2_formula,
        None if config_missing else expected_start_srv,
        recorded_start_srv,
        rule2_reason,
    )

    # 规则1：订单结算金额
    rule1_expected: Decimal | None
    if config_missing or mode is None:
        rule1_expected = None
        rule1_formula = "需有效的结算模式与店铺等级抽成配置"
    elif mode == 1:
        rule1_expected = _ZERO
        rule1_formula = "结算模式=1（整笔订单跟随分佣），场站直接结算金额 = 0.00"
    elif mode == 2:
        raw_r1 = start + add_tax - start_rop - tax_rop - recorded_start_srv
        rule1_expected = max(_ZERO, raw_r1)
        rule1_formula = (
            f"结算模式=2（电费跟随服务费）：{shown(start)}(启动费) + {shown(add_tax)}(增值税) "
            f"- {shown(start_rop)}(启动渠道费) - {shown(tax_rop)}(增值税渠道费) "
            f"- {shown(recorded_start_srv)}(启动费服务费) = {shown(raw_r1)}"
        )
        if raw_r1 < 0:
            rule1_formula += " → 小于0取0.00"
    else:
        raw_r1 = electric + start + add_tax - charging_rop - start_rop - tax_rop - recorded_start_srv
        rule1_formula = (
            f"结算模式=0（电费单独结算）：{shown(electric)}(电费) + {shown(start)}(启动费) "
            f"+ {shown(add_tax)}(增值税) - {shown(charging_rop)}(电费渠道费) "
            f"- {shown(start_rop)}(启动渠道费) - {shown(tax_rop)}(增值税渠道费) "
            f"- {shown(recorded_start_srv)}(启动费服务费)"
        )
        if elec_rate != 0:
            active_elec_rate = elec_rate if order_rate == 0 else order_rate
            r1_elec_cut = _round2(electric * active_elec_rate / _HUNDRED)
            raw_r1 -= r1_elec_cut
            rule1_formula += f" - {shown(r1_elec_cut)}(电费抽成)"
        rule1_formula += f" = {shown(raw_r1)}"
        rule1_expected = max(_ZERO, raw_r1)
        if raw_r1 < 0:
            rule1_formula += " → 小于0取0.00"
    check_rule1 = _check("规则1 订单结算金额", rule1_formula, rule1_expected, recorded_settlement)

    # 规则3：结算单实付款组成 & 与充电订单实付款一致
    expected_r3_formula = order_amount - platform_discount - shop_discount + add_tax - refund_amount
    check_rule3_1 = _check(
        "规则3-1 结算单实付款组成",
        f"{shown(order_amount)}(订单金额) - {shown(platform_discount)}(平台优惠) "
        f"- {shown(shop_discount)}(店铺优惠) + {shown(add_tax)}(增值税) "
        f"- {shown(refund_amount)}(退款) = {shown(expected_r3_formula)}",
        expected_r3_formula,
        payment_amount,
    )
    check_rule3_2 = _check(
        "规则3-2 充电单与结算单实付款一致",
        f"充电订单实付款 {shown(charge_pay) if charge_pay is not None else '未提供'} = 商城结算单实付款",
        charge_pay,
        payment_amount,
        "订单详情缺少 payAmount" if charge_pay is None else "",
    )

    # 规则4：支付通道费分项与合计
    rate_pct = _fmt_rate(rate_of_payment * _HUNDRED)
    calc_elec_rop = _round2(electric * rate_of_payment)
    calc_start_rop = _round2(start * rate_of_payment)
    calc_tax_rop = _round2(add_tax * rate_of_payment)
    calc_channel_sum = charging_rop + service_rop + start_rop + tax_rop
    check_rule4_1 = _check(
        "规则4-1 电费支付渠道费",
        f"{shown(electric)} × {rate_pct}% = {shown(calc_elec_rop)}",
        calc_elec_rop,
        charging_rop,
    )
    check_rule4_2 = _check(
        "规则4-2 启动费支付渠道费",
        f"{shown(start)} × {rate_pct}% = {shown(calc_start_rop)}",
        calc_start_rop,
        start_rop,
    )
    check_rule4_3 = _check(
        "规则4-3 增值税支付渠道费",
        f"{shown(add_tax)} × {rate_pct}% = {shown(calc_tax_rop)}",
        calc_tax_rop,
        tax_rop,
    )
    check_rule4_4 = _check(
        "规则4-4 支付通道费合计",
        f"{shown(charging_rop)}(电费) + {shown(service_rop)}(服务费) "
        f"+ {shown(start_rop)}(启动费) + {shown(tax_rop)}(增值税) = {shown(calc_channel_sum)}",
        calc_channel_sum,
        fee_of_pay_channel,
    )

    # 规则5：平台总服务费
    service_fee_diff = max(_ZERO, first_service_fee - charge_market)
    if order_rate == 0:
        elec_fee = _round2(electric * elec_rate / _HUNDRED)
        serv_fee = _round2(service_fee_diff * service_rate / _HUNDRED)
        expected_rule5 = expected_start_srv + elec_fee + serv_fee
        rule5_formula = (
            f"整笔费率=0：{shown(expected_start_srv)}(启动费抽成) + "
            f"{shown(electric)}×{_fmt_rate(elec_rate)}%({shown(elec_fee)}) + "
            f"{shown(service_fee_diff)}×{_fmt_rate(service_rate)}%({shown(serv_fee)}) "
            f"= {shown(expected_rule5)}"
        )
        rule5_reason = ""
    else:
        unified_base = electric + service_fee_diff + start + add_tax
        unified_part = _round2(unified_base * order_rate / _HUNDRED)
        elec_fee = _round2(electric * order_rate / _HUNDRED)
        serv_fee = _round2(service_fee_diff * order_rate / _HUNDRED)
        start_fee2 = _round2(start * order_rate / _HUNDRED)
        tax_fee = _round2(add_tax * order_rate / _HUNDRED)
        expected_rule5 = expected_start_srv + unified_part
        per_item_sum = expected_start_srv + elec_fee + serv_fee + start_fee2 + tax_fee
        rule5_formula = (
            f"整笔费率={_fmt_rate(order_rate)}%：{shown(expected_start_srv)}(启动费独立抽成) + "
            f"({shown(electric)}+{shown(service_fee_diff)}+{shown(start)}+{shown(add_tax)})"
            f"×{_fmt_rate(order_rate)}%({shown(unified_part)}) = {shown(expected_rule5)}"
        )
        rule5_reason = ""
        if order_amount_service != expected_rule5 and order_amount_service == per_item_sum:
            rule5_reason = "与分项逐项四舍五入之和完全相等，差额源于合并基数与分项舍入方式差异"
    check_rule5 = _check(
        "规则5 平台服务费",
        rule5_formula,
        None if config_missing else expected_rule5,
        order_amount_service,
        rule5_reason,
    )

    # 规则6：订单分佣基数（5 种组合 + 电损扣减 + 小于0取0）
    rule6_expected: Decimal | None
    if config_missing or mode is None:
        rule6_expected = None
        rule6_formula = "需有效的结算模式与店铺等级抽成配置"
    else:
        if mode == 1:
            raw_r6 = (
                electric
                + first_service_fee
                + start
                + add_tax
                - charging_rop
                - service_rop
                - start_rop
                - tax_rop
                - charge_market
                - order_amount_service
            )
            rule6_formula = (
                f"模式=1(整笔跟随)：{shown(electric)}(电费)+{shown(first_service_fee)}(服务费)"
                f"+{shown(start)}(启动费)+{shown(add_tax)}(增值税)-{shown(charging_rop)}-{shown(service_rop)}"
                f"-{shown(start_rop)}-{shown(tax_rop)}-{shown(charge_market)}(优惠)"
                f"-{shown(order_amount_service)}(平台服务费)"
            )
        elif order_rate == 0 and mode == 2:
            raw_r6 = (
                electric
                + first_service_fee
                - charging_rop
                - service_rop
                - charge_market
                - elec_fee
                - serv_fee
            )
            rule6_formula = (
                f"整笔费率=0且模式=2(电费跟随服务费)：{shown(electric)}(电费)+{shown(first_service_fee)}(服务费)"
                f"-{shown(charging_rop)}-{shown(service_rop)}-{shown(charge_market)}(优惠)"
                f"-{shown(elec_fee)}(电费抽成)-{shown(serv_fee)}(服务费抽成)"
            )
        elif order_rate != 0 and mode == 2:
            raw_r6 = (
                electric
                + first_service_fee
                - charging_rop
                - service_rop
                - charge_market
                - order_amount_service
                + expected_start_srv
            )
            rule6_formula = (
                f"整笔费率≠0且模式=2(电费跟随服务费)：{shown(electric)}(电费)+{shown(first_service_fee)}(服务费)"
                f"-{shown(charging_rop)}-{shown(service_rop)}-{shown(charge_market)}(优惠)"
                f"-{shown(order_amount_service)}(平台服务费)+{shown(expected_start_srv)}(启动费服务费)"
            )
        elif order_rate == 0:
            raw_r6 = first_service_fee - service_rop - charge_market - serv_fee
            rule6_formula = (
                f"整笔费率=0且模式=0(电费单独结算)：{shown(first_service_fee)}(服务费)"
                f"-{shown(service_rop)}(服务费渠道费)-{shown(charge_market)}(优惠)"
                f"-{shown(serv_fee)}(服务费抽成)"
            )
        else:
            raw_r6 = (
                first_service_fee - service_rop - charge_market - order_amount_service + expected_start_srv
            )
            rule6_formula = (
                f"整笔费率≠0且模式=0(电费单独结算)：{shown(first_service_fee)}(服务费)"
                f"-{shown(service_rop)}(服务费渠道费)-{shown(charge_market)}(优惠)"
                f"-{shown(order_amount_service)}(平台服务费)+{shown(expected_start_srv)}(启动费服务费)"
            )
        if electric_lose > 0:
            raw_r6 -= electric_lose
            rule6_formula += f" - {shown(electric_lose)}(电损金额)"
        rule6_formula += f" = {shown(raw_r6)}"
        rule6_expected = max(_ZERO, raw_r6)
        if raw_r6 < 0:
            rule6_formula += " → 小于0取0.00"
    check_rule6 = _check("规则6 订单分佣基数", rule6_formula, rule6_expected, recorded_commission_base)

    checks = [
        check_rule1,
        check_rule2,
        check_rule3_1,
        check_rule3_2,
        check_rule4_1,
        check_rule4_2,
        check_rule4_3,
        check_rule4_4,
        check_rule5,
        check_rule6,
    ]

    # 按前置条件与已确认边界精准降级受影响项为“待验证”，严禁误报“不一致”或“通过”
    limitations: list[str] = []
    if truncated:
        msg = "商城子项超过单次查询上限发生截断，全部汇总核验待人工复核"
        limitations.append(msg)
        for item in checks:
            _mark_unverified(item, msg)
    if extra_fee_names:
        msg = f"订单含非零{'/'.join(extra_fee_names)}，超出四科目标准公式范围，受影响汇总项待验证"
        limitations.append(msg)
        for item in (check_rule1, check_rule4_4, check_rule5, check_rule6):
            _mark_unverified(item, msg)
    if per_kwh_mode:
        msg = (
            f"当前店铺等级配置了按电量定额抽成（settleFeeOfElect={_fmt_rate(per_kwh_fee)}），"
            "不适用百分比抽成公式"
        )
        limitations.append(msg)
        for item in (check_rule1, check_rule5, check_rule6):
            _mark_unverified(item, msg)
    if config_missing:
        msg = "缺少有效的店铺等级抽成费率配置（shop_level）"
        limitations.append(msg)
        for item in (check_rule1, check_rule2, check_rule5, check_rule6):
            _mark_unverified(item, msg)
    if mode_conflict or mode is None:
        msg = "场站/订单结算类型与商城子项实际分佣标记（is_commission）不自洽，需人工核对"
        limitations.append(msg)
        for item in (check_rule1, check_rule6):
            _mark_unverified(item, msg)
    if has_refund:
        msg = f"订单存在非零退款（{shown(refund_amount)}），商城子项退款分摊口径待人工复核"
        limitations.append(msg)
        for item in (check_rule1, check_rule3_2, check_rule5, check_rule6):
            _mark_unverified(item, msg)

    limitations.extend(base_limitations)

    if any(item["status"] == "不一致" for item in checks):
        conclusion = "发现商城结算单账面清分不一致，不能判整单结算通过"
    elif any(item["status"] == "待验证" for item in checks):
        conclusion = "已完成可得结算数据的算术对照；部分规则因附加科目或前置条件待验证"
    else:
        conclusion = "商城结算单 6 类账面清分规则算术对照一致；历史等级费率变更及实际打款到账仍待验证"

    return {
        "checks": checks,
        "settlement_mode": mode_summary,
        "limitations": limitations,
        "conclusion": conclusion,
    }
