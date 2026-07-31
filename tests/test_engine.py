from pathlib import Path
from typing import Any

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.engine import DiagnosticEngine
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.sources import FixtureSources

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
