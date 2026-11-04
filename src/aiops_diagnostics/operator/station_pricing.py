"""指定站点、已确认条件下的一次连续充电估算。"""

from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiops_diagnostics.operator.order_analysis import number, shown


class PlanUnavailable(ValueError):
    """当前条件不能证明费用方案。"""


def minute(value, *, template_end=False):
    if value == "24:00" or (template_end and value == "23:59"):
        return 1440
    try:
        hour, minutes = value.split(":")
        if len(hour) != 2 or len(minutes) != 2 or not 0 <= int(hour) < 24 or not 0 <= int(minutes) < 60:
            raise ValueError()
        return int(hour) * 60 + int(minutes)
    except (ValueError, AttributeError) as exc:
        raise PlanUnavailable("请提供明确的HH:mm时间。") from exc


def clock(value):
    total_seconds = int((Decimal(value) * 60).to_integral_value(rounding=ROUND_CEILING))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}" + (f":{seconds:02d}" if seconds else "")


def cheapest(rows, start, end, energy, power):
    """穷举分钟起点，逐段积分；尾段按实际电量，不把整分钟费用当精确值。"""
    energy, power = number(energy), number(power)
    if energy is None or power is None or not 0 < energy <= 1000 or not 0 < power <= 1000:
        raise PlanUnavailable("需要正数电量(kWh)和可持续功率(kW)，上限各为1000。")
    low, high = minute(start), minute(end)
    if low >= high:
        raise PlanUnavailable("请提供同一计费日内递增的时间窗。")
    duration = energy * 60 / power
    if duration > high - low:
        raise PlanUnavailable("给定功率与时间窗不足以完成所需电量。")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise PlanUnavailable("计费时段缺失或超过支持范围。")
    intervals = []
    for row in rows:
        a, b = minute(row.get("startTime")), minute(row.get("endTime"), template_end=True)
        electric, service = number(row.get("price")), number(row.get("feePrice"))
        if a >= b or electric is None or service is None or min(electric, service) < 0:
            raise PlanUnavailable("时段费率缺失或无效。")
        intervals.append((a, b, electric, service))
    intervals.sort()
    if (
        intervals[0][0] != 0
        or intervals[-1][1] != 1440
        or any(left[1] != right[0] for left, right in zip(intervals, intervals[1:], strict=False))
    ):
        raise PlanUnavailable("计费时段存在空洞或重叠。")
    best = baseline = None
    for begin in range(low, int(Decimal(high) - duration) + 1):
        finish = Decimal(begin) + duration
        electric = service = Decimal(0)
        pieces = []
        for a, b, rate, fee in intervals:
            overlap = min(finish, b) - max(begin, a)
            if overlap <= 0:
                continue
            used = overlap * power / 60
            electric += used * rate
            service += used * fee
            pieces.append(
                {
                    "start": clock(Decimal(max(begin, a))),
                    "end": clock(min(finish, b)),
                    "energy": used,
                    "price": rate,
                    "feePrice": fee,
                }
            )
        if baseline is None:
            baseline = (Decimal(shown(electric)), Decimal(shown(service)))
        if best is None or Decimal(shown(electric)) + Decimal(shown(service)) < (
            Decimal(shown(best["electricity"])) + Decimal(shown(best["service"]))
        ):
            best = {
                "start": clock(Decimal(begin)),
                "end": clock(finish),
                "electricity": electric,
                "service": service,
                "energy": energy,
                "power": power,
                "pieces": pieces,
            }
    best["baseline"] = baseline
    return best


BASIS = "普通按量、不用优惠、不预约、不买保险、充完离场"


def validate_conditions(arguments):
    if arguments.get("basis") != BASIS:
        raise PlanUnavailable("请确认本次比较条件：" + BASIS + "；如需其他条件请明确说明。")
    try:
        zone = ZoneInfo(arguments.get("timezone", ""))
        day = date.fromisoformat(arguments.get("date", ""))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise PlanUnavailable("请提供日期YYYY-MM-DD与明确的计费时区（如Asia/Shanghai）。") from exc
    now = datetime.now(zone)
    today = now.date()
    if (
        datetime.combine(day, datetime.min.time(), zone).utcoffset()
        != datetime.combine(day, datetime.max.time(), zone).utcoffset()
    ):
        raise PlanUnavailable("该计费日存在夏令时切换，需按实际时刻单独核算。")
    if not 0 <= (day - today).days <= 31:
        raise PlanUnavailable("请选择计费时区内今天至未来31天的日期；历史订单请用历史核算。")
    start, end = minute(arguments["start"]), minute(arguments["end"])
    if day == today and start <= now.hour * 60 + now.minute:
        raise PlanUnavailable("计划开始时间已经过去，请提供未来时间窗。")
    if start >= end:
        raise PlanUnavailable("请提供同一计费日内递增的时间窗。")


