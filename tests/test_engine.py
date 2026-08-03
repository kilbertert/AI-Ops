import copy
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.engine import DiagnosticEngine, _normalize_error_code
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.sources import FixtureSources, SourceError

FIXTURES = Path(__file__).parents[1] / "examples" / "fixtures"


def _diagnose(fixture: str, problem: str):
    engine = DiagnosticEngine(FixtureSources(FIXTURES / fixture), SafetySettings())
    return engine.diagnose(parse_request(problem))


def test_ykc_amount_mismatch_and_temperature_fault() -> None:
    report = _diagnose("ykc_amount_mismatch.json", "订单 TEST-YKC-0001 金额异常")
    assert "amount_inconsistent" in report.classifications
    assert "controlled_abnormal_end" in report.classifications
    assert "over_temperature" in report.classifications
    assert "device_error_reported" in report.classifications
    assert "金额或电量" in report.summary
    comm_evidence = next(item for item in report.evidence if item.title == "设备通讯报文")
    assert "1 条" in comm_evidence.observation
    assert report.confidence == "high"


def test_missing_tx_data_is_primary_diagnosis() -> None:
    report = _diagnose("missing_tx_data.json", "订单 TEST-MISSING-0002 中途停机")
    assert report.classifications[:2] == ["uncontrollable_exception", "missing_tx_data"]
    assert report.summary.startswith("平台未确认收到设备交易结束数据")
    assert "communication_missing" in report.classifications


def test_ocpp_does_not_use_pile_total_fee_formula() -> None:
    report = _diagnose("ocpp_consistent.json", "订单 TEST-OCPP-0003 金额是否正常")
    assert "amount_inconsistent" not in report.classifications
    amount_evidence = next(item for item in report.evidence if item.title == "金额与电量一致性")
    assert amount_evidence.facts["billing_side"] == "server"


class _EmptySources:
    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None):
        raise AssertionError("not called")

    def get_device(self, device_id: str | None, device_code: str | None):
        raise AssertionError("not called")

    def get_gun_samples(self, device, start_time, end_time, tx_serial_no):
        raise AssertionError("not called")

    def get_comm_messages(self, device, start_time, end_time):
        raise AssertionError("not called")

    def inspect_streams(self, order_no: str):
        raise AssertionError("not called")

    def doctor(self):
        return {}


def test_not_found_stops_before_secondary_sources() -> None:
    engine = DiagnosticEngine(_EmptySources(), SafetySettings())
    report = engine.diagnose(parse_request("订单 TEST-NOTFOUND-01 怎么回事"))
    assert report.classifications == ["order_not_found"]
    assert report.queried_sources == ["mysql:ch_order_info"]


def _base_order(**overrides: Any) -> dict[str, Any]:
    order = {
        "order_no": "TEST-ORDER-1234",
        "tenant_id": "TENANT-1",
        "status": 1,
        "type": 0,
        "launch_type": "remote",
        "device_id": "DEVICE-ID-1",
        "device_code": "PILE-1",
        "child_device_code": "GUN-1",
        "device_protocol": "YKC1.8",
        "created_time": "2026-07-31 10:00:00",
        "stop_time": "2026-07-31 10:30:00",
        "electricity": "10",
        "electricity_fee": "8",
        "service_fee": "2",
        "launch_fee": "0",
        "park_fee": "0",
        "ds_electric_fee": "0",
        "ds_service_fee": "0",
        "total_amount": "10",
        "is_receive_tx_data": 1,
        "stopped_reason_code": "64",
        "tx_data": {
            "txSerialNo": "TEST-ORDER-1234",
            "electricityQuantity": "10",
            "totalFee": "10",
            "tipFee": "0",
            "peakFee": "0",
            "flatFee": "10",
            "valleyFee": "0",
        },
    }
    order.update(overrides)
    return order


