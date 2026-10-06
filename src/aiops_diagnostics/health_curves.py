from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aiops_diagnostics.health_report_copy import curve_name
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE

MAX_CURVE_POINTS = 300


def build_curves(
    samples: Iterable[Mapping[str, Any]],
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    rows = sorted(
        (row for row in samples if _number(row.get("_ts")) is not None),
        key=lambda row: _number(row.get("_ts")) or 0,
    )
    original_points = len(rows)
    curves = {
        "power": _curve_group(
            rows,
            (
                ("power", curve_name(language, "power"), "kW"),
                ("outputCurrent", curve_name(language, "outputCurrent"), "A"),
            ),
        ),
        "voltage": _curve_group(rows, (("outputVoltage", curve_name(language, "outputVoltage"), "V"),)),
        "temperature": _curve_group(
            rows,
            (
                ("temperature", curve_name(language, "temperature"), "degC"),
                ("batteryMaxTemperature", curve_name(language, "batteryMaxTemperature"), "degC"),
                ("batteryMinTemperature", curve_name(language, "batteryMinTemperature"), "degC"),
            ),
        ),
    }
    for group in curves.values():
        group["original_points"] = original_points
    return curves


def _curve_group(rows: list[Mapping[str, Any]], series: tuple[tuple[str, str, str], ...]) -> dict[str, Any]:
    result: dict[str, Any] = {"series": [], "sample_range": None}
    for field, name, unit in series:
        points = [(_number(row.get("_ts")), _number(row.get(field))) for row in rows]
        points = [(x, y) for x, y in points if x is not None and y is not None]
        if not points:
            continue
        sampled = _sample(points)
        result["series"].append({"name": name, "field": field, "unit": unit, "points": sampled})
        result["sample_range"] = [points[0][0], points[-1][0]]
    result["output_points"] = max((len(item["points"]) for item in result["series"]), default=0)
    return result


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


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
