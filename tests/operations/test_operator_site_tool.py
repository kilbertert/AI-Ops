"""站点工具：模型可提议参数，能不能用由网关判（ADR-0182 / ADR-0176）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops_diagnostics.operator import operator_conversation, operator_cpms, operator_tools

DECLARATION = Path(operator_tools.__file__).with_name("operator_tools.json")

TOOL = "cpms_site_search"
QUESTION = "帮我找一下广州天河附近的充电站"


def test_declared_parameters_land_in_the_manifest_schema():
    """发布清单里的参数集合**恰好**等于声明登记的 model_inputs。

    运行绑定的输入（订单号）不出现在这里：模型不能选它，写进去等于给它一个入口。
    """
    declared = operator_tools.tool(TOOL)
    assert declared.inputs == ("name", "address")
    assert declared.model_inputs == ("name", "address")
    schema = json.loads(declared.input_schema())
    assert set(schema["properties"]) == {"name", "address"}
    assert schema["additionalProperties"] is False
    # 订单工具仍然是空参数集合——这是既有契约，不能因为新工具而变。
    assert json.loads(operator_tools.tool("cpms_order_detail").input_schema()) == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_undeclared_parameter_cannot_be_added_by_the_model():
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_cpms.validate_tool_arguments(TOOL, {"tenantId": "T2"}, QUESTION)


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "✕" * 81},
        {"address": "广州%"},
        {"address": "广州_天河"},
        {"address": "广州\\"},
        {"address": "广州\n天河"},
        {"name": 123},
    ],
)
def test_bad_parameter_shape_is_refused(arguments):
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_cpms.validate_tool_arguments(TOOL, arguments, QUESTION)


def test_invented_or_recycled_keywords_are_refused():
    """编一个地址、或把上一个提问的地址搬过来，都过不了「必须是原文子串」这一关。"""
    assert operator_cpms.validate_tool_arguments(TOOL, {"address": "广州天河"}, QUESTION) == {
        "address": "广州天河"
    }
    for invented in ({"address": "深圳南山"}, {"name": "上海虹桥"}):
        with pytest.raises(operator_cpms.OperatorRefused):
            operator_cpms.validate_tool_arguments(TOOL, invented, QUESTION)


def test_parameters_only_ever_touch_the_filter_never_the_shape():
    """范围、租户、分页上限与路径都不受参数影响：参数只进查询串的过滤器。"""
    declared = operator_tools.tool(TOOL)
    bare = declared.request({"name": "", "address": ""})
    filled = declared.request({"name": "站A", "address": "广州天河"})
    assert bare["path"] == filled["path"] == "/chSite/page"
    assert bare["method"] == filled["method"] == "GET"
    # 分页上限是声明的常量，调用方给不出别的值。
    assert "current=1&size=50" in bare["query"] and "current=1&size=50" in filled["query"]
    assert bare["query"] != filled["query"]


def test_the_declaration_cannot_express_an_arbitrary_path():
    """声明能力是有边界的：只允许 GET，路径必须是绝对路径且不含 `..`。"""
    body = json.loads(DECLARATION.read_text(encoding="utf-8"))
    for mutate, message in (
        (lambda tool: tool["request"].__setitem__("method", "DELETE"), "only GET"),
        (
            lambda tool: tool["request"].__setitem__("path_template", "https://evil.example/x"),
            "absolute path",
        ),
        (lambda tool: tool["request"].__setitem__("path_template", "/../etc/passwd"), "absolute path"),
    ):
        broken = json.loads(json.dumps(body))
        mutate(broken["tools"][2])
        with pytest.raises(operator_tools.ToolDeclarationError, match=message):
            operator_tools._parse(json.dumps(broken))


def test_a_parameter_that_disagrees_with_its_source_is_refused():
    """查询参数要么是常量，要么来自声明过的输入；指到一个不存在的输入就整份声明作废。"""
    broken = json.loads(DECLARATION.read_text(encoding="utf-8"))
    broken["tools"][2]["request"]["query"].append({"name": "site", "from": "site_id"})
    with pytest.raises(operator_tools.ToolDeclarationError, match="undeclared input"):
        operator_tools._parse(json.dumps(broken))


def test_conversation_manifest_keeps_only_conversation_tools():
    """无订单回合保留能在这类运行里用的工具，其余删掉——那不是裁剪，是正确性。"""
    template = {
        "schema": "operator-exec-config/1",
        "name": "n",
        "history_policy": "none",
        "model": {"model_key": "m", "provider_key": "p", "adapter_key": "a", "endpoint": "https://e"},
        "model_options": {},
        "system_messages": ["SOP"],
        "tools": [
            {"name": six, "description": "d", "input_schema": operator_tools.tool(six).input_schema()}
            for six in operator_tools.names()
        ],
    }
    manifest = json.loads(operator_conversation.manifest(json.dumps(template)))
    assert [entry["name"] for entry in manifest["tools"]] == [TOOL]
    assert json.loads(manifest["tools"][0]["input_schema"])["properties"]
