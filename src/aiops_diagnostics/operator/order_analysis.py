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


#: 监测表视图的过程字段（cpms_charging_samples 证据形状）。
_MONITOR_FIELDS = (("voltage", "V"), ("current", "A"), ("power", "原值，单位待核对"))
#: 原始时序的过程字段（cpms_charging_timeline 证据形状，SOP §2.2 同名语义列）。
_TIMELINE_FIELDS = (("outputVoltage", "V"), ("outputCurrent", "A"), ("power", "原值，单位待核对"))


def _series_metrics(detail, rows, fields, complete_timeline):
    result = {"sample_count": len(rows), "complete_timeline": complete_timeline, "metrics": []}
    for field, unit in fields:
        values = [value for row in rows if (value := number(row.get(field))) is not None and value >= 0]
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


def metrics(detail, samples):
    """工具读取全部可得统计；是否调用及展示由模型决定，不再匹配问题词语。"""
    return _series_metrics(detail, samples.get("records", []), _MONITOR_FIELDS, False)


def timeline_metrics(detail, timeline):
    """监测表无行时的 SOP §2.2 兜底：同一已授权设备+窗口的时序行算同一组统计。

    行级 txSerialNo 多于一个说明窗口混入相邻交易——SOP 要求分段叙述不合并，
    统计同样不跨流水号：metrics 留空并在限制里说明，由订单详情字段继续提供
    整单口径，不猜哪一段属于本单。
    """
    rows = timeline.get("records", [])
    result = _series_metrics(detail, rows, _TIMELINE_FIELDS, bool(timeline.get("complete_timeline")))
    serials = {str(row.get("txSerialNo")) for row in rows if row.get("txSerialNo") not in (None, "")}
    if len(serials) > 1:
        result["metrics"] = []
        result["limitations"] += "窗口含多个交易流水号，统计未跨流水合并，按段参考时序行。"
    return result
