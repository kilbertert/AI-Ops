"""历史模板与账面金额核对；算术相符不代表计量、优惠资格或支付到账已验证。"""

import json
from decimal import Decimal, localcontext

from aiops_diagnostics.operator.operator_conversation import OperatorRefused
from aiops_diagnostics.operator.order_analysis import number, shown


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate discount field")
        result[key] = value
    return result


def read_billing(body, caller, order, site):
    from aiops_diagnostics.operator.operator_tools import tool
    from aiops_diagnostics.operator.order_pricing import project

    if not isinstance(body, dict) or type(body.get("code")) is not int or body["code"] != 0:
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    row = body.get("data")
    if not isinstance(row, dict):
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    if (row.get("orderNo"), row.get("tenantId"), row.get("siteId")) != (
        order,
        caller.effective_tenant_id,
        site,
    ):
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    raw = row.get("discountDetails")
    discounts = None
    if raw is not None:
        if not isinstance(raw, str) or len(raw) > 4096:
            raise OperatorRefused("CPMS_INVALID_FIELD")
        try:
            discounts = json.loads(raw, object_pairs_hook=_unique) if raw.strip() else None
        except (ValueError, RecursionError) as exc:
            raise OperatorRefused("CPMS_INVALID_FIELD") from exc
        if discounts is not None and not isinstance(discounts, dict):
            raise OperatorRefused("CPMS_INVALID_FIELD")
    shape = tool("cpms_order_billing").evidence_shape
    result = project({**{k: row[k] for k in shape if k in row}, "discounts": discounts}, shape)
    amounts = [v for k, v in result.items() if k not in {"orderNo", "siteId", "discounts"}]
    amounts.extend((result.get("discounts") or {}).values())
    if any(v is not None and (number(v) is None or number(v) < 0) for v in amounts):
        raise OperatorRefused("CPMS_INVALID_FIELD")
    return result


def _amount(row, key):
    value = number(row.get(key))
    return value if value is not None and value >= 0 else None


def _check(label, formula, expected, recorded, reason=""):
    expected = Decimal(shown(expected)) if expected is not None else None
    difference = recorded - expected if expected is not None and recorded is not None else None
    status = "待验证" if difference is None or reason else "通过" if difference == 0 else "不一致"
    return dict(
        label=label,
        formula=formula,
        calculated=expected,
        recorded=recorded,
        difference=difference,
        status=status,
        reason=reason,
    )


def verify(snapshot, detail, billing=None):
    """每项独立对照后台值；整体判据同时要求前置证据成立。"""
    with localcontext() as context:
        context.prec = 64
        return _verify(snapshot, detail, billing)


