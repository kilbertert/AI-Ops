#!/usr/bin/env python3
"""离线生成WF01独立CPMS节点草稿；保留源模型、提示词和应用特性，不发布。"""

import argparse
import copy
import os
from pathlib import Path

import yaml

from aiops_diagnostics.consumer_flow_contract import TYPES, publication_settings
from aiops_diagnostics.dify_dsl_pull import workflow_content

ROOT = Path(__file__).resolve().parents[1]


def select_knowledge(source, libraries, plugin_identifier):
    from aiops_diagnostics.consumer_knowledge_selection import PROVIDER, TOOL, selected_libraries

    if not plugin_identifier.startswith("trendpower/consumer_knowledge:"):
        raise ValueError("必须提供已验签知识检索插件的完整标识")
    candidate = copy.deepcopy(source)
    graph = candidate["workflow"]["graph"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    if set(nodes) != set(TYPES):
        raise ValueError("请先升级为WF01独立取数节点")
    node = nodes["knowledge"]["data"]
    nodes["knowledge"]["data"] = {
        "type": "tool",
        "title": node.get("title", "检索当前用户适用知识"),
        "provider_type": "builtin",
        "provider_id": PROVIDER,
        "provider_name": PROVIDER,
        "tool_name": TOOL,
        "tool_label": "检索当前用户适用知识",
        "tool_node_version": "2",
        "plugin_unique_identifier": plugin_identifier,
        "tool_configurations": {"knowledge_base_ids": {"type": "constant", "value": libraries}},
        "tool_parameters": {
            "data_grant": {"type": "mixed", "value": "{{#start.data_grant#}}"},
            "image_text": {"type": "mixed", "value": "{{#photo_context.output#}}"},
        },
    }
    selected_libraries(nodes["knowledge"]["data"])
    nodes["data_context"]["data"]["variables"][0] = ["knowledge", "text"]
    for edge in graph["edges"]:
        if edge["source"] == "knowledge":
            edge["data"]["sourceType"] = "tool"
        if edge["target"] == "knowledge":
            edge["data"]["targetType"] = "tool"
    dependencies = candidate.setdefault("dependencies", [])
    dependencies[:] = [
        d
        for d in dependencies
        if not str(d.get("value", {}).get("plugin_unique_identifier", "")).startswith(
            "trendpower/consumer_knowledge:"
        )
    ]
    dependencies.append({"type": "package", "value": {"plugin_unique_identifier": plugin_identifier}})
    return candidate


def live_documents(source):
    from aiops_diagnostics.consumer_documents import MODE_VARIABLE, PROMPT

    candidate = copy.deepcopy(source)
    nodes = {n["id"]: n["data"] for n in candidate["workflow"]["graph"]["nodes"]}
    if set(nodes) != set(TYPES) or nodes["knowledge"].get("type") != "tool":
        raise ValueError("动态知识需要现有WF01选库节点")
    variables = candidate["workflow"].setdefault("environment_variables", [])
    variables[:] = [v for v in variables if v.get("name") != MODE_VARIABLE]
    variables.append(
        {"id": MODE_VARIABLE, "name": MODE_VARIABLE, "value_type": "string", "value": "documents"}
    )
    from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES

    language = next(v for v in nodes["start"]["variables"] if v["variable"] == "language")
    language["options"] = list(SUPPORTED_LANGUAGES)
    prompts = nodes["answer"]["prompt_template"]
    prompts[:] = [p for p in prompts if p.get("role") != "system"]
    prompts.insert(0, {"role": "system", "text": PROMPT})
    nodes["disabled"]["answer"] = '{"kind":"unavailable","answer":"","reference_ids":[]}'
    return candidate


def upgrade(source, public_base):
    candidate = copy.deepcopy(source)
    original = {n["id"]: n["data"] for n in source["workflow"]["graph"]["nodes"]}
    known = {"start", "knowledge", "answer", "end"}
    if set(original) not in (known, known | {"photo_gate", "photo", "photo_context"}, set(TYPES)):
        raise ValueError("源应用不是已支持的WF01，拒绝覆盖")
    template = yaml.safe_load((ROOT / "deploy/dify-chat/consumer-service.dsl.yml").read_text())["workflow"]
    candidate["workflow"]["graph"] = template["graph"]
    nodes = {n["id"]: n["data"] for n in template["graph"]["nodes"]}
    for field in ("model", "prompt_template", "context", "memory", "vision"):
        if field in original["answer"]:
            nodes["answer"][field] = copy.deepcopy(original["answer"][field])
    for item in nodes["answer"]["prompt_template"]:
        item["text"] = item["text"].replace("{{#knowledge.body#}}", "{{#data_context.output#}}")
    nodes["photo"]["model"] = copy.deepcopy(original.get("photo", original["answer"])["model"])
    if "photo" in original:
        for field in ("prompt_template", "context", "memory", "vision"):
            if field in original["photo"]:
                nodes["photo"][field] = copy.deepcopy(original["photo"][field])
    for node in nodes.values():
        if node["type"] == "http-request":
            node["url"] = node["url"].replace("http://gateway.invalid", public_base.rstrip("/"))
    current = {e["name"]: e for e in candidate["workflow"].get("environment_variables", [])}
    for flag in template["environment_variables"]:
        current.setdefault(flag["name"], flag)
    candidate["workflow"]["environment_variables"] = list(current.values())
    publication_settings(
        workflow_content(yaml.safe_dump(candidate)),
        {"live_order_status": True, "live_billing_record": True},
        public_base,
        require_split=True,
    )
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--knowledge-base", action="append")
    parser.add_argument("--knowledge-plugin")
    parser.add_argument("--live-documents", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    source = yaml.safe_load(args.source.read_text())
    if args.live_documents:
        result = live_documents(source)
    elif args.knowledge_base or args.knowledge_plugin:
        if not args.knowledge_base or not args.knowledge_plugin:
            parser.error("选库迁移同时需要 --knowledge-base 与 --knowledge-plugin")
        result = select_knowledge(source, args.knowledge_base, args.knowledge_plugin)
    else:
        result = upgrade(source, args.gateway)
    with args.out.open("x") as file:
        file.write(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    print("已生成草稿；尚未导入或发布")


if __name__ == "__main__":
    main()
