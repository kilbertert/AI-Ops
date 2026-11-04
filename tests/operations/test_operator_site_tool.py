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
    assert declared.inputs == ("name", "address", "address_terms")
    assert declared.model_inputs == ("name", "address_terms")
    schema = json.loads(declared.input_schema())
    assert set(schema["properties"]) == {"name", "address_terms"}
    assert schema["properties"]["address_terms"]["maxItems"] == 4
    assert schema["required"] == ["address_terms"]
    assert schema["additionalProperties"] is False
    assert set(json.loads(operator_tools.tool("cpms_order_detail").input_schema())["properties"]) == {
        "order_no"
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


def test_matching_preserves_original_english_spacing_and_casefold_offsets():
    assert operator_cpms.validate_tool_arguments(TOOL, {"address": "NewYork"}, "Find New York stations") == {
        "address": "New York"
    }
    assert operator_conversation.question_span("York", "Straße York") == "York"
    assert operator_conversation.question_span("STRASSE", "Straße York") == "Straße"


# ─────────────── 2026-11-04：地址词是「或」，不是「且」
#
# 真实反馈：「帮我找出广州天河的充电桩」返回 0 条，但 CPMS 里明明有
# HuaJing Station（地址写着 天河公园 Tian He Qu, Guang Zhou Shi）。
# 原因：本地 matches() 用 all()，要求「广州」与「天河」同时出现在同一条地址里 ——
# 而地址用的是拼音 Guang Zhou，「广州」永远命不中，一票否决把真实站点筛掉了。

HUAJING = {
    "id": "1950545576277942273",
    "name": "HuaJing Station",
    "address": (
        "49P4+2PV, Zhong Shan Da Dao Xi, 天河公园 Tian He Qu, Guang Zhou Shi, Guang Dong Sheng, China, 510630"
    ),
}


def test_address_terms_are_any_not_all():
    """用户说「广州天河」指的是**这一带**，不是要求两个字面串同时出现。"""
    from aiops_diagnostics.operator.operator_cpms import SiteKeywords

    assert SiteKeywords("", ("广州", "天河")).matches(HUAJING), "多给一个地名不该把真实结果筛掉"
    assert SiteKeywords("", ("天河",)).matches(HUAJING)
    assert SiteKeywords("", ("天河公园",)).matches(HUAJING)


def test_a_term_that_really_is_absent_still_excludes_the_site():
    """对照组：放宽成「或」不能放宽成「什么都收」。"""
    from aiops_diagnostics.operator.operator_cpms import SiteKeywords

    assert not SiteKeywords("", ("深圳",)).matches(HUAJING)
    assert not SiteKeywords("", ("深圳", "珠海")).matches(HUAJING)


def test_the_site_name_still_requires_every_word():
    """站名保持「且」：真给了多个词时，名字里**都要**出现才算是那个站。"""
    from aiops_diagnostics.operator.operator_cpms import SiteKeywords

    assert SiteKeywords("HuaJing", ()).matches(HUAJING)
    assert not SiteKeywords("HuaJing Other", ()).matches(HUAJING)


def test_listing_matches_every_site_the_scope_returned():
    """空关键词是**列举**，不是过滤 —— 一条都不许筛掉。"""
    from aiops_diagnostics.operator.operator_cpms import SiteKeywords

    other = {"id": "x", "name": "ZDH--Test", "address": "984HF52X+QW"}
    keywords = SiteKeywords("", ())
    assert keywords.matches(HUAJING) and keywords.matches(other)