def _verify(snapshot, detail, billing):
    template = snapshot.get("feeTemplate") or {}
    limitations = []
    if not template or snapshot.get("missing"):
        limitations.append("缺少订单历史计费模板，不能用当前站点价格替代")
    if (
        type(detail.get("type")) is not int
        or detail["type"] != 0
        or detail.get("launchType") in (None, "", "operator")
    ):
        limitations.append("订单类型或启动类型不满足普通汽车充电口径")
    if (
        type(detail.get("status")) is not int
        or detail["status"] not in (1, 2)
        or detail.get("packageType") is not None
    ):
        limitations.append("订单未结束或适用套餐，最终收费规则待核对")
    energy = _amount(detail, "electricity")
    if energy is None or energy == 0:
        limitations.append("电量为零或缺失，启动费减免等特殊结算分支待核对")
    if billing is None:
        limitations.append("未取得已授权的账务/优惠明细")
    else:
        for key in (
            "electricityFee",
            "serviceFee",
            "launchFee",
            "dsElectricFee",
            "dsServiceFee",
            "parkFee",
            "totalAmount",
            "marketAmount",
            "vat",
            "vatFee",
            "payAmount",
        ):
            if key in billing and key in detail and number(billing[key]) != number(detail[key]):
                limitations.append("同轮订单与账务记录不一致，需重新取证")
                break
        for key in ("reduceBalance", "insuranceAmount"):
            value = _amount(billing, key)
            if value is None or value != 0:
                limitations.append("余额抵扣或保险费用未排除，税基与付款口径待核对")
                break

    checks = []
    vat, snapshot_vat = _amount(detail, "vat"), _amount(template, "vat")
    if snapshot_vat is not None and snapshot_vat > 100:
        snapshot_vat = None
    checks.append(_check("模板税率", "订单税率 = 历史模板税率（%）", snapshot_vat, vat))
    enabled = template.get("enableLaunchFee")
    launch = Decimal(0) if enabled is False else _amount(template, "launchFee") if enabled is True else None
    checks.append(
        _check("模板启动费", "订单启动费 = 历史模板有效启动费", launch, _amount(detail, "launchFee"))
    )

    electric, service = _amount(detail, "electricityFee"), _amount(detail, "serviceFee")
    loss_e, loss_s, park = (_amount(detail, k) for k in ("dsElectricFee", "dsServiceFee", "parkFee"))
    discounts = (billing or {}).get("discounts")
    rate = _amount(discounts or {}, "serviceDiscount")
    if rate is not None and rate > 10:
        rate = None
    basis = service + loss_s if service is not None and loss_s is not None else None
    reduction = basis - Decimal(shown(basis * rate / 10)) if basis is not None and rate is not None else None
    formula = (
        f"{shown(basis)} - {shown(basis)} × {rate * 10:f}% = {shown(reduction)}（折后价先取两位）"
        if reduction is not None
        else "服务费计费基数 - 服务费计费基数 × 折后比例"
    )
    checks.append(
        _check(
            "规则1 服务费折扣优惠",
            formula,
            reduction,
            None,
            "接口未提供独立服务费折扣额，不能把总优惠当此项；本单是否有折扣以实际优惠记录为准",
        )
    )
    components = (electric, service, _amount(detail, "launchFee"), loss_e, loss_s, park)
    subtotal = Decimal(shown(sum(components))) if all(v is not None for v in components) else None
    formula = "电费 + 服务费 + 启动费（非零电损/停车费另列）"
    if subtotal is not None:
        formula = " + ".join(shown(x) for x in components[:3])
        for label, value in zip(("电损电费", "电损服务费", "停车费"), components[3:], strict=True):
            if value:
                formula += f" + {shown(value)}({label})"
        formula += " = " + shown(subtotal)
    checks.append(
        _check(
            "规则2 充电费用",
            formula,
            subtotal,
            _amount(detail, "totalAmount"),
            "费用分项缺失或非法，不能按零处理" if subtotal is None else "",
        )
    )
    total, discount = _amount(detail, "totalAmount"), _amount(detail, "marketAmount")
    tax_base = total - discount if total is not None and discount is not None and discount <= total else None
    tax = tax_base * snapshot_vat / 100 if tax_base is not None and snapshot_vat is not None else None
    formula = (
        f"({shown(total)} - {shown(discount)}) × {snapshot_vat:f}% = {shown(tax)}"
        if tax is not None
        else "(充电费用 - 优惠减免) × 历史模板增值税比例"
    )
    checks.append(_check("规则3 增值税费用", formula, tax, _amount(detail, "vatFee")))
    tax_record = _amount(detail, "vatFee")
    paid = tax_base + tax_record if tax_base is not None and tax_record is not None else None
    formula = (
        f"{shown(total)} - {shown(discount)} + {shown(tax_record)} = {shown(paid)}"
        if paid is not None
        else "充电费用 - 优惠减免 + 增值税"
    )
    checks.append(_check("规则4 实付款", formula, paid, _amount(detail, "payAmount")))
    # 用重新算出的小计/税额另走整条链，避免错误中间值彼此相符而被当成全链通过。
    recomputed_base = (
        subtotal - discount
        if subtotal is not None and discount is not None and discount <= subtotal
        else None
    )
    payable = (
        recomputed_base + Decimal(shown(recomputed_base * snapshot_vat / 100))
        if recomputed_base is not None and snapshot_vat is not None
        else None
    )
    checks.append(
        _check(
            "独立串联复算",
            "费用分项合计 - 记录优惠 + 按历史税率重新计算的税额",
            payable,
            _amount(detail, "payAmount"),
        )
    )
    # 先计算差额供排查，但前置条件不成立时不允许把任何局部相符说成通过。
    if limitations:
        for check in checks:
            check["status"] = "待验证"
            check["reason"] = "；".join(filter(None, (check["reason"], *limitations)))
    if not isinstance(discounts, dict):
        limitations.append("缺订单优惠明细，总优惠适用性尚未独立验证")
    else:
        limitations.append("优惠资格及总优惠组成尚未独立核实；折扣试算不能冒充后台折扣额验证")
    if any(c["status"] == "不一致" for c in checks):
        conclusion = "发现账面或模板不一致，不能判整单通过"
    else:
        conclusion = "已完成可得数据的算术对照；整单计费仍待验证"
    return {
        "checks": checks,
        "limitations": limitations,
        "conclusion": conclusion,
        "template_name": template.get("name"),
    }


def report(snapshot, detail, billing, language):
    result = verify(snapshot, detail, billing)
    zh = language.startswith("zh")
    if not zh:
        labels = (
            "Historical tax rate (%)",
            "Historical launch fee",
            "Rule 1: service discount",
            "Rule 2: charging subtotal",
            "Rule 3: tax",
            "Rule 4: recorded payment",
            "Independent payable",
        )
        lines = ["Order amount verification (historical tariff + recorded discounts)"]
        for label, check in zip(labels, result["checks"], strict=True):
            values = [
                shown(check[k]) if check[k] is not None else "unavailable"
                for k in ("recorded", "calculated", "difference")
            ]
            status = {"通过": "MATCH", "不一致": "MISMATCH", "待验证": "UNVERIFIED"}[check["status"]]
            lines.append(
                f"{label}: recorded {values[0]}, computed {values[1]}, difference {values[2]} — {status}"
            )
        lines.append(
            "Amounts use half-up cents. Rule 1 lacks a separately recorded discount amount. "
            "MATCH only means local arithmetic agrees; whole-bill correctness, discount eligibility, "
            "meter accuracy and bank receipt are not verified."
        )
        if result["limitations"]:
            lines.append("Missing or conflicting evidence or an unsupported billing branch requires review.")
        return "\n".join(lines)
    lines = [
        "【订单金额验证】" if zh else "Order amount verification",
        ("历史模板：" if zh else "Historical template: ") + str(result["template_name"] or "未提供"),
    ]
    for check in result["checks"]:
        labels = (
            ("后台显示", "实际计算值", "差额", "验证结果")
            if zh
            else ("Recorded", "Computed", "Difference", "Result")
        )
        values = [
            shown(check[k]) if check[k] is not None else ("未提供/无法计算" if zh else "unavailable")
            for k in ("recorded", "calculated", "difference")
        ]
        lines.extend(
            [
                check["label"],
                "  " + check["formula"],
                "  "
                + "；".join(f"{k}: {v}" for k, v in zip(labels, [*values, check["status"]], strict=True)),
            ]
        )
        if check["reason"]:
            lines.append("  " + check["reason"])
    lines.append(result["conclusion"])
    lines.extend(result["limitations"])
    lines.append("各项通过仅表示该项算术一致；金额两位小数四舍五入，不证明原始计量、最终结算或银行到账。")
    return "\n".join(lines)
