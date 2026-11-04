"""自然答复不再要求字段引用；实际取证来源由系统附加。"""

import pytest

from aiops_diagnostics.operator.operator_agent import render
from aiops_diagnostics.operator.operator_conversation import OperatorRefused


def test_published_prompt_strips_only_the_known_runtime_suffix():
    from aiops_diagnostics.execution.node_publication import business_prompt
    from aiops_diagnostics.operator.operator_agent import INSTRUCTIONS

    assert business_prompt("业务SOP\n" + INSTRUCTIONS) == "业务SOP"
    assert business_prompt("业务SOP") == "业务SOP"


@pytest.mark.parametrize("answer", ["", " ", None, {}, "字" * 12001])
def test_empty_or_unbounded_answer_is_refused(answer):
    with pytest.raises(OperatorRefused):
        render(answer, {})


def test_natural_reply_keeps_only_requested_detail_and_real_sources():
    evidence = {"cpms_order_detail": {"data": {"voltage": None, "payAmount": 99}, "observed_at": "now"}}
    text = render("电压记录未提供。", evidence)["blocks"][0]["text"]
    assert text.startswith("电压记录未提供。") and "99" not in text
    assert "订单详情" in text and "订单金额核验" not in text