class _StaticSources:
    def __init__(
        self,
        orders: list[dict[str, Any]],
        *,
        fee_record: dict[str, Any] | None = None,
        gun_error: str | None = None,
        redis_error: str | None = None,
        streams: list[dict[str, Any]] | None = None,
    ) -> None:
        self.orders = orders
        self.fee_record = fee_record if fee_record is not None else {"period_fee_detail": {}}
        self.gun_error = gun_error
        self.redis_error = redis_error
        self.streams = streams or []
        self.fee_calls = 0
        self.gun_calls: list[str | None] = []

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        return copy.deepcopy(self.orders)

    def get_fee_template_record(self, order_no: str, tenant_id: str | None = None):
        self.fee_calls += 1
        return copy.deepcopy(self.fee_record)

    def get_device(self, device_id: str | None, device_code: str | None):
        return {"online_status": 1, "protocol": self.orders[0].get("device_protocol")}

    def get_gun_samples(self, device, start_time, end_time, tx_serial_no):
        self.gun_calls.append(tx_serial_no)
        if self.gun_error:
            raise SourceError(self.gun_error)
        return [{"status": 2, "power": 7.2, "isInsert": 1}]

    def get_comm_messages(self, device, start_time, end_time):
        return [{"direction": "up", "code": "event", "decoded": "ok"}]

    def inspect_streams(self, order_no: str):
        if self.redis_error:
            raise SourceError(self.redis_error)
        return copy.deepcopy(self.streams)

    def doctor(self):
        return {}


def _diagnose_order(order: dict[str, Any], problem: str = "订单 TEST-ORDER-1234 怎么回事", **kwargs):
    sources = _StaticSources([order], **kwargs)
    report = DiagnosticEngine(sources, SafetySettings()).diagnose(parse_request(problem))
    return report, sources


def test_tx_data_presence_takes_precedence_over_receive_flag() -> None:
    report, _ = _diagnose_order(_base_order(is_receive_tx_data=0))

    assert "missing_tx_data" not in report.classifications
    assert "tx_receive_flag_inconsistent" in report.classifications
    assert "amount_inconsistent" not in report.classifications


def test_charging_order_does_not_require_end_transaction_data() -> None:
    report, _ = _diagnose_order(
        _base_order(status=0, tx_data=None, is_receive_tx_data=0, stop_time=None, stopped_reason_code=None)
    )

    assert "charging" in report.classifications
    assert "missing_tx_data" not in report.classifications


def test_operator_order_uses_operator_billing_and_skips_fee_snapshot() -> None:
    report, sources = _diagnose_order(
        _base_order(
            launch_type="operator",
            is_receive_tx_data=0,
            electricity_fee="0",
            service_fee="0",
            total_amount="12.50",
            tx_data={"electricityQuantity": "10", "totalFee": "12.50"},
        )
    )

    assert "missing_tx_data" not in report.classifications
    assert "amount_inconsistent" not in report.classifications
    amount = next(item for item in report.evidence if item.title == "金额与电量一致性")
    assert amount.facts["billing_side"] == "operator"
    assert sources.fee_calls == 0


def test_two_wheel_order_is_conservatively_marked_unsupported() -> None:
    report, sources = _diagnose_order(
        _base_order(type=1, tx_data={"energy": 1000}, electricity=None, total_amount="0")
    )

    assert "unsupported_order_type" in report.classifications
    assert "amount_inconsistent" not in report.classifications
    assert report.confidence == "low"
    assert sources.fee_calls == 0
    assert sources.gun_calls == []


@pytest.mark.parametrize(
    ("protocol", "transaction_id", "expected"),
    [
        ("OCPP1.6-J", "38192", "38192"),
        ("AYK", "AYK-INTERNAL-ID", "TEST-ORDER-1234"),
        ("YKC1.6", "YKC-INTERNAL-ID", "TEST-ORDER-1234"),
    ],
)
def test_tdengine_correlation_follows_backend_protocol_rules(
    protocol: str, transaction_id: str, expected: str
) -> None:
    order = _base_order(
        device_protocol=protocol,
        transaction_id=transaction_id,
        tx_data={
            "txSerialNo": "EVENT-SERIAL",
            "electricityQuantity": "10",
            "totalFee": "10",
            "flatFee": "10",
        },
    )
    report, sources = _diagnose_order(order)

    assert sources.gun_calls == [expected]
    serial_evidence = next(item for item in report.evidence if item.title == "设备交易流水号")
    assert serial_evidence.facts["source"] == (
        "transaction_id" if protocol.startswith("OCPP") else "order_no"
    )


@pytest.mark.parametrize(
    ("status", "stop_code"),
    [(5, "64"), (1, "116")],
)
def test_status_and_ykc_stop_reason_disagreement_is_reported(status: int, stop_code: str) -> None:
    report, _ = _diagnose_order(_base_order(status=status, stopped_reason_code=stop_code))

    assert "status_stop_reason_inconsistent" in report.classifications


