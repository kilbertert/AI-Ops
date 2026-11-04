"""历史计费快照与可复核的账面计算；不写业务数据。"""

from decimal import Decimal

from aiops_diagnostics.operator.operator_conversation import OperatorRefused
from aiops_diagnostics.operator.order_analysis import number, shown


def project(value, shape, depth=0):
    """只留显式字段；有界嵌套值的同一规则也由Java执行。"""
    if value is None:
        return None
    if depth > 6:
        raise OperatorRefused("CPMS_INVALID_FIELD")
    if shape is None:
        if isinstance(value, (dict, list)) or (isinstance(value, str) and len(value) > 1000):
            raise OperatorRefused("CPMS_INVALID_FIELD")
        return value
    if isinstance(shape, list):
        if not isinstance(value, list) or len(value) > 64 or any(row is None for row in value):
            raise OperatorRefused("CPMS_INVALID_FIELD")
        return [project(row, shape[0], depth + 1) for row in value]
    if not isinstance(value, dict):
        raise OperatorRefused("CPMS_INVALID_FIELD")
    return {k: project(value[k], sub, depth + 1) for k, sub in shape.items() if k in value}


def read_snapshot(body, caller, order):
    if not isinstance(body, dict) or type(body.get("code")) is not int or body["code"] != 0:
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    if "data" not in body:
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    row = body["data"]
    if row is None:
        return {"missing": True}
    if (
        not isinstance(row, dict)
        or row.get("orderNo") != order
        or row.get("tenantId") != caller.effective_tenant_id
    ):
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    from aiops_diagnostics.operator.operator_tools import tool

    return project(
        {k: row[k] for k in ("feeTemplate", "periodFeeDetail") if k in row},
        tool("cpms_order_tariff").evidence_shape,
    )


def period_rates(template, row):
    mode = template.get("billingMode")
    if type(mode) is not int or mode not in (0, 1, 2):
        return None, None
    if mode in (0, 1):
        kind = row.get("type")
        if type(kind) is not int or not 0 <= kind <= 3:
            return None, None
        label = ("tip", "peak", "flat", "valley")[kind]
        return number(template.get(label)), number(template.get(label + "Service"))
    return number(row.get("price")), number(row.get("feePrice"))


def independent_period_check(template, detail, zh):
    if (
        detail.get("type") != 0
        or detail.get("launchType") in (None, "", "operator")
        or not str(detail.get("deviceProtocol") or "").upper().startswith(("OCPP", "AYK", "YKC"))
        or template.get("billingMode") not in (0, 2)
        or detail.get("packageType") is not None
    ):
        return []
    amounts = [number(detail.get(key + "Electricity")) for key in ("tip", "peak", "flat", "valley")]
    energy = number(detail.get("electricity"))
    if any(value is None or value < 0 for value in amounts) or energy is None or sum(amounts) != energy:
        return []
    lines, electric_sum, service_sum = [], 0, 0
    for kind, amount in enumerate(amounts):
        if not amount:
            continue
        prices = {
            period_rates(template, row)
            for row in template.get("feeTemplateItems") or []
            if type(row.get("type")) is int and row["type"] == kind
        }
        if len(prices) != 1:
            return []
        electric, service = prices.pop()
        if electric is None or service is None or min(electric, service) < 0:
            return []
        electric_sum += amount * electric
        service_sum += amount * service
        lines.append(
            f"{('尖', '峰', '平', '谷')[kind] if zh else ('tip', 'peak', 'flat', 'valley')[kind]}: "
            f"{amount:f} kWh × {electric:f} = {shown(amount * electric)}; "
            f"{amount:f} kWh × {service:f} = {shown(amount * service)}"
        )
    for computed, key, label in (
        (electric_sum, "electricityFee", "电费" if zh else "Electricity"),
        (service_sum, "serviceFee", "服务费" if zh else "Service"),
    ):
        recorded = number(detail.get(key))
        if recorded is not None:
            difference = recorded - Decimal(shown(computed))
            lines.append(
                f"{label}: {shown(computed)}, "
                + (
                    f"订单 {shown(recorded)}，差额 {shown(difference)}"
                    if zh
                    else f"recorded {shown(recorded)}, difference {shown(difference)}"
                )
            )
    if lines:
        lines.insert(
            0,
            "按订单分时电量与快照单价核算（不使用费用明细反推电量）："
            if zh
            else "Recomputed from order period energy and snapshot rates (not fee-derived energy):",
        )
    return lines


