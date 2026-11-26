from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.common.scalar_rules import as_datetime
from aiops_diagnostics.health.health_report_copy import curve_name
from aiops_diagnostics.health.ykc_frames import YkcProtocolEvidence

MAX_CURVE_POINTS = 300


def build_curves(
    samples: Iterable[Mapping[str, Any]],
    language: str = DEFAULT_LANGUAGE,
    *,
    protocol: YkcProtocolEvidence | None = None,
) -> dict[str, Any]:
    parsed_rows = [(ts, row) for row in samples if (ts := _parse_ts(row.get("_ts"))) is not None]
    parsed_rows.sort(key=lambda item: item[0])
    original_points = len(parsed_rows)

    power_group = _curve_group(
        parsed_rows,
        (
            ("power", curve_name(language, "power"), "kW"),
            ("outputCurrent", curve_name(language, "outputCurrent"), "A"),
        ),
    )
    power_group["original_points"] = original_points

    voltage_group = _curve_group(
        parsed_rows,
        (("outputVoltage", curve_name(language, "outputVoltage"), "V"),),
    )
    voltage_original = original_points
    if protocol and protocol.voltage_points:
        voltage_original = max(voltage_original, len(protocol.voltage_points))
        _append_series(
            voltage_group,
            field="bmsMeasuredVoltage",
            name=curve_name(language, "bmsMeasuredVoltage"),
            unit="V",
            points=[
                (pt.ts_seconds, pt.bms_measured_voltage_v)
                for pt in protocol.voltage_points
                if pt.bms_measured_voltage_v is not None
            ],
        )
        _append_series(
            voltage_group,
            field="maxCellVoltage",
            name=curve_name(language, "maxCellVoltage"),
            unit="V",
            points=[
                (pt.ts_seconds, pt.max_cell_voltage_v)
                for pt in protocol.voltage_points
                if pt.max_cell_voltage_v is not None
            ],
        )
    voltage_group["original_points"] = voltage_original

    temperature_group = _build_temperature_group(parsed_rows, language, protocol)

    return {
        "power": power_group,
        "voltage": voltage_group,
        "temperature": temperature_group,
    }


def _build_temperature_group(
    parsed_rows: list[tuple[float, Mapping[str, Any]]],
    language: str,
    protocol: YkcProtocolEvidence | None,
) -> dict[str, Any]:
    group: dict[str, Any] = {"series": [], "sample_range": None, "output_points": 0}
    original_points = len(parsed_rows)

    if protocol and protocol.temp_points:
        original_points = max(original_points, len(protocol.temp_points))
        max_pts = [(pt.ts_seconds, pt.max_temp_c) for pt in protocol.temp_points if pt.max_temp_c is not None]
        min_pts = [(pt.ts_seconds, pt.min_temp_c) for pt in protocol.temp_points if pt.min_temp_c is not None]
        delta_pts = [
            (pt.ts_seconds, round(pt.max_temp_c - pt.min_temp_c, 2))
            for pt in protocol.temp_points
            if pt.max_temp_c is not None and pt.min_temp_c is not None and pt.max_temp_c >= pt.min_temp_c
        ]
        _append_series(
            group,
            field="batteryMaxTemperature",
            name=curve_name(language, "batteryMaxTemperature"),
            unit="degC",
            points=max_pts,
        )
        _append_series(
            group,
            field="batteryMinTemperature",
            name=curve_name(language, "batteryMinTemperature"),
            unit="degC",
            points=min_pts,
        )
        _append_series(
            group,
            field="batteryTempDelta",
            name=curve_name(language, "batteryTempDelta"),
            unit="degC",
            points=delta_pts,
        )
        group["original_points"] = original_points
        return group

    for field in ("temperature", "batteryMaxTemperature", "batteryMinTemperature"):
        pts = [(ts, val) for ts, row in parsed_rows if (val := _valid_temp(row.get(field))) is not None]
        _append_series(group, field=field, name=curve_name(language, field), unit="degC", points=pts)

    delta_pts = []
    for ts, row in parsed_rows:
        t_max = _valid_temp(row.get("batteryMaxTemperature"))
        t_min = _valid_temp(row.get("batteryMinTemperature"))
        if t_max is not None and t_min is not None and t_max >= t_min:
            delta_pts.append((ts, round(t_max - t_min, 2)))
    _append_series(
        group,
        field="batteryTempDelta",
        name=curve_name(language, "batteryTempDelta"),
        unit="degC",
        points=delta_pts,
    )
    group["original_points"] = original_points
    return group


def _curve_group(
    rows: list[tuple[float, Mapping[str, Any]]],
    series: tuple[tuple[str, str, str], ...],
) -> dict[str, Any]:
    result: dict[str, Any] = {"series": [], "sample_range": None, "output_points": 0}
    for field, name, unit in series:
        points = [(ts, val) for ts, row in rows if (val := _number(row.get(field))) is not None]
        _append_series(result, field=field, name=name, unit=unit, points=points)
    return result


def _append_series(
    result: dict[str, Any],
    *,
    field: str,
    name: str,
    unit: str,
    points: list[tuple[float, float]],
) -> None:
    if not points:
        return
    sampled = _sample(points)
    result["series"].append({"name": name, "field": field, "unit": unit, "points": sampled})
    start_x, end_x = points[0][0], points[-1][0]
    existing = result.get("sample_range")
    if existing is None:
        result["sample_range"] = [start_x, end_x]
    else:
        result["sample_range"] = [min(existing[0], start_x), max(existing[1], end_x)]
    result["output_points"] = max(int(result.get("output_points") or 0), len(sampled))


def _sample(points: list[tuple[float, float]]) -> list[list[float]]:
    if len(points) <= MAX_CURVE_POINTS:
        return [[x, y] for x, y in points]
    indexes = {0, len(points) - 1}
    indexes.update(sorted(range(len(points)), key=lambda i: points[i][1])[:2])
    indexes.update(sorted(range(len(points)), key=lambda i: points[i][1], reverse=True)[:2])
    remaining = MAX_CURVE_POINTS - len(indexes)
    indexes.update(round(i * (len(points) - 1) / (remaining + 1)) for i in range(1, remaining + 1))
    if len(indexes) < MAX_CURVE_POINTS:
        for index in range(len(points)):
            if index not in indexes:
                indexes.add(index)
                if len(indexes) == MAX_CURVE_POINTS:
                    break
    return [[points[i][0], points[i][1]] for i in sorted(indexes)]


def _valid_temp(value: Any) -> float | None:
    num = _number(value)
    if num is None or num == -50.0 or not (-40.0 <= num <= 120.0):
        return None
    return num


def _parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    numeric = _number(value)
    if numeric is not None:
        return numeric
    dt = as_datetime(value)
    return dt.timestamp() if dt is not None else None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