def test_non_finite_transaction_values_do_not_crash_amount_checks() -> None:
    tx_data = copy.deepcopy(_base_order()["tx_data"])
    tx_data["electricityQuantity"] = "NaN"

    report, _ = _diagnose_order(_base_order(tx_data=tx_data))

    assert "transaction_data_incomplete" in report.classifications


def test_error_code_normalization_deduplicates_numbers_and_preserves_protocol_codes() -> None:
    assert _normalize_error_code(5) == "5"
    assert _normalize_error_code(5.0) == "5"
    assert _normalize_error_code("5.0") == "5"
    assert _normalize_error_code("E116") == "E116"
    assert _normalize_error_code(0) is None


def test_server_billing_rejects_reversed_meter_values_even_with_tx_energy() -> None:
    report, _ = _diagnose_order(
        _base_order(device_protocol="OCPP1.6-J", transaction_id="38192", meter_start=12000, meter_end=11000)
    )

    assert "amount_inconsistent" in report.classifications
    amount = next(item for item in report.evidence if item.title == "金额与电量一致性")
    assert "结束电表值小于开始电表值" in amount.observation


def test_duplicate_rows_in_one_tenant_do_not_request_tenant_filter() -> None:
    sources = _StaticSources([_base_order(), _base_order(id="SECOND-ROW")])
    report = DiagnosticEngine(sources, SafetySettings()).diagnose(
        parse_request("订单 TEST-ORDER-1234 怎么回事")
    )

    assert report.classifications == ["duplicate_order_record"]
    assert "tenant_id" not in report.summary
    assert "tenant_id" not in " ".join(report.next_steps)


def test_source_failure_and_missing_fee_snapshot_cap_confidence() -> None:
    report, _ = _diagnose_order(
        _base_order(),
        "订单 TEST-ORDER-1234 金额不对",
        fee_record={},
        gun_error="TDengine 拒绝查询: Invalid column name",
        redis_error="Redis 只读查询失败: UnicodeDecodeError",
    )

    assert report.confidence != "high"
    assert "tdengine:charging-gun_property" in report.failed_sources
    assert "redis:order_sync_streams" in report.failed_sources


@pytest.mark.parametrize(
    ("created_time", "stop_time"),
    [
        ("2026-07-31 10:00:00", "2026-07-31T10:30:00+08:00"),
        ("2026-07-31T10:00:00+08:00", "2026-07-31 10:30:00"),
    ],
)
def test_mixed_timezone_order_window_does_not_crash(created_time: str, stop_time: str) -> None:
    report, sources = _diagnose_order(_base_order(created_time=created_time, stop_time=stop_time))

    assert sources.gun_calls == ["TEST-ORDER-1234"]
    assert "order_not_found" not in report.classifications


def test_global_stream_pending_is_not_attributed_to_current_order() -> None:
    report, _ = _diagnose_order(
        _base_order(),
        "订单 TEST-ORDER-1234 为什么没有同步",
        streams=[
            {
                "stream": "third.order.sync.queue",
                "length": 20,
                "matches": 0,
                "groups": [{"name": "mall", "pending": 7, "lag": 3, "consumers": 1}],
            }
        ],
    )

    assert "sync_pending" not in report.classifications
    assert "当前订单仍在 pending" not in report.summary


@pytest.mark.parametrize("status", [0, 1, 2, 3, 5])
@pytest.mark.parametrize("protocol", ["YKC1.8", "YKC1.6", "OCPP1.6-J", "AYK"])
@pytest.mark.parametrize("launch_type", ["remote", "operator"])
def test_protocol_status_launch_type_matrix_is_stable(status: int, protocol: str, launch_type: str) -> None:
    tx_data = None if status == 0 else copy.deepcopy(_base_order()["tx_data"])
    report, sources = _diagnose_order(
        _base_order(
            status=status,
            device_protocol=protocol,
            launch_type=launch_type,
            transaction_id="38192",
            tx_data=tx_data,
            is_receive_tx_data=0,
            stop_time=None if status == 0 else "2026-07-31 10:30:00",
            stopped_reason_code=None if status == 0 else "64",
        )
    )

    assert report.summary != "诊断未完成"
    assert report.confidence in {"low", "medium", "high"}
    if status == 0:
        assert "missing_tx_data" not in report.classifications
    if tx_data is not None:
        assert "missing_tx_data" not in report.classifications
    assert sources.fee_calls == (0 if launch_type == "operator" else 1)
