"""Dify Workflow DSL 的结构自检（APP-RUNTIME-02）。

这些断言**不是**风格检查，每一条都对应一次实测过的失败：

* 边缺 `target` ⇒ Dify 静默跳过这条边（`if not isinstance(target, str): continue`），
  整张图只剩 start 一个节点执行，而导入与写草稿都是 200；
* 端节点没接变量 ⇒ `outputs` 是 `{}`，网关只能判 `DIFY_NO_DATA`；
* HTTP 步骤的 URL 不由模型决定 —— 它必须是一个写死的常量；
* 取数凭据必须由 **Workflow 输入直接绑定**到 Authorization 头。

跑法：`uv run pytest tests/test_dify_chat_dsl.py -q`
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DSL = Path(__file__).resolve().parents[1] / "deploy/dify-chat/consumer-order-status.dsl.yml"


def _graph() -> dict[str, Any]:
    document = yaml.safe_load(DSL.read_text(encoding="utf-8"))
    return document["workflow"]["graph"]


def _nodes() -> dict[str, dict[str, Any]]:
    return {node["id"]: node["data"] for node in _graph()["nodes"]}


def test_every_edge_names_both_ends() -> None:
    """每条边都必须写出 `source` 与 `target`。

    漏掉 `target` 时 Dify 不报错，只是把这条边丢掉 —— 症状是"workflow succeeded
    但只有一个节点执行过"，一路 200，直到运行结果为空才暴露。
    """
    edges = _graph()["edges"]
    assert edges, "DSL 没有任何边"
    nodes = _nodes()
    for edge in edges:
        assert isinstance(edge.get("source"), str) and edge["source"], f"边 {edge.get('id')} 缺 source"
        assert isinstance(edge.get("target"), str) and edge["target"], f"边 {edge.get('id')} 缺 target"
        assert edge["source"] in nodes, f"边 {edge.get('id')} 的 source 不是图里的节点"
        assert edge["target"] in nodes, f"边 {edge.get('id')} 的 target 不是图里的节点"


def test_the_chain_is_start_http_llm_end() -> None:
    """链路形状固定：start → http-request → llm → end。少一环就不是本片的链路。"""
    nodes = _nodes()
    order = []
    current = "start"
    seen = set()
    while current and current not in seen:
        seen.add(current)
        order.append(nodes[current]["type"] if current in nodes else "?")
        nxt = [e["target"] for e in _graph()["edges"] if e["source"] == current]
        assert len(nxt) <= 1, f"{current} 有多个下游，链路不再是线性的"
        current = nxt[0] if nxt else None
    assert order == ["start", "http-request", "llm", "end"], order


def test_the_fetch_step_url_is_a_fixed_constant() -> None:
    """取数 URL 不由模型生成或选择：它必须是字面量，且不含任何变量插值。"""
    http_node = next(data for data in _nodes().values() if data["type"] == "http-request")
    url = http_node["url"]
    assert "{{" not in url and "#}" not in url, f"URL 里出现了变量插值：{url}"
    assert url.endswith("/v1/internal/charging-status/dify-read"), url


def test_the_credential_is_bound_directly_to_the_authorization_header() -> None:
    """凭据由 Workflow 输入直接绑定在 Authorization 头上，不经过模型。"""
    http_node = next(data for data in _nodes().values() if data["type"] == "http-request")
    expected = "Authorization: Bearer {{#start.data_grant#}}"
    assert http_node["headers"].strip() == expected, http_node["headers"]
    # 凭据**不能**出现在模型能看到的地方。
    llm_node = next(data for data in _nodes().values() if data["type"] == "llm")
    serialized = yaml.safe_dump(llm_node, allow_unicode=True)
    assert "data_grant" not in serialized, "模型能看到取数凭据"


def test_the_end_node_publishes_the_snapshot_for_the_gateway() -> None:
    """端节点必须把本轮快照交出来：网关只信这份快照，不信模型写了什么。"""
    end_node = next(data for data in _nodes().values() if data["type"] == "end")
    outputs = {item["variable"]: item["value_selector"] for item in end_node["outputs"]}
    assert outputs.get("snapshot") == ["read_status", "body"], outputs
    assert outputs.get("answer") == ["answer", "text"], outputs


def test_the_model_sees_only_trimmed_facts() -> None:
    """模型只拿到**取数步骤的响应体**这一个值。

    它拿不到凭据、拿不到 URL、也拿不到别的节点输出 —— 取数步骤的 `body` 已经是网关
    裁剪过的五个字段。注意 Dify 的 HTTP 步骤把响应体当文本，所以这里**不能**写
    `read_status.body.order_no`：那会在运行时静默渲染成空串（`body` 是 str）。
    """
    llm_node = next(data for data in _nodes().values() if data["type"] == "llm")
    rendered = yaml.safe_dump(llm_node["prompt_template"], allow_unicode=True)
    selected = {part.split("#}")[0] for part in rendered.split("{{#")[1:]}
    assert selected == {"read_status.body"}, selected


def test_node_ids_are_usable_in_prompt_placeholders() -> None:
    """节点 id 只能用 `[a-zA-Z0-9_]`。

    Dify 用 `graphon` 的 `VARIABLE_PATTERN` 解析 `{{#node.var#}}`，那个正则**不接受连字符**：
    节点 id 写成 `read-status` 时占位符**原样**进到模型提示里，既不报错也不渲染 ——
    实测症状是模型收到字面的 `{{#read-status.body#}}`，而工作流状态是 succeeded。
    """
    import re

    pattern = re.compile(r"\{\{#([a-zA-Z0-9_]{1,50}(?:\.[a-zA-Z_][a-zA-Z0-9_]{0,29}){1,10})#\}\}")
    for node_id in _nodes():
        assert re.fullmatch(r"[a-zA-Z0-9_]+", node_id), f"节点 id 含非法字符：{node_id}"
    llm_node = next(data for data in _nodes().values() if data["type"] == "llm")
    for message in llm_node["prompt_template"]:
        text = message["text"]
        for raw in re.findall(r"\{\{#.*?#\}\}", text):
            assert pattern.fullmatch(raw), f"占位符不会被 Dify 解析：{raw}"
