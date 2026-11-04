"""确定性统计通过工具返回，不按自然语言关键词选择计算。"""

from decimal import Decimal

import pytest

from aiops_diagnostics.operator.order_analysis import metrics


def test_average_power_is_independent_of_rated_power():
    data = metrics({"electricity": "1.7111", "time": 23, "gunPower": 7}, {"records": []})
    assert data["average_power_kw"] == "4.46"
    assert data["sample_count"] == 0 and not data["complete_timeline"]


def test_ranges_ignore_missing_invalid_and_negative_readings():
    data = metrics({}, {"records": [{"voltage": 220}, {"voltage": 230}, {"voltage": None}, {"voltage": -1}]})
    voltage = data["metrics"][0]
    assert voltage["minimum"] == Decimal("220") and voltage["maximum"] == Decimal("230")
    assert voltage["count"] == 2


@pytest.mark.parametrize("value", [None, 0, -1, "NaN", "Infinity", "invalid"])
def test_invalid_duration_does_not_create_average(value):
    assert metrics({"electricity": 1, "time": value}, {"records": []})["average_power_kw"] is None