def historical_report(snapshot, detail, language):
    zh = language.startswith("zh")
    if snapshot.get("missing") or not snapshot.get("feeTemplate"):
        return (
            "历史计费快照缺失，不能使用当前站点价替代。"
            if zh
            else "Historical tariff missing; current rates cannot replace it."
        )
    template = snapshot["feeTemplate"]
    lines = ["【历史计费核对】" if zh else "Historical billing checks"]
    mode = template.get("billingMode")
    for row in template.get("feeTemplateItems") or []:
        price, service = period_rates(template, row)
        if price is None or service is None:
            lines.append("时段费率缺项，无法核对。" if zh else "Incomplete period rates.")
            continue
        unit = (
            ("按时长另算" if zh else "time-based, calculated separately") if mode == 1 else f"{service:f}/kWh"
        )
        lines.append(
            f"{row.get('startTime')}–{row.get('endTime')}: "
            + (f"电价 {price:f}/kWh，服务费 {unit}" if zh else f"electricity {price:f}/kWh, service {unit}")
        )
    lines.extend(independent_period_check(template, detail, zh))
    periods = snapshot.get("periodFeeDetail") or []
    totals = [[], []]
    for row in periods:
        electric, service, total = (number(row.get(k)) for k in ("electricFee", "serviceFee", "totalFee"))
        if electric is None or service is None:
            lines.append("分时费用缺项，不能合计。" if zh else "Incomplete period fees; cannot sum.")
            totals = None
            break
        if totals is not None:
            totals[0].append(electric)
            totals[1].append(service)
        difference = (
            ("；差额 " if zh else "; difference ") + shown(total - electric - service)
            if total is not None
            else ("；原小计缺失" if zh else "; subtotal missing")
        )
        lines.append(
            f"{row.get('startTime')}–{row.get('endTime')}: {shown(electric)} + "
            f"{shown(service)} = {shown(electric + service)}" + difference
        )
    if totals and periods:
        for values, key, label in zip(
            totals,
            ("electricityFee", "serviceFee"),
            (("电费", "服务费") if zh else ("Electricity", "Service")),
            strict=True,
        ):
            recorded = number(detail.get(key))
            if recorded is not None:
                lines.append(
                    f"{label}分时合计 {shown(sum(values))}，订单记录 {shown(recorded)}，"
                    f"差额 {shown(recorded - sum(values))}。"
                    if zh
                    else f"{label} period sum {shown(sum(values))}, recorded {shown(recorded)}, "
                    f"difference {shown(recorded - sum(values))}."
                )
    elif not periods:
        lines.append("没有分时费用记录，尚不能对照时段合计。" if zh else "No period fee records to compare.")
    lines.append(
        "金额沿用订单币种，按两位小数四舍五入。分时展示电量可能由费用反推，并非独立电表读数；"
        "上述一致性检查不能证明原始计量或最终计费正确。"
        if zh
        else "Amounts use the order currency and half-up cents. Displayed period energy may be "
        "derived from fees, not independent meter readings; "
        "arithmetic agreement does not prove billing correctness."
    )
    protocol, launch = detail.get("deviceProtocol"), detail.get("launchType")
    if launch == "operator":
        lines.append(
            "运营商启动订单应对照运营商原账单，不套用桩侧服务费公式。"
            if zh
            else "Operator-launched order: compare the operator bill, not the charger-side service formula."
        )
    elif not protocol or not launch:
        lines.append(
            "协议或启动类型缺失，无法选定SOP的运营商、平台计费或桩计费分支。"
            if zh
            else "Protocol or launch type missing; the SOP billing branch cannot be determined."
        )
    elif str(protocol).upper().startswith(("OCPP", "AYK")):
        lines.append(
            "平台计费分支还需电表起止值与原始分时计量，不能以桩上报总额替代。"
            if zh
            else "Platform billing still needs start/end meters and raw interval readings."
        )
    elif str(protocol).upper().startswith("YKC"):
        lines.append(
            "桩计费分支还需原始交易报文的分时电量与总额，才能交叉核对桩侧服务费。"
            if zh
            else "Charger billing still needs raw transaction period energy and total fees."
        )
    else:
        lines.append(
            "该协议的独立核算口径尚未验证。"
            if zh
            else "This protocol is not verified for independent billing checks."
        )
    lines.append(
        "启动费、电损、停车、占位、优惠、套餐及税费需另核对；上述小计不是最终应付额。"
        if zh
        else "Launch, loss, parking, occupancy, discounts, packages and tax need separate checks; "
        "this is not the final payable."
    )
    return "\n".join(lines)
