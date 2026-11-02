"""站点工具：模型可提议参数，能不能用由网关判（ADR-0182 / ADR-0176）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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


def test_a_space_joined_place_is_still_the_users_own_words():
    """模型按自然习惯用空格拼地点（「广州 天河」），不能因此判成编造。

    2026-11-02 生产实测：用户说「广州天河附近的充电站」，模型给
    `address="广州 天河"`；整串子串匹配失败 →
    `OPERATOR_TOOL_ARGUMENTS_INVALID` → 节点兜底成 `cpms_unavailable`，
    站点工具第一次被真实使用就 100% 失败。

    放行后的值是**原文那一段**（「广州天河」）：下游（CPMS 过滤器、证据、最终答复）
    都只认这一个值，不能让模型的拼法活到查询里。
    """
    assert operator_cpms.validate_tool_arguments(TOOL, {"address": "广州 天河"}, QUESTION) == {
        "address": "广州天河"
    }
    # 放宽的只是分隔符：词本身仍然必须按原文顺序出现在原文里
    for invented in ({"address": "广州 南山"}, {"address": "深圳 天河"}):
        with pytest.raises(operator_cpms.OperatorRefused):
            operator_cpms.validate_tool_arguments(TOOL, invented, QUESTION)


@pytest.mark.parametrize(
    "value,question",
    [
        # 重排：两段都在原文里，顺序反了
        ("天河 广州", "帮我找一下广州天河附近的充电站"),
        # 重组：各取一个词拼出一个用户没说过的地方
        ("深圳 南山", "我关心深圳福田和广州南山"),
    ],
)
def test_reordered_or_reassembled_places_are_refused(value, question):
    """**逐词都在**不是判据 —— 它放过重排与重组。

    ADR-0176 要求参数是提问原文的子串。把判据写成 `all(part in question)` 看着像
    「只放宽分隔符」，实测放行「广州天河」→「天河 广州」与
    「深圳福田和广州南山」→「深圳 南山」。这里钉住的是**连续等序**这一半。
    """
    with pytest.raises(operator_cpms.OperatorRefused):
        operator_cpms.validate_tool_arguments(TOOL, {"address": value}, question)


def test_tool_arguments_and_final_answer_speak_the_same_address(monkeypatch):
    """工具参数 → CPMS 过滤器 → 冻结证据 → 最终答复 是**同一段原文**。

    2026-11-02 实测的第二个失败：参数 `"广州 天河"` 被放行，最终答复按协议拆成
    `["广州","天河"]`，`_sites_from_evidence` 拿最长词（「广州」）比整串地址，
    于是合法的运行必然 `OPERATOR_SITE_SEARCH_MISMATCH`。
    """
    args = operator_cpms.validate_tool_arguments(TOOL, {"address": "广州 天河"}, QUESTION)
    assert args == {"address": "广州天河"}
    answer = operator_conversation.validate(
        {"kind": "site_search", "text": "", "name": "", "address_terms": ["广州", "天河"]}, QUESTION
    )
    # 最终答复的关键词是原文里那几段（协议允许拆成多段），拼起来就是工具参数那一段
    assert answer["address_terms"] == ["广州", "天河"]
    assert operator_cpms.site_keywords(answer).address == args["address"]
    task = SimpleNamespace(
        delivered={TOOL},
        arguments={TOOL: args},
        evidence={
            TOOL: {
                "source": "cpms.sitePage",
                "observed_at": "2026-11-02T00:00:00+00:00",
                "data": {"records": [], "total": 0, "truncated": False},
            }
        },
    )
    # 范围由调用者决定，这里不是被测对象：只让它返回一个空范围，把断言留在一致性比对上。
    monkeypatch.setattr(operator_cpms, "operator_scope", lambda *_, **__: SimpleNamespace(site_ids=()))
    sites = operator_cpms._sites_from_evidence(object(), task, answer)
    assert sites["source"] == "cpms.sitePage"


def test_an_answer_about_a_different_place_is_still_refused():
    """一致性检查本身没有放松：参数说一处、答复说另一处，照样拒。"""
    task = SimpleNamespace(
        delivered={TOOL}, arguments={TOOL: {"address": "广州天河"}}, evidence={TOOL: {"data": {}}}
    )
    with pytest.raises(operator_cpms.OperatorRefused, match="OPERATOR_SITE_SEARCH_MISMATCH"):
        operator_cpms._sites_from_evidence(
            None,
            task,
            {"kind": "site_search", "text": "", "name": "", "address_terms": ["哈尔滨"]},
        )
