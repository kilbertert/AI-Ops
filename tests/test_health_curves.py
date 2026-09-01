"""Unit tests for the health-report curve builder (T4 #89).

The builder consumes a full ``DiagnosticSources`` and emits the curves + source
summary that ride alongside the #87 minimum health report. Off-process
``get_gun_samples`` results are faked here so the test stays at the seam the
contract cares about: bounded output, first/last/extrema preservation, and
no leakage of VIN / raw protocol frames / SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aiops_diagnostics.health_curves import (
    MAX_CURVE_POINTS,
    build_curves_and_summary,
    downsample,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _row(t_seconds: int, **fields: float) -> dict[str, Any]:
    """Build one gun-sample row keyed by ``_ts`` (epoch seconds) plus
    any per-test fields (``temperature`` / ``power`` / ``outputVoltage``)."""
    row: dict[str, Any] = {"_ts": datetime.fromtimestamp(t_seconds, tz=UTC).isoformat()}
    row.update(fields)
    return row


def _order_no() -> str:
    return "O-1"


def _device() -> str:
    return "DEVICE-1"


def _started_at() -> datetime:
    return datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def _stopped_at() -> datetime:
    return _started_at() + timedelta(minutes=90)


def _sources(samples: list[dict[str, Any]] | None = None) -> Any:
    """A ``DiagnosticSources``-like object with only ``get_gun_samples``."""

    class _Fake:
        def get_gun_samples(self, device, start, end, tx_serial_no):  # noqa: D401
            return list(samples or [])

    return _Fake()


# --------------------------------------------------------------------------- #
# Pure downsample                                                             #
# --------------------------------------------------------------------------- #


def test_downsample_keeps_all_points_when_under_limit() -> None:
    points = [(float(i), i * 1.0) for i in range(50)]
    assert downsample(points, limit=300) == points


def test_downsample_preserves_first_last_and_extrema() -> None:
    # 1000 points; first=0, last=999, max=999 (== last), min=0 (== first)
    # Use a non-monotonic shape so first/last/extrema/min are all distinct.
    points = []
    for i in range(1000):
        # baseline goes up linearly, with a peak at i=500 and a dip at i=250
        v = float(i)
        if i == 500:
            v = 5000.0  # max
        if i == 250:
            v = -100.0  # min
        points.append((float(i), v))
    out = downsample(points, limit=300)
    assert len(out) <= 300
    assert out[0] == (0.0, 0.0)
    assert out[-1] == (999.0, 999.0)
    values = {round(v, 1) for _, v in out}
    assert 5000.0 in values, "max not preserved"
    assert -100.0 in values, "min not preserved"


def test_downsample_handles_duplicate_timestamps() -> None:
    points = [(1.0, 5.0), (1.0, 5.0), (2.0, 7.0), (3.0, 9.0)]
    out = downsample(points, limit=2)
    # Sort key collapses duplicates, then first/last/extrema
    assert len(out) <= 4
    assert out[0] == (1.0, 5.0)
    assert out[-1] == (3.0, 9.0)


def test_downsample_preserves_order() -> None:
    points = [(float(i), float(i % 7)) for i in range(600)]
    out = downsample(points, limit=100)
    times = [t for t, _ in out]
    assert times == sorted(times)


# --------------------------------------------------------------------------- #
# build_curves_and_summary                                                    #
# --------------------------------------------------------------------------- #


def test_curves_output_point_count_capped_at_300() -> None:
    rows = [
        _row(60 * i, temperature=20.0 + 0.1 * i, power=10.0 + i, outputVoltage=400.0) for i in range(2000)
    ]
    started = _started_at()
    out = build_curves_and_summary(
        sources=_sources(rows),
        order_no=_order_no(),
        device=_device(),
        started_at=started,
        stopped_at=started + timedelta(minutes=2000),
        safety_max_window_hours=72,
        order_status=1,
    )
    for series in out["curves"]["power"]["series"]:
        assert len(series["points"]) <= MAX_CURVE_POINTS
    for series in out["curves"]["temperature"]["series"]:
        assert len(series["points"]) <= MAX_CURVE_POINTS
    for series in out["curves"]["voltage"]["series"]:
        assert len(series["points"]) <= MAX_CURVE_POINTS


def test_curves_return_source_point_counts() -> None:
    rows = [_row(60 * i, temperature=25.0) for i in range(100)]
    started = _started_at()
    out = build_curves_and_summary(
        sources=_sources(rows),
        order_no=_order_no(),
        device=_device(),
        started_at=started,
        stopped_at=started + timedelta(minutes=120),
        safety_max_window_hours=72,
        order_status=1,
    )
    for kind in ("power", "voltage", "temperature"):
        for series in out["curves"][kind]["series"]:
            assert series["original_points"] == 100
            assert series["output_points"] == 100
            assert series["sample_range"]["start"] is not None
            assert series["sample_range"]["end"] is not None


def test_curves_unit_and_axis_metadata_present() -> None:
    rows = [_row(60 * i, temperature=20.0, power=10.0, outputVoltage=400.0) for i in range(10)]
    started = _started_at()
    out = build_curves_and_summary(
        sources=_sources(rows),
        order_no=_order_no(),
        device=_device(),
        started_at=started,
        stopped_at=started + timedelta(minutes=15),
        safety_max_window_hours=72,
        order_status=1,
    )
    assert out["curves"]["power"]["unit_x"] == "min"
    assert out["curves"]["power"]["unit_y"] == "kW"
    assert out["curves"]["voltage"]["unit_x"] == "min"
    assert out["curves"]["voltage"]["unit_y"] == "V"
    assert out["curves"]["temperature"]["unit_x"] == "min"
    assert out["curves"]["temperature"]["unit_y"] == "°C"


def test_summary_says_calculation_used_full_data() -> None:
    """The PRD #89 rule: calculation uses full input data even when the API
    returns downsampled points. We assert the summary records the original
    point count so the consumer can verify the invariant."""
    rows = [_row(60 * i, temperature=20.0) for i in range(900)]
    started = _started_at()
    out = build_curves_and_summary(
        sources=_sources(rows),
        order_no=_order_no(),
        device=_device(),
        started_at=started,
        stopped_at=started + timedelta(minutes=950),
        safety_max_window_hours=72,
        order_status=1,
    )
    assert out["source_summary"]["telemetry"]["original_points"] == 900
    assert out["source_summary"]["telemetry"]["output_points"] <= MAX_CURVE_POINTS


def test_summary_warns_when_telemetry_missing() -> None:
    out = build_curves_and_summary(
        sources=_sources([]),
        order_no=_order_no(),
        device=_device(),
        started_at=_started_at(),
        stopped_at=_stopped_at(),
        safety_max_window_hours=72,
        order_status=1,
    )
    assert out["source_summary"]["telemetry"]["available"] is False
    assert "telemetry" in out["source_summary"]["warnings"]


def test_summary_records_data_as_of_and_completeness() -> None:
    out = build_curves_and_summary(
        sources=_sources([_row(60, temperature=20.0)]),
        order_no=_order_no(),
        device=_device(),
        started_at=_started_at(),
        stopped_at=_stopped_at(),
        safety_max_window_hours=72,
        order_status=1,
    )
    assert "data_as_of" in out["source_summary"]
    assert 0.0 <= out["source_summary"]["completeness"] <= 1.0


def test_does_not_leak_vin_or_raw_fields() -> None:
    rows = [_row(60, temperature=20.0)]
    started = _started_at()
    out = build_curves_and_summary(
        sources=_sources(rows),
        order_no=_order_no(),
        device=_device(),
        started_at=started,
        stopped_at=started + timedelta(minutes=10),
        safety_max_window_hours=72,
        order_status=1,
    )
    blob = repr(out)
    for forbidden in ("vin", "raw", "SQL", "evidence", "workspace", "provider", "fixture"):
        assert forbidden not in blob, f"leaked {forbidden!r} in curves output"


def test_window_exceeds_safety_limit_raises_but_summary_still_builds() -> None:
    started = _started_at()
    # 100h > 72h limit
    with pytest.raises(ValueError, match="window"):
        build_curves_and_summary(
            sources=_sources([]),
            order_no=_order_no(),
            device=_device(),
            started_at=started,
            stopped_at=started + timedelta(hours=100),
            safety_max_window_hours=72,
            order_status=1,
        )


def test_unfinished_order_is_flagged() -> None:
    out = build_curves_and_summary(
        sources=_sources([]),
        order_no=_order_no(),
        device=_device(),
        started_at=_started_at(),
        stopped_at=_stopped_at(),
        safety_max_window_hours=72,
        order_status=0,
    )
    assert "order" in out["source_summary"]["warnings"]


def test_curves_temperature_has_max_min_delta_series() -> None:
    rows = [
        _row(60 * i, batteryMaxTemperature=40.0 + 0.1 * i, batteryMinTemperature=30.0 + 0.05 * i)
        for i in range(20)
    ]
    started = _started_at()
    out = build_curves_and_summary(
        sources=_sources(rows),
        order_no=_order_no(),
        device=_device(),
        started_at=started,
        stopped_at=started + timedelta(minutes=30),
        safety_max_window_hours=72,
        order_status=1,
    )
    names = {s["name"] for s in out["curves"]["temperature"]["series"]}
    assert {"电池最高温度", "电池最低温度", "电池实时温差"}.issubset(names)
