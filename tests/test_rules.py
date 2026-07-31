from aiops_diagnostics.rules import classify_stop_reason, is_server_billing, status_label


def test_order_status_uses_backend_meaning() -> None:
    assert status_label(2) == "不可控异常"
    assert status_label(5) == "可控异常结束"


def test_ykc_temperature_stop_is_abnormal() -> None:
    reason = classify_stop_reason("YKC1.8", "116", "")
    assert reason.classification == "over_temperature"
    assert reason.abnormal is True
    assert "温度" in reason.description


def test_server_billing_protocols() -> None:
    assert is_server_billing("OCPP1.6-J")
    assert is_server_billing("AYK")
    assert not is_server_billing("YKC1.8")
