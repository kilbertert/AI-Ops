"""Health-report curves and source summary (T4 #89).

This module is the only place the standard charging health report talks
about curves. It consumes a full ``DiagnosticSources`` so the upstream
calculation uses the *complete* telemetry, but the API surface is bounded:

- every curve has at most ``MAX_CURVE_POINTS`` output points while the
  first, last and extrema are always preserved;
- each curve records its original input point count and the time range
  of the source data, so consumers can verify the downsampling is honest;
- the returned payload deliberately excludes VIN, raw protocol frames,
  SQL, evidence files, provider, fixture, workspace and run identifiers
  to keep the standard API consumer-neutral.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

MAX_CURVE_POINTS = 300


class _Source(Protocol):
    def get_gun_samples(
        self,
        device: str,
        start_time: datetime,
        end_time: datetime,
        tx_serial_no: str | None,
    ) -> list[dict[str, Any]]: ...


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    return None


def downsample(
    points: list[tuple[float, float]], *, limit: int = MAX_CURVE_POINTS
) -> list[tuple[float, float]]:
    """Bounded downsampling that keeps the first, last, and extrema points.

    ``points`` is a time-ordered list of ``(t, v)`` tuples. The output is
    sorted by ``t`` and never exceeds ``limit`` entries. Time is in
    minutes-from-window-start so consumers can render without doing
    timezone math themselves.
    """
    if not points:
        return []
    if len(points) <= limit:
        return list(points)
    # dedupe by timestamp while preserving first occurrence order
    seen: set[float] = set()
    unique: list[tuple[float, float]] = []
    for t, v in points:
        if t in seen:
            continue
        seen.add(t)
        unique.append((t, v))
    if len(unique) <= limit:
        return unique

    first = unique[0]
    last = unique[-1]
    # extrema = max value; if multiple share the max, prefer the first occurrence
    max_value = max(item[1] for item in unique)
    extrema = next(item for item in unique if item[1] == max_value)
    min_value = min(item[1] for item in unique)
    minimum = next(item for item in unique if item[1] == min_value)

    # Reserve four slots for first / last / max / min and pick evenly
    # between them. Duplicate removal is handled by callers (series-level).
    body_budget = max(0, limit - 4)
    if body_budget == 0:
        seen_keys: set[tuple[float, float]] = set()
        result: list[tuple[float, float]] = []
        for item in [first, last, extrema, minimum]:
            if item in seen_keys:
                continue
            seen_keys.add(item)
            result.append(item)
        result.sort(key=lambda item: item[0])
        return result[:limit]

    step = (len(unique) - 1) / (body_budget + 1)
    sampled_indices: list[int] = [0, int(round(step))] if body_budget >= 2 else []
    for k in range(2, body_budget + 1):
        sampled_indices.append(int(round(k * step)))
    sampled: list[tuple[float, float]] = []
    for idx in sampled_indices:
        if 0 <= idx < len(unique):
            sampled.append(unique[idx])

    # pin extrema and last
    seen_keys = set()
    result = []
    for item in [first, *sampled, last, extrema, minimum]:
        if item in seen_keys:
            continue
        seen_keys.add(item)
        result.append(item)
    result.sort(key=lambda item: item[0])
    return result[:limit]


def _series(
    *,
    name: str,
    points_min_v: list[tuple[float, float]],
    original_points: int,
    output_points: int,
    start: datetime | None,
    end: datetime | None,
    unit: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "unit": unit,
        "points": points_min_v,
        "original_points": original_points,
        "output_points": output_points,
        "sample_range": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
    }


def _empty_curve(unit_y: str) -> dict[str, Any]:
    return {
        "unit_x": "min",
        "unit_y": unit_y,
        "series": [],
    }


def _rows_to_minutes(
    rows: list[dict[str, Any]],
    field: str,
    *,
    started_at: datetime,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows:
        ts = _parse_ts(row.get("_ts"))
        value = row.get(field)
        if ts is None or value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric != numeric:  # NaN guard
            continue
        delta = (ts - started_at).total_seconds() / 60.0
        out.append((round(delta, 3), numeric))
    out.sort(key=lambda item: item[0])
    return out


def _temperature_delta_series(
    rows: list[dict[str, Any]],
    *,
    started_at: datetime,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows:
        ts = _parse_ts(row.get("_ts"))
        max_v = row.get("batteryMaxTemperature")
        min_v = row.get("batteryMinTemperature")
        if ts is None or max_v is None or min_v is None:
            continue
        try:
            delta = (ts - started_at).total_seconds() / 60.0
            out.append((round(delta, 3), float(max_v) - float(min_v)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda item: item[0])
    return out


def build_curves_and_summary(
    *,
    sources: _Source,
    order_no: str,
    device: str,
    started_at: datetime,
    stopped_at: datetime,
    safety_max_window_hours: int,
    order_status: int | None,
) -> dict[str, Any]:
    """Build the curves + source summary that ride alongside the #87 report.

    Raises ``ValueError`` if the order window exceeds the server safety limit;
    identity / authorization / missing order is the caller's responsibility.
    """
    if safety_max_window_hours <= 0:
        raise ValueError("safety_max_window_hours must be positive")
    delta_hours = (stopped_at - started_at).total_seconds() / 3600.0
    if delta_hours > safety_max_window_hours:
        raise ValueError(
            f"order window {delta_hours:.1f}h exceeds the {safety_max_window_hours}h safety limit"
        )
    if delta_hours < 0:
        raise ValueError("order window is negative")

    rows: list[dict[str, Any]] = []
    if order_status is not None and int(order_status) != 1:
        # Order is not yet finished. We do not pull telemetry; downstream
        # rendering shows empty curves plus a warning.
        pass
    else:
        rows = sources.get_gun_samples(device, started_at, stopped_at, None)

    warnings: list[str] = []
    if order_status is not None and int(order_status) != 1:
        warnings.append("order")
    if not rows:
        warnings.append("telemetry")

    power_curves: dict[str, Any] = _empty_curve("kW")
    voltage_curves: dict[str, Any] = _empty_curve("V")
    temperature_curves: dict[str, Any] = _empty_curve("°C")

    if rows:
        first_ts = _parse_ts(rows[0].get("_ts"))
        last_ts = _parse_ts(rows[-1].get("_ts"))

        power_points = _rows_to_minutes(rows, "power", started_at=started_at)
        current_points = _rows_to_minutes(rows, "outputCurrent", started_at=started_at)
        voltage_points = _rows_to_minutes(rows, "outputVoltage", started_at=started_at)
        max_temp_points = _rows_to_minutes(rows, "batteryMaxTemperature", started_at=started_at)
        min_temp_points = _rows_to_minutes(rows, "batteryMinTemperature", started_at=started_at)
        delta_temp_points = _temperature_delta_series(rows, started_at=started_at)

        original = len(rows)
        output = min(original, MAX_CURVE_POINTS)
        first_dt = first_ts or started_at
        last_dt = last_ts or stopped_at

        power_curves["series"] = [
            _series(
                name="需求功率",
                points_min_v=downsample(power_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="kW",
            ),
            _series(
                name="实际功率",
                points_min_v=downsample(power_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="kW",
            ),
            _series(
                name="输出电流",
                points_min_v=downsample(current_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="A",
            ),
        ]
        voltage_curves["series"] = [
            _series(
                name="需求电压",
                points_min_v=downsample(voltage_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="V",
            ),
            _series(
                name="实际电压",
                points_min_v=downsample(voltage_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="V",
            ),
        ]
        temperature_curves["series"] = [
            _series(
                name="电池最高温度",
                points_min_v=downsample(max_temp_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="°C",
            ),
            _series(
                name="电池最低温度",
                points_min_v=downsample(min_temp_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="°C",
            ),
            _series(
                name="电池实时温差",
                points_min_v=downsample(delta_temp_points),
                original_points=original,
                output_points=output,
                start=first_dt,
                end=last_dt,
                unit="°C",
            ),
        ]

    available = bool(rows)
    output_total = sum(
        len(s["points"])
        for s in power_curves["series"] + voltage_curves["series"] + temperature_curves["series"]
    )
    summary = {
        "data_as_of": datetime.now(UTC).isoformat(),
        "completeness": min(1.0, output_total / (MAX_CURVE_POINTS * 3) if available else 0.0),
        "warnings": warnings,
        "order_snapshot": {
            "order_no": order_no,
            "device": device,
            "started_at": started_at.isoformat(),
            "stopped_at": stopped_at.isoformat(),
            "order_status": order_status,
        },
        "telemetry": {
            "available": available,
            "original_points": len(rows) if available else 0,
            "output_points": output_total,
            "source": "tdengine:charging-gun_property" if available else None,
        },
        "protocol": {
            "available": False,
            "source": None,
            "note": "协议帧级数据接入见 T3 #90",
        },
        "vehicle_capacity": {
            "available": False,
            "source": None,
            "nominal_capacity_kwh": None,
            "note": "权威容量接入见 T3 #90",
        },
    }

    return {
        "curves": {
            "power": power_curves,
            "voltage": voltage_curves,
            "temperature": temperature_curves,
        },
        "source_summary": summary,
    }
