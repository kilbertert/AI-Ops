"""工具声明是**唯一**来源：两侧都读它，加一个工具不改节点源码。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.operator import operator_tools, ops_provider

ROOT = Path(__file__).parents[2]
NODE_SOURCE = ROOT / "services/execution-node/src/main/java"


def declaration(**overrides) -> dict:
    body = json.loads((ROOT / "src/aiops_diagnostics/operator/operator_tools.json").read_text())
    body.update(overrides)
    return body


def parsed(body: dict):
    return operator_tools._parse(json.dumps(body))


def test_the_declaration_covers_the_live_tools():
    tools = operator_tools.tools()
    assert [tool.name for tool in tools] == [
        "cpms_order_detail",
        "cpms_charging_samples",
        "cpms_site_search",
    ]
    # 哪一类运行能用哪个工具，是声明的一部分：订单工具没有订单就没有取证对象。
    assert [tool.applies_to for tool in tools] == [("order",), ("order",), ("conversation",)]
    # 只有模型可以提议的那部分输入进发布清单的 schema。
    assert [tool.model_inputs for tool in tools] == [(), (), ("name", "address")]
    detail, samples, site = tools
    assert detail.kind == "single" and detail.request({"order_no": "ORDER-1"}) == {
        "method": "GET",
        "path": "/chOrderInfo/getOrderInfoDetail/ORDER-1",
        "query": "",
    }
    assert samples.kind == "page" and samples.page_size == 50
    assert samples.request({"order_no": "ORDER-1"})["query"] == "current=1&size=50"
    # 证据字段就是「模型能回答什么」：这两张表的长度是契约的一部分，改动要过 ADR。
    assert len(detail.evidence_fields) == 26 and len(samples.evidence_fields) == 13
    assert site.evidence_fields == ("id", "name", "address")
    assert site.request({"name": "天河", "address": ""})["query"] == (
        "current=1&size=50&type=0&name=%E5%A4%A9%E6%B2%B3"
    )


def test_publication_and_provider_read_the_same_declaration():
    """发布白名单与 provider 都从声明取，不再各留一份常量。"""
    declared = operator_tools.names()
    assert declared == chat_execution.OPERATOR_TOOL_NAMES

    class Console:
        def __init__(self):
            self.calls = []

        def _call(self, workspace, path, **kwargs):
            self.calls.append((path, kwargs))
            if path.endswith("/tool-providers?type=api"):
                return []
            if "/add" in path:
                return {"result": "success"}
            raise AssertionError(path)

    console = Console()
    assert ops_provider.ensure_node_provider(console, "ws", "https://gateway.example")["action"] == "created"
    _, kwargs = console.calls[-1]
    schema = json.loads(kwargs["body"]["schema"])
    assert set(schema["paths"]) == {f"/v1/operator-tools/{name}" for name in declared}
    assert schema["paths"][f"/v1/operator-tools/{declared[0]}"]["post"]["summary"] == (
        operator_tools.tool(declared[0]).description
    )


def test_provider_migration_is_an_explicit_action():
    """schema 变了：不加 --migrate 是明确报错，加了是一次可审计的覆盖。"""

    class Console:
        def __init__(self):
            self.calls = []

        def _call(self, workspace, path, **kwargs):
            self.calls.append(path)
            if path.endswith("/tool-providers?type=api"):
                return [{"name": "cpms-node"}]
            if "/get" in path:
                return {"schema": json.dumps({"openapi": "3.0.3", "paths": {}})}
            if "/update" in path:
                return {"result": "success"}
            raise AssertionError(path)

    with pytest.raises(ValueError, match="migrate explicitly"):
        ops_provider.ensure_node_provider(Console(), "ws", "https://gateway.example")
    console = Console()
    assert ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", migrate=True) == {
        "action": "migrated",
        "provider": "cpms-node",
    }
    assert any("/update" in path for path in console.calls)


@pytest.mark.parametrize(
    "fault, message",
    [
        ("schema", "schema"),
        ("duplicate", "duplicate"),
        ("post-method", "only GET"),
        ("placeholder", "path placeholders"),
        ("kind", "response kind"),
        ("no-fields", "evidence_fields"),
        ("page-size", "page_size"),
        ("single-page-size", "only page responses"),
    ],
)
def test_a_malformed_declaration_is_a_startup_failure(fault, message):
    """声明写错就是启动失败——不能被读成「少了一个工具」而以运营页面的形状出现。"""
    body = declaration()
    first = body["tools"][0]
    if fault == "schema":
        body["schema"] = "something-else/1"
    elif fault == "duplicate":
        body["tools"].append(dict(first))
    elif fault == "post-method":
        first["request"] = {**first["request"], "method": "POST"}
    elif fault == "placeholder":
        first["request"] = {**first["request"], "path_template": "/x/{site_id}"}
    elif fault == "kind":
        first["response"] = {**first["response"], "kind": "stream"}
    elif fault == "no-fields":
        first["evidence_fields"] = []
    elif fault == "page-size":
        body["tools"][1]["response"] = {**body["tools"][1]["response"], "page_size": 0}
    elif fault == "single-page-size":
        first["response"] = {**first["response"], "page_size": 50}
    with pytest.raises(operator_tools.ToolDeclarationError, match=message):
        parsed(body)


def test_a_missing_input_cannot_be_substituted_with_an_empty_string():
    with pytest.raises(operator_tools.ToolDeclarationError, match="missing input"):
        operator_tools.tool("cpms_order_detail").request({})


def test_undeclared_tool_is_still_refused():
    with pytest.raises(operator_tools.ToolDeclarationError, match="undeclared"):
        operator_tools.tool("cpms_nonexistent")


def test_the_node_source_names_no_tool_at_all():
    """AC2 的机械判据：声明里加了工具，节点源码不可能需要改——那里一个工具名都没有。

    这比「记得去看一眼 Java」强：它把「新增工具要改 Java」这件事变成**做不到**，
    而不是「不该做」。
    """
    declared = operator_tools.names()
    offenders = []
    for path in NODE_SOURCE.rglob("*.java"):
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path.name}: {name}" for name in declared if name in text)
    assert offenders == []
