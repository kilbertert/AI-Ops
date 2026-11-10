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
    expected = (
        "cpms_order_detail",
        "cpms_charging_samples",
        "cpms_site_search",
        "cpms_order_tariff",
        "cpms_station_plan",
        "cpms_order_billing",
        "cpms_order_amount_check",
        "cpms_order_process_stats",
        "cpms_charging_timeline",
        "cpms_device_offline",
        "cpms_order_communications",
    )
    assert tuple(t.name for t in tools) == expected
    assert all(
        t.model_inputs == ("order_no",)
        for t in tools
        if t.name in {expected[0], expected[1], expected[3], expected[5], expected[8], expected[10]}
    )
    # 无模型输入的是两个证据计算工具；按时序工具之后按名判，不再按尾部位置判。
    assert all(t.model_inputs == () for t in tools if t.name in {expected[6], expected[7]})
    detail, samples, site, tariff, plan, billing = tools[:6]
    assert billing.request({"order_no": "ORDER-1"})["path"] == "/chOrderInfo/ORDER-1"
    assert detail.kind == "single" and detail.request({"order_no": "ORDER-1"}) == {
        "method": "GET",
        "path": "/chOrderInfo/getOrderInfoDetail/ORDER-1",
        "query": "",
    }
    assert samples.kind == "page" and samples.page_size == 50
    assert samples.request({"order_no": "ORDER-1"})["query"] == "current=1&size=50"
    # 证据字段就是「模型能回答什么」：这两张表的长度是契约的一部分，改动要过 ADR。
    # 26 → 35：后台「费用信息」那一栏的金额（预付费、电损、启动、优惠、增值税、付款时间）
    # 补进来，见 ADR-0193。
    assert len(detail.evidence_fields) == 43 and len(samples.evidence_fields) == 13
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


class ProviderConsole:
    def __init__(self):
        self.calls = []
        self.current = None
        self.keep_old = False

    def _call(self, workspace, path, **kwargs):
        self.calls.append(path)
        if path.endswith("/tool-providers?type=api"):
            return [{"name": "cpms-node"}] if self.current is not None else []
        if "/get" in path:
            return {
                "schema": json.dumps(self.current),
                "credentials": {"auth_type": "none"},
                "icon": {"background": "#abcdef", "content": "🔎"},
                "privacy_policy": "https://example.test/privacy",
                "labels": ["utilities"],
            }
        if "/add" in path or "/update" in path:
            body = kwargs["body"]
            assert body["credentials"] == {"auth_type": "none"}
            if not self.keep_old:
                self.current = json.loads(body["schema"])
            return {"result": "success"}
        raise AssertionError(path)


def test_provider_migration_is_an_explicit_action():
    console = ProviderConsole()
    console.current = {"openapi": "3.0.3", "paths": {}}
    with pytest.raises(ValueError, match="migrate explicitly"):
        ops_provider.ensure_node_provider(console, "ws", "https://gateway.example")
    assert ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", migrate=True) == {
        "action": "migrated",
        "provider": "cpms-node",
    }


@pytest.mark.parametrize(
    "conflict", [None, "server", "custom-tool", "credentials", "custom-header", "custom-body", "readback"]
)
def test_reconcile_managed_provider_preserves_unowned_state_and_checks_readback(conflict):
    import copy

    console = ProviderConsole()
    ops_provider.ensure_node_provider(console, "ws", "https://gateway.example")
    expected = copy.deepcopy(console.current)
    console.current.pop("x-aiops-projection-sha256", None)
    first = next(iter(console.current["paths"]))
    console.current["paths"] = {first: console.current["paths"][first]}
    console.current["paths"][first]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    if conflict == "server":
        console.current["servers"] = [{"url": "https://another.example"}]
    elif conflict == "custom-tool":
        console.current["paths"]["/custom"] = {"post": {"operationId": "custom"}}
    elif conflict == "credentials":
        console.current["security"] = [{"api_key": []}]
    elif conflict == "custom-header":
        console.current["paths"][first]["post"]["parameters"] = [
            {"in": "header", "name": "X-Custom", "schema": {"type": "string", "default": "custom"}}
        ]
    elif conflict == "custom-body":
        console.current["paths"][first]["post"]["requestBody"]["content"]["application/json"]["schema"][
            "properties"
        ]["custom"] = {"type": "string"}
    elif conflict == "readback":
        console.keep_old = True
    before = copy.deepcopy(console.current)
    console.calls.clear()
    if conflict:
        with pytest.raises(ValueError):
            ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", reconcile=True)
        assert console.current == before
        assert any("/update" in p for p in console.calls) is (conflict == "readback")
    else:
        result = ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", reconcile=True)
        assert result["action"] == "migrated" and console.current == expected
        console.calls.clear()
        assert (
            ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", reconcile=True)[
                "action"
            ]
            == "unchanged"
        )
        assert not any("/update" in p for p in console.calls)


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


def test_managed_provider_creation_must_be_confirmed_by_readback():
    console = ProviderConsole()
    console.keep_old = True
    with pytest.raises(ValueError, match="confirmed"):
        ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", reconcile=True)


def test_intact_generated_projection_survives_later_input_schema_changes():
    console = ProviderConsole()
    ops_provider.ensure_node_provider(console, "ws", "https://gateway.example")
    path = "/v1/operator-tools/cpms_order_detail"
    request = console.current["paths"][path]["post"]["requestBody"]
    request["content"]["application/json"]["schema"]["properties"]["order_no"]["maxLength"] = 64
    console.current["x-aiops-projection-sha256"] = ops_provider._projection_digest(console.current)
    assert (
        ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", reconcile=True)["action"]
        == "migrated"
    )
    # 人工在已有生成物里改动任一请求参数，旧内容标记不再匹配，不能静默覆盖。
    console.current["paths"][path]["post"]["requestBody"]["required"] = False
    with pytest.raises(ValueError, match="managed projection"):
        ops_provider.ensure_node_provider(console, "ws", "https://gateway.example", reconcile=True)
