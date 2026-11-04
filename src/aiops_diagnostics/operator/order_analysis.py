"""SOP问题的可复核计算；只使用本轮已授权的订单与有限监测样本。"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if len(str(value)) > 64:
            return None
        result = Decimal(str(value))
        valid = result.is_finite() and result.as_tuple().exponent >= -12 and abs(result) < Decimal("1e15")
        return result if valid else None
    except (InvalidOperation, ValueError):
        return None


def shown(value, places=2):
    with localcontext() as context:
        context.prec = 64
        return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")


def analysis(evidence, question, language):
    text = question.casefold()
    detail = evidence["cpms_order_detail"]["data"]
    samples = (evidence.get("cpms_charging_samples", {}).get("data") or {}).get("records", [])
    zh = language.startswith("zh")
    lines = []
    metrics = [
        ("voltage", ("电压", "voltage"), "电压", "voltage", "V"),
        ("current", ("电流", "current"), "电流", "current", "A"),
        ("power", ("功率", "power"), "功率", "power", "（原值，单位待核对）"),
    ]
    for field, words, label, en, unit in metrics:
        if not any(w in text for w in words):
            continue
        values = [v for row in samples if (v := number(row.get(field))) is not None and v >= 0]
        if values:
            lines.append(
                f"本次样本中 {len(values)} 个有效{label}值，范围 {min(values):f}–{max(values):f} {unit}；"
                "不代表完整过程，不能单独判断故障。"
                if zh
                else f"{len(values)} valid {en} samples: {min(values):f}–{max(values):f} {unit}. "
                "This is not the complete timeline or a fault verdict."
            )
        else:
            lines.append(
                f"没有有效监测样本，无法核对{label}变化；订单单值与额定参数不能还原充电过程。"
                if zh
                else f"No valid monitor samples: {en} changes cannot be assessed. "
                "Order and rated values do not reconstruct the charging timeline."
            )
    if any(w in text for w in ("功率", "电量", "power", "energy")):
        energy, minutes = number(detail.get("electricity")), number(detail.get("time"))
        if energy is not None and energy >= 0 and minutes is not None and minutes > 0:
            average = energy / minutes * 60
            lines.append(
                f"平均功率核算：{energy:f} kWh ÷ {minutes:f} min × 60 = {shown(average)} kW。"
                "额定功率不等于实际输出；平均值不能证明过程稳定。"
                if zh
                else f"Average power: {energy:f} kWh / {minutes:f} min × 60 = {shown(average)} kW. "
                "Rated power is not actual output; an average does not prove a stable timeline."
            )
        else:
            lines.append(
                "电量或正数分钟时长缺失，不能计算平均功率。"
                if zh
                else "Energy or a positive duration in minutes is missing; "
                "average power cannot be calculated."
            )
    if any(w in text for w in ("电费", "服务费", "费用", "账单", "fee", "bill", "cost")):
        electric, service = number(detail.get("electricityFee")), number(detail.get("serviceFee"))
        if electric is not None and service is not None:
            lines.append(
                f"已记录的两项小计：电费 {shown(electric)} + 服务费 {shown(service)} "
                f"= {shown(electric + service)}"
                "（沿用订单金额单位）。这不是含全部附加费的总价，不能据此反推计费单价。"
                if zh
                else f"Recorded subtotal: electricity {shown(electric)} + service {shown(service)} = "
                f"{shown(electric + service)} in the order currency. "
                "This excludes other fee categories and cannot establish the tariff."
            )
    return ("\n\n" + ("【本轮分析】" if zh else "Analysis") + "\n" + "\n".join(lines)) if lines else ""
