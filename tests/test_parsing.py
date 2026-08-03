import pytest

from aiops_diagnostics.models import Intent
from aiops_diagnostics.parsing import detect_intent, parse_request


def test_parse_labeled_order_and_amount_intent() -> None:
    request = parse_request("订单号：2079842220423700481 金额不对")
    assert request.order_no == "2079842220423700481"
    assert request.intent == Intent.AMOUNT


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("商城没有同步", Intent.SYNC),
        ("扫码启动失败", Intent.START_FAILURE),
        ("设备离线了", Intent.OFFLINE),
        ("中途停机", Intent.ABNORMAL_STOP),
        ("看看怎么回事", Intent.GENERAL),
    ],
)
def test_detect_intent(text: str, intent: Intent) -> None:
    assert detect_intent(text) == intent


def test_order_is_required() -> None:
    with pytest.raises(ValueError, match="未识别到订单号"):
        parse_request("金额不对")
