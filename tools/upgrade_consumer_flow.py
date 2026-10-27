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
    args = parser.parse_args()
    os.umask(0o077)
    result = upgrade(yaml.safe_load(args.source.read_text()), args.gateway)
    with args.out.open("x") as file:
        file.write(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    print("已生成草稿；尚未导入或发布")


if __name__ == "__main__":
    main()
