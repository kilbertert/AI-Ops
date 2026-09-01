from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RULE_VERSION = "health-v2"


def score_temperature_balance(delta: float) -> float:
    if delta <= 2:
        return 100.0
    return max(0.0, 100.0 - (delta - 2) * 6 if delta <= 5 else 82.0 - (delta - 5) * 10)


def score_max_temperature(value: float) -> float:
    if value <= 35:
        return 100.0
    return max(0.0, 100.0 - (value - 35) * 1.8 if value <= 45 else 82.0 - (value - 45) * 5)


def score_capacity(soh: float | None) -> float | None:
    if soh is None:
        return None
    if soh >= 95:
        return 100.0
    if soh >= 80:
        return 80.0 + (soh - 80) / 15 * 20
    if soh >= 70:
        return 60.0 + (soh - 70) / 10 * 20
    return max(0.0, soh / 70 * 60)


def score_voltage_balance(delta_mv: float) -> float:
    if delta_mv <= 30:
        return 100.0
    return max(0.0, 100.0 - (delta_mv - 30) * 0.36 if delta_mv <= 80 else 82.0 - (delta_mv - 80) * 0.45)


def score_soc_balance(jumps: int, alarms: int) -> float:
    return max(0.0, 100.0 - jumps * 15 - alarms * 30)


def calculate_soh(
    energy_kwh: float, soc_delta: float, nominal_kwh: float | None, efficiency: float = 0.94
) -> float | None:
    if nominal_kwh is None or nominal_kwh <= 0 or soc_delta < 10 or energy_kwh < 0:
        return None
    value = energy_kwh / (soc_delta / 100) * efficiency / nominal_kwh * 100
    return value if 0 <= value <= 200 else None


def health_scores(
    *,
    delta_temperature: float | None,
    max_temperature: float | None,
    soh: float | None,
    delta_voltage_mv: float | None,
    soc_jumps: int | None,
    alarms: int | None,
) -> list[dict[str, Any]]:
    values = [
        (
            "temperature_balance",
            score_temperature_balance(delta_temperature) if delta_temperature is not None else None,
        ),
        ("max_temperature", score_max_temperature(max_temperature) if max_temperature is not None else None),
        ("capacity", score_capacity(soh)),
        (
            "voltage_balance",
            score_voltage_balance(delta_voltage_mv) if delta_voltage_mv is not None else None,
        ),
        ("soc_balance", score_soc_balance(soc_jumps, alarms or 0) if soc_jumps is not None else None),
    ]
    return [
        {
            "code": code,
            "score": score,
            "status": "unavailable"
            if score is None
            else "normal"
            if score >= 80
            else "attention"
            if score >= 60
            else "abnormal",
        }
        for code, score in values
    ]


def enrich_report(
    report: dict[str, Any], samples: list[Mapping[str, Any]], order: Mapping[str, Any]
) -> dict[str, Any]:
    temperatures = [_number(row.get("batteryMaxTemperature")) for row in samples]
    temperatures = [value for value in temperatures if value is not None]
    minimums = [_number(row.get("batteryMinTemperature")) for row in samples]
    minimums = [value for value in minimums if value is not None]
    soc = [_number(row.get("soc")) for row in samples]
    soc = [value for value in soc if value is not None]
    energy = [_number(row.get("chargingElectricityQuantity")) for row in samples]
    energy = [value for value in energy if value is not None]
    voltages = [_number(row.get("cellMaxVoltage")) for row in samples]
    voltages = [value for value in voltages if value is not None]
    delta_t = max(temperatures) - min(minimums) if temperatures and minimums else None
    delta_v = max(voltages) - min(voltages) if voltages else None
    soc_delta = soc[-1] - soc[0] if len(soc) >= 2 else None
    energy_delta = energy[-1] - energy[0] if len(energy) >= 2 else None
    nominal = _number(order.get("nominal_capacity_kwh"))
    soh = (
        calculate_soh(energy_delta, soc_delta, nominal)
        if energy_delta is not None and soc_delta is not None
        else None
    )
    report["radar"] = health_scores(
        delta_temperature=delta_t,
        max_temperature=max(temperatures) if temperatures else None,
        soh=soh,
        delta_voltage_mv=delta_v,
        soc_jumps=None,
        alarms=None,
    )
    report["health_metrics"] = {
        "temperature_delta": delta_t,
        "soc_delta": soc_delta,
        "energy_charged_kwh": energy_delta,
        "soh": soh,
        "soh_status": "unavailable" if soh is None else "normal",
    }
    report["rule_version"] = RULE_VERSION
    return report


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
