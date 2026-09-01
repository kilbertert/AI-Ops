from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.health_report import HealthReportError, build_minimal_health_report


class _Sources:
    def __init__(self, orders: list[dict]) -> None:
        self.orders = orders

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict]:
        return self.orders


def _order(**overrides):
    start = datetime(2026, 9, 1, 1, tzinfo=UTC)
    order = {
        "order_no": "O-1",
        "status": 1,
        "device_code": "D-1",
        "created_time": start,
        "stop_time": start + timedelta(hours=1),
        "device_protocol": "OCPP",
        "stopped_reason_code": "Local",
        "stopped_reason_content": "用户主动停止",
    }
    order.update(overrides)
    return order


def test_minimal_health_report_is_deterministic() -> None:
    report = build_minimal_health_report(_Sources([_order()]), "O-1", SafetySettings())

    assert report["order_no"] == "O-1"
    assert report["rule_version"] == "health-v1"
    assert report["summary"] == "本次充电健康报告包含 1 项正常指标"
    assert report["completeness"] == 1.0
    assert report["indicators"] == [
        {
            "code": "stop_reason",
            "status": "normal",
            "value": "用户主动停止",
            "unit": None,
            "reference": None,
            "reason_code": None,
        }
    ]


def test_minimal_health_report_completes_when_stop_reason_is_missing() -> None:
    report = build_minimal_health_report(
        _Sources([_order(stopped_reason_code=None, stopped_reason_content=None)]),
        "O-1",
        SafetySettings(),
    )

    assert report["completeness"] == 0.0
    assert report["indicators"][0]["status"] == "unavailable"
    assert report["indicators"][0]["reason_code"] == "SOURCE_DATA_MISSING"


@pytest.mark.parametrize(
    ("orders", "code"),
    [
        ([], "ORDER_NOT_FOUND"),
        ([_order(status=0)], "ORDER_NOT_ENDED"),
        ([_order(device_code=None, child_device_code=None)], "DEVICE_MISSING"),
        ([_order(stop_time=None)], "ORDER_WINDOW_INVALID"),
        (
            [_order(stop_time=datetime(2026, 9, 10, tzinfo=UTC))],
            "ORDER_WINDOW_TOO_LARGE",
        ),
    ],
)
def test_minimal_health_report_rejects_invalid_core_order(orders, code) -> None:
    with pytest.raises(HealthReportError) as excinfo:
        build_minimal_health_report(_Sources(orders), "O-1", SafetySettings())

    assert excinfo.value.code == code
