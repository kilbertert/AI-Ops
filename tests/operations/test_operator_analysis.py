"""SOP分析必须来自同轮证据，不能靠抄字段或模型编数。"""

import pytest

from aiops_diagnostics.operator.operator_cpms import render_report
from operations.test_operator_reply_facts import answer


def evidence():
    return {
        "cpms_order_detail": {
            "source": "cpms.orderDetail",
            "order_no": "SYNTHETIC-ORDER",
            "data": {
                "orderNo": "SYNTHETIC-ORDER",
                "electricity": "1.7111",
                "time": 23,
                "gunPower": 7,
                "electricityFee": "5.13",
                "serviceFee": "1.71",
                "voltage": None,
            },
        },
        "cpms_charging_samples": {
            "source": "cpms.chargingMonitor",
            "data": {"records": [], "samples_returned": 0, "complete_timeline": False},
        },
    }


def report(data, question):
    return render_report(answer("已核对本轮证据。\n{{fact:order_facts}}"), data, question=question)


def test_power_analysis_calculates_average_not_rated_power():
    text = report(evidence(), "分析这单充电功率")
    assert "1.7111 kWh ÷ 23 min × 60 = 4.46 kW" in text
    assert "额定功率不等于实际输出" in text
    assert "正常" not in text


def test_voltage_without_samples_explains_missing_trend_not_bill():
    text = report(evidence(), "分析这单电压")
    assert "无法核对电压变化" in text
    assert "电费" not in text


def test_voltage_range_uses_only_valid_observed_samples():
    data = evidence()
    data["cpms_charging_samples"]["data"]["records"] = [{"voltage": 220}, {"voltage": 230}, {"voltage": None}]
    data["cpms_charging_samples"]["data"]["samples_returned"] = 3
    text = report(data, "分析电压")
    assert "2 个有效电压值" in text and "220–230 V" in text
    assert "不代表完整过程" in text


@pytest.mark.parametrize("value", [None, 0, -1, "NaN", "Infinity", "invalid"])
def test_invalid_duration_is_not_divided(value):
    data = evidence()
    data["cpms_order_detail"]["data"]["time"] = value
    text = report(data, "分析充电功率")
    assert "× 60 =" not in text


def test_fee_analysis_is_a_named_subtotal_not_an_invented_tariff():
    text = report(evidence(), "分析电费")
    assert "电费 5.13 + 服务费 1.71 = 6.84" in text
    assert "不能据此反推计费单价" in text
    assert "V" not in text
