"""输出引用基于工具数据，不接受任意字段/错工具引用。"""

import pytest

from aiops_diagnostics.operator.operator_agent import pointer, render
from aiops_diagnostics.operator.operator_conversation import OperatorRefused


def test_json_pointer_escapes_and_missing_values():
    assert pointer({"a/b": {"~c": None}}, "/a~1b/~0c") is None
    for path in ["/none", "/a~2b", "/a~1b/~0c/deeper"]:
        with pytest.raises(OperatorRefused):
            pointer({"a/b": {"~c": None}}, path)


def test_published_prompt_strips_only_the_known_runtime_suffix():
    from aiops_diagnostics.execution.node_publication import business_prompt
    from aiops_diagnostics.operator.operator_agent import INSTRUCTIONS

    assert business_prompt("业务SOP\n" + INSTRUCTIONS) == "业务SOP"
    assert business_prompt("业务SOP") == "业务SOP"


@pytest.mark.parametrize("path", ["/token", "/rows/01", "/rows/-1", "/rows/9"])
def test_invalid_references_are_refused(path):
    evidence = {"cpms_order_detail": {"data": {"rows": ["allowed"]}, "observed_at": "now"}}
    with pytest.raises(OperatorRefused):
        render("{{cpms_order_detail#" + path + "}}", evidence)


def test_model_selects_only_relevant_fields_without_rewriting_values():
    evidence = {"cpms_order_detail": {"data": {"voltage": None, "payAmount": 99}, "observed_at": "now"}}
    reply = render(
        "电压记录：{{cpms_order_detail#/voltage}}",
        evidence,
    )
    text = reply["blocks"][0]["text"]
    assert "未提供" in text and "99" not in text
