from __future__ import annotations

from aiops_diagnostics.health_curves import MAX_CURVE_POINTS, build_curves


def test_curves_sort_and_bound_points_preserving_extrema() -> None:
    samples = [{"_ts": i, "power": i, "outputVoltage": 400 + i, "temperature": i} for i in range(400)]
    samples[200]["power"] = 9999
    result = build_curves(reversed(samples))

    power = result["power"]
    points = power["series"][0]["points"]
    assert len(points) == MAX_CURVE_POINTS
    assert points[0] == [0.0, 0.0]
    assert points[-1] == [399.0, 399.0]
    assert [200.0, 9999.0] in points
    assert power["original_points"] == 400
    assert power["output_points"] == MAX_CURVE_POINTS


def test_curves_drop_missing_values_without_fabricating_series() -> None:
    result = build_curves([{"_ts": 1, "power": None, "temperature": 20}])

    assert result["power"]["series"] == []
    assert result["temperature"]["series"][0]["points"] == [[1.0, 20.0]]
    assert result["voltage"]["series"] == []


def test_curves_keep_duplicate_timestamps_ordered() -> None:
    result = build_curves(
        [
            {"_ts": "10", "power": 2},
            {"_ts": "2", "power": 1},
            {"_ts": "10", "power": 3},
        ]
    )

    assert result["power"]["series"][0]["points"] == [
        [2.0, 1.0],
        [10.0, 2.0],
        [10.0, 3.0],
    ]