def plan_text(rules, arguments):
    """按已查规则估算基础充电费；未覆盖类别逐项列出。"""
    from aiops_diagnostics.operator.order_pricing import period_rates

    if rules.get("timezone") and rules["timezone"] != arguments.get("timezone"):
        raise PlanUnavailable("输入时区与站点计费时区不一致，请核对后重新提供时间窗。")
    template = rules["template"]
    if (
        type(template.get("type")) is not int
        or template["type"] != 0
        or type(template.get("billingMode")) is not int
        or template["billingMode"] not in (0, 2)
    ):
        raise PlanUnavailable("当前只支持四轮按电量的分时计费；该计费模式需单独核对。")
    if number(template.get("freeChargingTime")) != 0:
        raise PlanUnavailable("免费时长规则尚未完整核对，不能给出最低费用方案。")
    rows = []
    for item in template.get("feeTemplateItems") or []:
        electric, service = period_rates(template, item)
        rows.append({**item, "price": electric, "feePrice": service})
    result = cheapest(rows, arguments["start"], arguments["end"], arguments["energy"], arguments["power"])
    launch = number(template.get("launchFee"))
    if launch is None or launch < 0:
        raise PlanUnavailable("启动费用缺失或无效，不能核算费用。")
    if template.get("enableLaunchFee") is False:
        launch = Decimal(0)
    loss = number(template.get("electricityLoss"))
    if loss not in (None, Decimal(0)):
        raise PlanUnavailable("存在非零电损规则，需补充设备协议与损耗计费条件。")
    if rules["extras"].get("parkFeeTemplateId") is not False:
        raise PlanUnavailable("该站点停车收费条件尚未核实，不能给出最低总费用。")
    if type(template.get("enablePackage")) is not bool:
        raise PlanUnavailable("套餐适用状态缺失，不能确认可用付款方案。")
    if template.get("enablePackage") is True and any(
        type(p.get("type")) is not int or p["type"] != 2 for p in template.get("feeTemplatePackages") or []
    ):
        raise PlanUnavailable("该站含电量/时长/SOC套餐，需先核对套餐适用条件再比较总费用。")
    vat = number(template.get("vat"))
    if vat is None or not 0 <= vat <= 100:
        raise PlanUnavailable("税率缺失或无效，不能核算总费用。")
    electric, service = Decimal(shown(result["electricity"])), Decimal(shown(result["service"]))
    subtotal = electric + service + Decimal(shown(launch))
    tax = Decimal(shown(subtotal * vat / 100))
    total = subtotal + tax
    baseline_subtotal = sum(result["baseline"]) + Decimal(shown(launch))
    baseline_total = baseline_subtotal + Decimal(shown(baseline_subtotal * vat / 100))
    minimum = number(template.get("startLimit"))
    if minimum is None or minimum < 0:
        raise PlanUnavailable("最低启动金额未核实，不能确认方案可执行。")
    package_note = ""
    baseline_feasible = True
    if template.get("enablePackage") is True:
        amounts = [
            v
            for p in template.get("feeTemplatePackages") or []
            if (v := number(p.get("price"))) is not None and v >= max(total, minimum)
        ]
        if not amounts:
            raise PlanUnavailable("没有覆盖含税估算的金额套餐，请核对是否允许普通按量付款。")
        baseline_feasible = max(amounts) >= baseline_total
        package_note = f"金额套餐是预付限额，非折扣；覆盖含税估算的最小额度为 {shown(min(amounts))}。"
    lines = [
        f"站点：{rules['name']}；计费日：{rules['date']}"
        f"（{'节假日' if rules['holiday'] else '普通日'}规则）。",
        f"在 {arguments['start']}–{arguments['end']} 内按分钟选择开始时刻，连续充电含税估算最低方案：",
        f"{result['start']}–{result['end']}，{result['energy']:f} kWh，持续 {result['power']:f} kW。",
    ]
    for piece in result["pieces"][:4]:
        lines.append(
            f"{piece['start']}–{piece['end']}：{piece['energy']:.4f} kWh × "
            f"(电价 {piece['price']:f} + 服务费 {piece['feePrice']:f})。"
        )
    lines.append(
        f"电费 {shown(result['electricity'])} + 服务费 {shown(result['service'])} + "
        f"启动费 {shown(launch)} = {shown(subtotal)}（沿用站点计费金额单位）。"
    )
    if len(result["pieces"]) > 4:
        lines.append("其余时段计入合计，完整费率已随本轮证据保留。")
    if package_note:
        lines.append(package_note)
    lines.append(f"税额 {shown(subtotal)} × {vat:f}% = {shown(tax)}；含税合计 {shown(total)}。")
    if baseline_feasible:
        lines.append(
            f"对比最早{arguments['start']}开始：含税{shown(baseline_total)}，"
            f"预计节省{shown(baseline_total - total)}。"
        )
    else:
        lines.append("最早开始方案超出可用套餐额度，不能作为可执行的节省对照。")
    lines.append(f"最低启动金额 {shown(minimum)}；估算消费与预付门槛分别核对。")
    lines.append("采用已确认的普通按量、不用优惠、不预约、不买保险、充完离场条件；无停车模板。")
    lines.append(f"时间按已确认的计费时区 {arguments['timezone']}；假设持续达到输入功率、一次启动。")
    lines.append("结束时刻按秒向上显示；费用是当前规则下的估算，规则或实际功率变化需重新计算。")
    return "\n".join(lines)
