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


def metrics(detail, samples):
    """工具读取全部可得统计；是否调用及展示由模型决定，不再匹配问题词语。"""
    result = {"sample_count": len(samples.get("records", [])), "complete_timeline": False, "metrics": []}
    for field, unit in (("voltage", "V"), ("current", "A"), ("power", "原值，单位待核对")):
        values = [
            value
            for row in samples.get("records", [])
            if (value := number(row.get(field))) is not None and value >= 0
        ]
        result["metrics"].append(
            {
                "name": field,
                "unit": unit,
                "count": len(values),
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
            }
        )
    energy, minutes = number(detail.get("electricity")), number(detail.get("time"))
    result["average_power_kw"] = (
        shown(energy / minutes * 60)
        if energy is not None and energy >= 0 and minutes is not None and minutes > 0
        else None
    )
    result["limitations"] = "有限样本不能证明完整过程；平均功率不等于额定功率，缺样本不证明正常。"
    return result
