from aiops_diagnostics.rules import classify_stop_reason, is_server_billing, status_label


def test_order_status_uses_backend_meaning() -> None:
    assert status_label(2) == "不可控异常"
    assert status_label(5) == "可控异常结束"


def test_ykc_temperature_stop_is_abnormal() -> None:
    reason = classify_stop_reason("YKC1.8", "116", "")
    assert reason.classification == "over_temperature"
    assert reason.abnormal is True
    assert "温度" in reason.description


def test_ykc_start_failure_range_takes_precedence_over_temperature_keyword() -> None:
    reason = classify_stop_reason("YKC1.8", "83", "")

    assert reason.classification == "start_failure"
    assert reason.abnormal is True
    assert "启动失败" in reason.description


def test_ykc_stop_reason_boundaries_match_backend_ranges() -> None:
    assert classify_stop_reason("YKC1.8", "73", "").classification == "normal_stop"
    assert classify_stop_reason("YKC1.8", "74", "").classification == "start_failure"
    assert classify_stop_reason("YKC1.8", "102", "").classification == "start_failure"
    assert classify_stop_reason("YKC1.8", "116", "").classification == "over_temperature"


def test_server_billing_protocols() -> None:
    assert is_server_billing("OCPP1.6-J")
    assert is_server_billing("AYK")
    assert not is_server_billing("YKC1.8")
