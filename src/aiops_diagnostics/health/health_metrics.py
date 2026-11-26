"""电池充电健康评估指标、五维评分、SOH 浅充判定与确定性结论（health-v3 / ADR-0352）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE, NON_CHINESE_LANGUAGES
from aiops_diagnostics.common.scalar_rules import as_datetime
from aiops_diagnostics.health.health_report_copy import health_advice, health_conclusion_headline
from aiops_diagnostics.health.ykc_frames import (
    BATTERY_TYPE_LFP,
    BATTERY_TYPE_NCM,
    YkcProtocolEvidence,
)

#: 作业行与报告体共享的唯一规则版本（ADR-0352）。
#: 凡涉及指标增删、阈值/滑窗口径、五维分段公式、SOH 浅充判据或结论分类变化均递增此版本。
RULE_VERSION = "health-v3"

#: SOH 有效估算最小 SOC 增幅（规格 §2.3：ΔSOC < 30% 判浅充，不作有效健康度判定）。
SOH_MIN_SOC_DELTA = 30.0
#: 默认快充恒流段充放电综合能效因子（规格 §2）。
DEFAULT_EFFICIENCY = 0.94

#: 温升速率 5 分钟滑窗（规避 1℃ 整数采样在 15s 间隔下的量化假尖峰，规格 §6）。
TEMP_RISE_WINDOW_SECONDS = 300.0
TEMP_RISE_MIN_SPAN_SECONDS = 60.0

#: SOC 变化率 1 分钟滑窗与 15 秒跳变检测窗口（规格 §3 & §6）。
SOC_RATE_WINDOW_SECONDS = 60.0
SOC_RATE_MIN_SPAN_SECONDS = 15.0
SOC_JUMP_WINDOW_SECONDS = 15.0
SOC_JUMP_THRESHOLD_PCT = 3.0


def score_temperature_balance(delta: float) -> float:
    """温度均衡性五维评分（规格 §4.2）。"""
    if delta <= 2:
        return 100.0
    if delta <= 5:
        return round(100.0 - (delta - 2.0) * 6.0, 2)
    if delta <= 8:
        return round(82.0 - (delta - 5.0) * 10.0, 2)
    return round(max(0.0, 52.0 - (delta - 8.0) * 13.0), 2)


def score_max_temperature(value: float) -> float:
    """最高温度五维评分（规格 §4.3）。"""
    if value <= 35:
        return 100.0
    if value <= 45:
        return round(100.0 - (value - 35.0) * 1.8, 2)
    if value <= 55:
        return round(82.0 - (value - 45.0) * 5.0, 2)
    return 0.0


def score_capacity(soh: float | None) -> float | None:
    """电池容量五维评分（规格 §4.4）。"""
    if soh is None:
        return None
    if soh >= 95:
        return 100.0
    if soh >= 80:
        return round(80.0 + (soh - 80.0) / 15.0 * 20.0, 2)
    if soh >= 70:
        return round(60.0 + (soh - 70.0) / 10.0 * 20.0, 2)
    return round(max(0.0, soh / 70.0 * 60.0), 2)


def score_voltage_balance(delta_mv: float) -> float:
    """电压均衡性五维评分（规格 §4.5）。"""
    if delta_mv <= 30:
        return 100.0
    if delta_mv <= 80:
        return round(100.0 - (delta_mv - 30.0) * 0.36, 2)
    if delta_mv <= 150:
        return round(82.0 - (delta_mv - 80.0) * 0.45, 2)
    return round(max(0.0, 50.5 - (delta_mv - 150.0) * 0.6), 2)


def score_soc_balance(jumps: int, alarms: int) -> float:
    """SOC 均衡性五维评分（规格 §4.1）。"""
    return round(max(0.0, 100.0 - jumps * 15.0 - alarms * 30.0), 2)


def calculate_soh(
    energy_kwh: float,
    soc_delta: float,
    nominal_kwh: float | None,
    efficiency: float = DEFAULT_EFFICIENCY,
    *,
    allow_shallow: bool = False,
) -> float | None:
    """按规格 §2.2 计算电池健康度 SOH（%）。

    默认严格执行 §2.3 有效充电条件（``ΔSOC >= 30%``）；浅充返回 ``None``，
    仅在 ``allow_shallow=True`` 时返回供参考标注的浅充估算值。
    """
    min_delta = 0.0 if allow_shallow else SOH_MIN_SOC_DELTA
    if nominal_kwh is None or nominal_kwh <= 0 or energy_kwh <= 0 or soc_delta <= 0 or soc_delta < min_delta:
        return None
    value = energy_kwh / (soc_delta / 100.0) * efficiency / nominal_kwh * 100.0
    return round(value, 1) if 0.0 < value <= 200.0 else None


def health_scores(
    *,
    delta_temperature: float | None,
    max_temperature: float | None,
    soh: float | None,
    delta_voltage_mv: float | None,
    soc_jumps: int | None,
    alarms: int | None,
    capacity_reason_code: str | None = None,
    temp_delta_reason_code: str | None = None,
    max_temp_reason_code: str | None = None,
    voltage_reason_code: str | None = None,
    soc_reason_code: str | None = None,
) -> list[dict[str, Any]]:
    """构造五维雷达图评分数组。"""
    entries: list[tuple[str, float | None, str | None]] = [
        (
            "temperature_balance",
            score_temperature_balance(delta_temperature) if delta_temperature is not None else None,
            temp_delta_reason_code or "TEMP_MIN_DATA_MISSING",
        ),
        (
            "max_temperature",
            score_max_temperature(max_temperature) if max_temperature is not None else None,
            max_temp_reason_code or "TEMP_DATA_MISSING",
        ),
        (
            "capacity",
            score_capacity(soh),
            capacity_reason_code or "SOH_UNAVAILABLE",
        ),
        (
            "voltage_balance",
            score_voltage_balance(delta_voltage_mv) if delta_voltage_mv is not None else None,
            voltage_reason_code or "CELL_VOLTAGE_DATA_MISSING",
        ),
        (
            "soc_balance",
            score_soc_balance(soc_jumps, alarms or 0) if soc_jumps is not None else None,
            soc_reason_code or "SOC_SERIES_INSUFFICIENT",
        ),
    ]
    return [
        {
            "code": code,
            "score": round(score, 1) if score is not None else None,
            "status": (
                "unavailable"
                if score is None
                else "normal"
                if score >= 80
                else "attention"
                if score >= 60
                else "abnormal"
            ),
            "reason_code": None if score is not None else missing_reason,
        }
        for code, score, missing_reason in entries
    ]


def enrich_report(
    report: dict[str, Any],
    samples: list[Mapping[str, Any]],
    order: Mapping[str, Any],
    *,
    protocol: YkcProtocolEvidence | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    """基于订单、枪遥测样本与可选 YKC 协议帧，补全 `health-v3` 完整指标、雷达、结论与概览。"""
    proto = protocol or YkcProtocolEvidence()
    ordered_samples = _sort_samples(samples)

    # 1. 温度系列与温差 / 最高温 / 温升速率
    temp_max_series, temp_delta_values = _extract_temperature_series(ordered_samples, proto)
    max_temp = round(max(v for _, v in temp_max_series), 1) if temp_max_series else None
    if max_temp is None and proto.end_max_temp_c is not None:
        max_temp = round(proto.end_max_temp_c, 1)

    delta_t: float | None = round(max(temp_delta_values), 1) if temp_delta_values else None
    if delta_t is None and proto.end_max_temp_c is not None and proto.end_min_temp_c is not None:
        end_delta = proto.end_max_temp_c - proto.end_min_temp_c
        if end_delta >= 0:
            delta_t = round(end_delta, 1)

    temp_rise_rate = _sliding_window_max_rise_rate(
        temp_max_series,
        window_seconds=TEMP_RISE_WINDOW_SECONDS,
        min_span_seconds=TEMP_RISE_MIN_SPAN_SECONDS,
    )

    # 2. SOC 系列、起止 SOC、变化率与 15s 跳变
    soc_series = _extract_soc_series(ordered_samples, proto)
    soc_change_rate = _sliding_window_max_rise_rate(
        soc_series,
        window_seconds=SOC_RATE_WINDOW_SECONDS,
        min_span_seconds=SOC_RATE_MIN_SPAN_SECONDS,
        allow_negative_abs=True,
    )
    soc_jumps = _count_soc_jumps(soc_series) if len(soc_series) >= 2 else None
    soc_alarms = proto.soc_alarm_count if proto.temp_points else 0

    start_soc, end_soc, soc_delta = _resolve_soc_span(soc_series, order, proto)

    # 3. 充入电量、标称容量与 SOH / 浅充判定（选项 A：总健康分 = 有效 SOH）
    energy_delta = _resolve_energy_delta(ordered_samples, order)
    nominal_kwh, nominal_source = _resolve_nominal_capacity(order, proto)

    is_shallow_charge = soc_delta is not None and 0.0 < soc_delta < SOH_MIN_SOC_DELTA
    soh = (
        calculate_soh(energy_delta, soc_delta, nominal_kwh)
        if energy_delta is not None and soc_delta is not None
        else None
    )
    soh_shallow_estimate = (
        calculate_soh(energy_delta, soc_delta, nominal_kwh, allow_shallow=True)
        if is_shallow_charge and energy_delta is not None and soc_delta is not None
        else None
    )
    if soh is not None:
        soh_status = "normal"
        capacity_reason_code: str | None = None
    elif is_shallow_charge:
        soh_status = "shallow_charge"
        capacity_reason_code = "SHALLOW_CHARGE"
    elif nominal_kwh is None:
        soh_status = "unavailable"
        capacity_reason_code = "NOMINAL_CAPACITY_MISSING"
    elif soc_delta is None or soc_delta <= 0:
        soh_status = "unavailable"
        capacity_reason_code = "SOC_DELTA_MISSING"
    else:
        soh_status = "unavailable"
        capacity_reason_code = "ENERGY_DATA_MISSING"

    # 4. 单体电压极差（0x19 结算值 vs 0x23 过程值）与单体最高压
    battery_type = proto.battery_type or _normalize_battery_type(order.get("battery_type"))
    cell_series_count = _positive_int(order.get("cell_series_count"))
    (
        cell_delta_end_mv,
        cell_delta_process_mv,
        cell_delta_sample_mv,
    ) = _resolve_cell_voltage_deltas(ordered_samples, proto, cell_series_count)

    # 体检指标口径（规格 §3）：优先 0x19 结算值，次选过程值，次选直接样本差值
    indicator_cell_delta_mv = (
        cell_delta_end_mv
        if cell_delta_end_mv is not None
        else cell_delta_process_mv
        if cell_delta_process_mv is not None
        else cell_delta_sample_mv
    )
    # 雷达图口径（规格 §4.5）：优先 0x23 过程值，缺电芯串数时回退 0x19 结算值并标明 basis
    if cell_delta_process_mv is not None:
        radar_cell_delta_mv: float | None = cell_delta_process_mv
        voltage_balance_basis: str | None = "process"
    elif cell_delta_end_mv is not None:
        radar_cell_delta_mv = cell_delta_end_mv
        voltage_balance_basis = "settlement_0x19"
    elif cell_delta_sample_mv is not None:
        radar_cell_delta_mv = cell_delta_sample_mv
        voltage_balance_basis = "sample_range"
    else:
        radar_cell_delta_mv = None
        voltage_balance_basis = None

    max_cell_voltage_v, max_cell_probe_no = _resolve_max_cell_voltage(ordered_samples, proto)

    # 5. 构造 7 项完整体检指标（保留既有 stop_reason，追加其余 6 项）
    base_indicators = [item for item in (report.get("indicators") or []) if item.get("code") == "stop_reason"]
    new_indicators = [
        _build_temp_delta_indicator(delta_t, has_telemetry=bool(ordered_samples or proto.has_data)),
        _build_temp_rise_rate_indicator(temp_rise_rate),
        _build_max_temp_indicator(max_temp),
        _build_soc_change_rate_indicator(soc_change_rate, soc_jumps),
        _build_cell_voltage_delta_indicator(indicator_cell_delta_mv),
        _build_cell_max_voltage_indicator(max_cell_voltage_v, battery_type, max_cell_probe_no),
    ]
    all_indicators = [*base_indicators, *new_indicators]
    report["indicators"] = all_indicators

    # 6. 五维雷达图评分
    report["radar"] = health_scores(
        delta_temperature=delta_t,
        max_temperature=max_temp,
        soh=soh,
        delta_voltage_mv=radar_cell_delta_mv,
        soc_jumps=soc_jumps,
        alarms=soc_alarms,
        capacity_reason_code=capacity_reason_code,
        temp_delta_reason_code=(
            "TEMP_MIN_DATA_MISSING" if (ordered_samples or proto.has_data) else "TELEMETRY_DATA_MISSING"
        ),
        max_temp_reason_code="TEMP_DATA_MISSING",
        voltage_reason_code="CELL_VOLTAGE_DATA_MISSING",
        soc_reason_code="SOC_SERIES_INSUFFICIENT",
    )

    # 7. 中间指标、总健康分（选项 A）与概览
    duration_seconds = _resolve_duration_seconds(order, report)
    report["overall_health_score"] = soh
    report["overall_health_source"] = "soh"
    report["overview"] = {
        "start_soc": start_soc,
        "end_soc": end_soc,
        "soc_delta": soc_delta,
        "energy_charged_kwh": energy_delta,
        "duration_seconds": duration_seconds,
        "battery_type": battery_type,
        "overall_health_score": soh,
        "overall_health_source": "soh",
        "soh_status": soh_status,
        "is_shallow_charge": is_shallow_charge,
    }
    report["health_metrics"] = {
        "temperature_delta": delta_t,
        "max_temperature": max_temp,
        "temp_rise_rate_c_per_min": temp_rise_rate,
        "soc_delta": soc_delta,
        "soc_change_rate_pct_per_min": soc_change_rate,
        "soc_jumps_15s": soc_jumps,
        "soc_alarm_count": soc_alarms if proto.temp_points else None,
        "energy_charged_kwh": energy_delta,
        "nominal_capacity_kwh": nominal_kwh,
        "nominal_capacity_source": nominal_source,
        "soh": soh,
        "soh_shallow_estimate": soh_shallow_estimate,
        "soh_status": soh_status,
        "is_shallow_charge": is_shallow_charge,
        "battery_type": battery_type,
        "cell_voltage_delta_end_mv": cell_delta_end_mv,
        "cell_voltage_delta_process_mv": cell_delta_process_mv,
        "voltage_balance_basis": voltage_balance_basis,
        "max_cell_voltage_v": max_cell_voltage_v,
        "max_cell_voltage_probe_no": max_cell_probe_no,
    }

    # 8. 完整度与确定性 11 语分档结论
    available_count = sum(1 for item in all_indicators if item.get("status") != "unavailable")
    report["completeness"] = round(available_count / len(all_indicators), 3) if all_indicators else 0.0

    conclusion = _build_conclusion(
        all_indicators,
        is_shallow_charge=is_shallow_charge,
        language=language,
    )
    report["conclusion"] = conclusion
    report["summary"] = conclusion["text"]

    source_summary = report.get("source_summary")
    if isinstance(source_summary, dict):
        source_summary["vehicle_capacity"] = "available" if nominal_kwh is not None else "unavailable"
        if proto.has_data:
            source_summary["protocol"] = "available"

    report["rule_version"] = RULE_VERSION
    return report


def _indicator_item(
    *,
    code: str,
    status: str,
    value: object,
    unit: str | None,
    reference: str | None,
    reason_code: str | None = None,
    probe_no: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code,
        "status": status,
        "value": value,
        "reported_value": None,
        "reported_language": None,
        "unit": unit,
        "reference": reference,
        "reason_code": reason_code,
    }
    if probe_no is not None:
        item["probe_no"] = probe_no
    return item


def _build_temp_delta_indicator(delta_t: float | None, *, has_telemetry: bool) -> dict[str, Any]:
    if delta_t is None:
        return _indicator_item(
            code="battery_temp_delta",
            status="unavailable",
            value=None,
            unit="°C",
            reference="<= 5",
            reason_code="TEMP_MIN_DATA_MISSING" if has_telemetry else "TELEMETRY_DATA_MISSING",
        )
    return _indicator_item(
        code="battery_temp_delta",
        status="abnormal" if delta_t > 5.0 else "normal",
        value=delta_t,
        unit="°C",
        reference="<= 5",
    )


def _build_temp_rise_rate_indicator(rate: float | None) -> dict[str, Any]:
    if rate is None:
        return _indicator_item(
            code="temp_rise_rate",
            status="unavailable",
            value=None,
            unit="°C/min",
            reference="<= 0.3",
            reason_code="TEMP_SERIES_INSUFFICIENT",
        )
    return _indicator_item(
        code="temp_rise_rate",
        status="abnormal" if rate > 0.3 else "normal",
        value=rate,
        unit="°C/min",
        reference="<= 0.3",
    )


def _build_max_temp_indicator(max_temp: float | None) -> dict[str, Any]:
    if max_temp is None:
        return _indicator_item(
            code="battery_max_temp",
            status="unavailable",
            value=None,
            unit="°C",
            reference="<= 45 (safe <= 55)",
            reason_code="TEMP_DATA_MISSING",
        )
    status = "abnormal" if max_temp > 55.0 else "attention" if max_temp > 45.0 else "normal"
    return _indicator_item(
        code="battery_max_temp",
        status=status,
        value=max_temp,
        unit="°C",
        reference="<= 45 (safe <= 55)",
    )


def _build_soc_change_rate_indicator(rate: float | None, jumps: int | None) -> dict[str, Any]:
    if rate is None:
        return _indicator_item(
            code="soc_change_rate",
            status="unavailable",
            value=None,
            unit="%/min",
            reference="<= 5 (15s jump < 3%)",
            reason_code="SOC_SERIES_INSUFFICIENT",
        )
    abnormal = rate > 5.0 or (jumps is not None and jumps > 0)
    return _indicator_item(
        code="soc_change_rate",
        status="abnormal" if abnormal else "normal",
        value=rate,
        unit="%/min",
        reference="<= 5 (15s jump < 3%)",
    )


def _build_cell_voltage_delta_indicator(delta_mv: float | None) -> dict[str, Any]:
    if delta_mv is None:
        return _indicator_item(
            code="cell_voltage_delta",
            status="unavailable",
            value=None,
            unit="mV",
            reference="<= 60 (pass <= 150)",
            reason_code="CELL_VOLTAGE_DATA_MISSING",
        )
    return _indicator_item(
        code="cell_voltage_delta",
        status="abnormal" if delta_mv > 150.0 else "normal",
        value=delta_mv,
        unit="mV",
        reference="<= 60 (pass <= 150)",
    )


def _build_cell_max_voltage_indicator(
    max_v: float | None,
    battery_type: str | None,
    probe_no: int | None,
) -> dict[str, Any]:
    if battery_type == BATTERY_TYPE_LFP:
        reference = "<= 3.65"
    elif battery_type == BATTERY_TYPE_NCM:
        reference = "<= 4.20"
    else:
        reference = "<= 3.65 (LFP) / <= 4.20 (NCM)"

    if max_v is None:
        return _indicator_item(
            code="cell_max_voltage",
            status="unavailable",
            value=None,
            unit="V",
            reference=reference,
            reason_code="CELL_VOLTAGE_DATA_MISSING",
        )

    if battery_type == BATTERY_TYPE_LFP:
        status = "abnormal" if max_v > 3.70 else "attention" if max_v > 3.65 else "normal"
        return _indicator_item(
            code="cell_max_voltage",
            status=status,
            value=max_v,
            unit="V",
            reference=reference,
            probe_no=probe_no,
        )
    if battery_type == BATTERY_TYPE_NCM:
        status = "abnormal" if max_v > 4.25 else "attention" if max_v > 4.20 else "normal"
        return _indicator_item(
            code="cell_max_voltage",
            status=status,
            value=max_v,
            unit="V",
            reference=reference,
            probe_no=probe_no,
        )
    # 电池化学类型未知时：<=3.65V 在 LFP/NCM 下均安全，>4.25V 均过压，中间区间诚实标 unavailable
    if max_v <= 3.65:
        return _indicator_item(
            code="cell_max_voltage",
            status="normal",
            value=max_v,
            unit="V",
            reference=reference,
            probe_no=probe_no,
        )
    if max_v > 4.25:
        return _indicator_item(
            code="cell_max_voltage",
            status="abnormal",
            value=max_v,
            unit="V",
            reference=reference,
            probe_no=probe_no,
        )
    return _indicator_item(
        code="cell_max_voltage",
        status="unavailable",
        value=max_v,
        unit="V",
        reference=reference,
        reason_code="BATTERY_TYPE_UNKNOWN",
        probe_no=probe_no,
    )


def _build_conclusion(
    indicators: list[dict[str, Any]],
    *,
    is_shallow_charge: bool,
    language: str,
) -> dict[str, Any]:
    counts = {"normal": 0, "attention": 0, "abnormal": 0, "unavailable": 0}
    triggered: list[str] = []
    for item in indicators:
        st = str(item.get("status") or "unavailable")
        counts[st if st in counts else "unavailable"] += 1
        if st in {"attention", "abnormal"} and item.get("code"):
            triggered.append(str(item["code"]))

    assessed = counts["normal"] + counts["attention"] + counts["abnormal"]
    if counts["abnormal"] > 0:
        level = "abnormal"
        code = "ABNORMAL_DETECTED"
        headline = health_conclusion_headline(language, "abnormal", abnormal=counts["abnormal"])
    elif counts["attention"] > 0:
        level = "attention"
        code = "ATTENTION_REQUIRED"
        headline = health_conclusion_headline(language, "attention", attention=counts["attention"])
    elif assessed > 0:
        level = "normal"
        code = "ALL_NORMAL"
        if counts["unavailable"] > 0:
            headline = health_conclusion_headline(
                language,
                "normal_partial",
                assessed=assessed,
                unavailable=counts["unavailable"],
            )
        else:
            headline = health_conclusion_headline(language, "normal")
    else:
        level = "insufficient"
        code = "INSUFFICIENT_DATA"
        headline = health_conclusion_headline(language, "insufficient")

    advice_codes: list[str] = []
    triggered_set = set(triggered)
    if triggered_set & {"battery_temp_delta", "temp_rise_rate", "battery_max_temp"}:
        advice_codes.append("thermal")
    if triggered_set & {"cell_voltage_delta", "cell_max_voltage"}:
        advice_codes.append("voltage")
    if "soc_change_rate" in triggered_set:
        advice_codes.append("soc")
    if "stop_reason" in triggered_set:
        advice_codes.append("stop_reason")
    if is_shallow_charge:
        advice_codes.append("shallow_charge")

    parts = [headline, *(health_advice(language, key) for key in advice_codes)]
    joiner = " " if language in NON_CHINESE_LANGUAGES else ""
    return {
        "level": level,
        "code": code,
        "text": joiner.join(parts),
        "counts": counts,
        "triggered_codes": triggered,
        "advice_codes": advice_codes,
    }


def _is_pre_handshake_zero_row(row: Mapping[str, Any]) -> bool:
    """识别 41 实测的握手前全零遥测行（I=0, soc=0, E=0，常见 T=0/-50 哨兵）。"""
    soc = _number(row.get("soc"))
    current = _number(row.get("outputCurrent"))
    energy = _number(row.get("chargingElectricityQuantity"))
    return (
        (soc is None or soc == 0.0)
        and (current is None or current == 0.0)
        and (energy is None or energy == 0.0)
    )


def _extract_temperature_series(
    samples: list[tuple[float | None, Mapping[str, Any]]],
    proto: YkcProtocolEvidence,
) -> tuple[list[tuple[float | None, float]], list[float]]:
    if proto.temp_points:
        max_series = [(pt.ts_seconds, pt.max_temp_c) for pt in proto.temp_points if pt.max_temp_c is not None]
        deltas = [
            pt.max_temp_c - pt.min_temp_c
            for pt in proto.temp_points
            if pt.max_temp_c is not None and pt.min_temp_c is not None and pt.max_temp_c >= pt.min_temp_c
        ]
        if max_series:
            return max_series, deltas

    max_series = []
    deltas = []
    for ts, row in samples:
        if _is_pre_handshake_zero_row(row):
            continue
        t_max = _valid_temp(row.get("batteryMaxTemperature"))
        t_min = _valid_temp(row.get("batteryMinTemperature"))
        if t_max is not None:
            max_series.append((ts, t_max))
        if t_max is not None and t_min is not None and t_max >= t_min:
            deltas.append(t_max - t_min)
    return max_series, deltas


def _extract_soc_series(
    samples: list[tuple[float | None, Mapping[str, Any]]],
    proto: YkcProtocolEvidence,
) -> list[tuple[float | None, float]]:
    sample_soc: list[tuple[float | None, float]] = []
    for ts, row in samples:
        if _is_pre_handshake_zero_row(row):
            continue
        val = _number(row.get("soc"))
        if val is not None and 0.0 <= val <= 100.0:
            sample_soc.append((ts, val))
    # 剔除握手前/停充后偶发的 soc=0 边界点（仅当存在有效正 SOC 时）
    sample_soc = _trim_boundary_zero_soc(sample_soc)
    if sample_soc and any(v > 0 for _, v in sample_soc):
        return sample_soc
    proto_soc = _trim_boundary_zero_soc(
        [(pt.ts_seconds, pt.soc) for pt in proto.voltage_points if pt.soc is not None]
    )
    if proto_soc and any(v > 0 for _, v in proto_soc):
        return proto_soc
    return []


def _trim_boundary_zero_soc(
    series: list[tuple[float | None, float]],
) -> list[tuple[float | None, float]]:
    if not series or not any(v > 0 for _, v in series):
        return series
    start = 0
    while start < len(series) and series[start][1] == 0.0:
        start += 1
    end = len(series)
    while end > start and series[end - 1][1] == 0.0:
        end -= 1
    return series[start:end]


def _sliding_window_max_rise_rate(
    series: list[tuple[float | None, float]],
    *,
    window_seconds: float,
    min_span_seconds: float,
    allow_negative_abs: bool = False,
) -> float | None:
    timed = [(ts, val) for ts, val in series if ts is not None]
    if len(timed) < 2:
        return None
    timed.sort(key=lambda item: item[0])
    total_span = timed[-1][0] - timed[0][0]
    if total_span < min_span_seconds:
        return None

    def _rate(v1: float, v2: float, dt: float) -> float:
        diff = abs(v2 - v1) if allow_negative_abs else max(0.0, v2 - v1)
        return diff / (dt / 60.0)

    if total_span < window_seconds:
        return round(_rate(timed[0][1], timed[-1][1], total_span), 2)

    max_rate = 0.0
    left = 0
    for right in range(1, len(timed)):
        t_right, v_right = timed[right]
        while left + 1 < right and (t_right - timed[left + 1][0]) >= window_seconds:
            left += 1
        dt = t_right - timed[left][0]
        if dt >= window_seconds:
            rate = _rate(timed[left][1], v_right, dt)
            if rate > max_rate:
                max_rate = rate
    return round(max_rate, 2)


def _count_soc_jumps(series: list[tuple[float | None, float]]) -> int | None:
    timed = [(ts, val) for ts, val in series if ts is not None]
    if len(timed) < 2:
        return None
    timed.sort(key=lambda item: item[0])
    jumps = 0
    last_jump_ts: float | None = None
    left = 0
    for right in range(1, len(timed)):
        t_right, v_right = timed[right]
        while left < right and (t_right - timed[left][0]) > SOC_JUMP_WINDOW_SECONDS:
            left += 1
        for idx in range(left, right):
            dt = t_right - timed[idx][0]
            if 0.0 < dt <= SOC_JUMP_WINDOW_SECONDS and abs(v_right - timed[idx][1]) >= SOC_JUMP_THRESHOLD_PCT:
                if last_jump_ts is None or (t_right - last_jump_ts) > SOC_JUMP_WINDOW_SECONDS:
                    jumps += 1
                    last_jump_ts = t_right
                break
    return jumps


def _resolve_soc_span(
    soc_series: list[tuple[float | None, float]],
    order: Mapping[str, Any],
    proto: YkcProtocolEvidence,
) -> tuple[float | None, float | None, float | None]:
    if len(soc_series) >= 2:
        start = round(soc_series[0][1], 1)
        end = round(soc_series[-1][1], 1)
        return start, end, round(end - start, 1)
    start_ord = _number(order.get("start_soc"))
    end_ord = _number(order.get("end_soc"))
    if end_ord is None and proto.end_soc is not None:
        end_ord = proto.end_soc
    if start_ord is not None and end_ord is not None and 0 <= start_ord <= 100 and 0 <= end_ord <= 100:
        return round(start_ord, 1), round(end_ord, 1), round(end_ord - start_ord, 1)
    return (
        round(start_ord, 1) if start_ord is not None else None,
        round(end_ord, 1) if end_ord is not None else None,
        None,
    )


def _resolve_energy_delta(
    samples: list[tuple[float | None, Mapping[str, Any]]],
    order: Mapping[str, Any],
) -> float | None:
    energies = [
        val
        for _, row in samples
        if (val := _number(row.get("chargingElectricityQuantity"))) is not None and val >= 0
    ]
    if len(energies) >= 2:
        delta = energies[-1] - energies[0]
        if delta > 0:
            return round(delta, 3)
    order_elec = _number(order.get("electricity"))
    if order_elec is not None and order_elec > 0:
        return round(order_elec, 3)
    return None


def _resolve_nominal_capacity(
    order: Mapping[str, Any],
    proto: YkcProtocolEvidence,
) -> tuple[float | None, str | None]:
    order_nominal = _number(order.get("nominal_capacity_kwh"))
    if order_nominal is not None and order_nominal > 0:
        return round(order_nominal, 2), "order"
    if proto.nominal_energy_kwh is not None and proto.nominal_energy_kwh > 0:
        return round(proto.nominal_energy_kwh, 2), "ykc_0x17"
    return None, None


def _resolve_cell_voltage_deltas(
    samples: list[tuple[float | None, Mapping[str, Any]]],
    proto: YkcProtocolEvidence,
    cell_series_count: int | None,
) -> tuple[float | None, float | None, float | None]:
    end_mv: float | None = None
    if (
        proto.end_max_cell_voltage_v is not None
        and proto.end_min_cell_voltage_v is not None
        and proto.end_max_cell_voltage_v >= proto.end_min_cell_voltage_v
    ):
        end_mv = round((proto.end_max_cell_voltage_v - proto.end_min_cell_voltage_v) * 1000.0, 1)

    process_mv: float | None = None
    if cell_series_count and cell_series_count > 0 and proto.voltage_points:
        process_vals: list[float] = []
        for pt in proto.voltage_points:
            if pt.max_cell_voltage_v is not None and pt.bms_measured_voltage_v is not None:
                diff_v = 2.0 * (pt.max_cell_voltage_v - pt.bms_measured_voltage_v / cell_series_count)
                if diff_v >= 0:
                    process_vals.append(diff_v * 1000.0)
        if process_vals:
            process_mv = round(max(process_vals), 1)

    sample_mv: float | None = None
    explicit_deltas: list[float] = []
    for _, row in samples:
        c_max = _number(row.get("cellMaxVoltage"))
        c_min = _number(row.get("cellMinVoltage"))
        if c_max is not None and c_min is not None and c_max >= c_min:
            # 既兼容 mV（如 4050）也兼容 V（如 4.05）
            scale = 1000.0 if c_max <= 10.0 else 1.0
            explicit_deltas.append((c_max - c_min) * scale)
    if explicit_deltas:
        sample_mv = round(max(explicit_deltas), 1)
    else:
        # 兼容旧测试夹具仅提供 cellMaxVoltage（mV）序列的写法
        max_only = [val for _, row in samples if (val := _number(row.get("cellMaxVoltage"))) is not None]
        if len(max_only) >= 2:
            scale = 1000.0 if max(max_only) <= 10.0 else 1.0
            sample_mv = round((max(max_only) - min(max_only)) * scale, 1)

    return end_mv, process_mv, sample_mv


def _resolve_max_cell_voltage(
    samples: list[tuple[float | None, Mapping[str, Any]]],
    proto: YkcProtocolEvidence,
) -> tuple[float | None, int | None]:
    max_v: float | None = None
    peak_ts: float | None = None

    for pt in proto.voltage_points:
        if pt.max_cell_voltage_v is not None and (max_v is None or pt.max_cell_voltage_v > max_v):
            max_v = pt.max_cell_voltage_v
            peak_ts = pt.ts_seconds

    if max_v is None and proto.end_max_cell_voltage_v is not None:
        max_v = proto.end_max_cell_voltage_v

    if max_v is None:
        raw_vals = [val for _, row in samples if (val := _number(row.get("cellMaxVoltage"))) is not None]
        if raw_vals:
            peak = max(raw_vals)
            v = peak / 1000.0 if peak > 10.0 else peak
            if 0.5 <= v <= 6.0:
                max_v = round(v, 3)

    probe_no: int | None = None
    if max_v is not None and proto.temp_points:
        with_probe = [pt for pt in proto.temp_points if pt.max_cell_probe_no is not None]
        if with_probe:
            if peak_ts is not None:
                nearest = min(with_probe, key=lambda pt: abs(pt.ts_seconds - peak_ts))
                probe_no = nearest.max_cell_probe_no
            else:
                probe_no = with_probe[-1].max_cell_probe_no

    return (round(max_v, 3) if max_v is not None else None), probe_no


def _resolve_duration_seconds(order: Mapping[str, Any], report: Mapping[str, Any]) -> int | None:
    window = report.get("order_window")
    if isinstance(window, Mapping):
        started = as_datetime(window.get("started_at"))
        stopped = as_datetime(window.get("stopped_at"))
        if started is not None and stopped is not None and stopped > started:
            return int((stopped - started).total_seconds())
    started = as_datetime(order.get("created_time"))
    stopped = as_datetime(order.get("stop_time"))
    if started is not None and stopped is not None and stopped > started:
        return int((stopped - started).total_seconds())
    return None


def _sort_samples(samples: list[Mapping[str, Any]]) -> list[tuple[float | None, Mapping[str, Any]]]:
    parsed = [(_parse_ts_seconds(row.get("_ts")), row) for row in samples]
    if all(ts is not None for ts, _ in parsed):
        parsed.sort(key=lambda item: item[0] or 0.0)
    return parsed


def _valid_temp(value: Any) -> float | None:
    num = _number(value)
    if num is None or num == -50.0 or not (-40.0 <= num <= 120.0):
        return None
    return num


def _normalize_battery_type(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {BATTERY_TYPE_LFP, "03", "0x03", "磷酸铁锂"}:
        return BATTERY_TYPE_LFP
    if text in {BATTERY_TYPE_NCM, "06", "0x06", "三元锂"}:
        return BATTERY_TYPE_NCM
    return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed > 0 else None


def _parse_ts_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000.0 if abs(numeric) > 1e11 else numeric
    dt = as_datetime(value)
    if dt is not None:
        return dt.timestamp()
    try:
        numeric = float(value)
        return numeric / 1000.0 if abs(numeric) > 1e11 else numeric
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
