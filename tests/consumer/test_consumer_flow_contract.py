"""发布契约拒绝空壳节点，开关随不可变版本冻结。"""

import copy
from pathlib import Path

import pytest
import yaml

from aiops_diagnostics.consumer.consumer_flow_contract import publication_settings
from aiops_diagnostics.dify_dsl_pull import workflow_content

BASE = "https://gateway.example.test"
CEILING = {"live_order_status": True, "live_billing_record": True}


def graph():
    text = (
        (Path(__file__).parents[2] / "deploy/dify-chat/consumer-service.dsl.yml")
        .read_text()
        .replace("http://gateway.invalid", BASE)
    )
    return workflow_content(text)


def test_template_freezes_both_flags_and_server_ceiling():
    flow = graph()
    assert publication_settings(flow, CEILING, BASE)["callback_version"] == 2
    flow["environment_variables"][0]["value"] = "false"
    result = publication_settings(flow, CEILING, BASE)
    assert result["live_order_status"] is False and result["live_billing_record"] is True
    assert (
        publication_settings(graph(), {**CEILING, "live_order_status": False}, BASE)["live_order_status"]
        is False
    )


@pytest.mark.parametrize(
    "change",
    [
        "remove",
        "url",
        "auth",
        "body",
        "retry",
        "tls",
        "edge",
        "condition",
        "model_token",
        "aggregate",
        "input",
        "flag",
        "photo_token",
    ],
)
def test_bad_graph_is_refused(change):
    flow = graph()
    nodes = {n["id"]: n["data"] for n in flow["graph"]["nodes"]}
    if change == "remove":
        flow["graph"]["nodes"].pop()
    elif change == "url":
        nodes["cpms_status"]["url"] = "https://wrong.example.test/v1/internal/consumer-data/status"
    elif change == "auth":
        nodes["cpms_status"]["headers"] = "Authorization: Bearer fixed"
    elif change == "body":
        nodes["cpms_status"]["body"] = {"type": "json", "data": '{"order_no":"wrong"}'}
    elif change == "retry":
        nodes["cpms_status"]["retry_config"]["retry_enabled"] = True
    elif change == "tls":
        nodes["cpms_status"]["ssl_verify"] = False
    elif change == "edge":
        flow["graph"]["edges"][0]["target"] = "end"
    elif change == "condition":
        nodes["data_route"]["cases"][0]["conditions"][0]["variable_selector"] = ["sys", "query"]
    elif change == "model_token":
        nodes["answer"]["prompt_template"][0]["text"] += "{{#start.data_grant#}}"
    elif change == "aggregate":
        nodes["data_context"]["variables"] = [["knowledge", "body"]]
    elif change == "input":
        nodes["start"]["variables"].pop()
    elif change == "photo_token":
        nodes["photo_context"]["variables"] = [["start", "data_grant"]]
    elif change == "flag":
        flow["environment_variables"][0]["value"] = "maybe"
    with pytest.raises(ValueError, match="WF01编排不完整"):
        publication_settings(flow, CEILING, BASE)


def test_old_graph_is_preserved_but_cannot_downgrade_new_publication():
    old = {"graph": {"nodes": [], "edges": []}}
    assert publication_settings(old, CEILING, "") == CEILING
    with pytest.raises(ValueError):
        publication_settings(old, CEILING, BASE, require_split=True)


def test_publish_checks_before_native_publish_and_freezes_graph_flags(tmp_path, monkeypatch):
    from test_app_runtime_dify_entry import dify_chat
    from test_app_runtime_node_entry import _Resolver

    from aiops_diagnostics.chat_execution import select_binding
    from consumer.test_consumer_publication import Console, prepare, publish

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, content, routes = prepare(env, monkeypatch, tmp_path)
        flow = graph()
        monkeypatch.setattr(Console, "draft", flow["graph"])
        original = Console.export_dsl

        def export(self, *args, **kwargs):
            d = yaml.safe_load(original(self, *args, **kwargs))
            d["workflow"]["environment_variables"] = copy.deepcopy(flow["environment_variables"])
            return yaml.safe_dump(d)

        monkeypatch.setattr(Console, "export_dsl", export)
        env["settings"].public_base_url = BASE
        assert publish(env).status_code == 200
        caller = _Resolver().resolve("token-a", required_scope="aiops:orders:read")
        before = select_binding(env["settings"], caller, scenario="consumer_service")
        assert before.callback_version == 2 and before.live_order_status
        flow["environment_variables"][0]["value"] = "false"
        assert select_binding(env["settings"], caller, scenario="consumer_service").live_order_status
        assert publish(env).status_code == 200
        after = select_binding(env["settings"], caller, scenario="consumer_service")
        assert not after.live_order_status and after.live_billing_record
        count = Console.publishes
        Console.draft["nodes"].pop()
        refused = publish(env)
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "CONSUMER_FLOW_INVALID"
        assert "必需节点" in refused.json()["error"]["message"]
        assert (
            Console.publishes == count
            and select_binding(env["settings"], caller, scenario="consumer_service") == after
        )


def test_upgrade_keeps_real_model_prompt_and_disabled_photo_features(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "upgrade_flow", Path(__file__).parents[2] / "tools/upgrade_consumer_flow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = yaml.safe_load(
        (Path(__file__).parents[2] / "deploy/dify-chat/consumer-service.dsl.yml").read_text()
    )
    nodes = {n["id"]: n["data"] for n in source["workflow"]["graph"]["nodes"]}
    nodes["answer"]["model"]["name"] = "existing-model"
    nodes["answer"]["prompt_template"][1]["text"] = "existing {{#knowledge.body#}}"
    source["workflow"]["features"]["file_upload"]["enabled"] = False
    original = copy.deepcopy(source)
    result = module.upgrade(source, BASE)
    output = {n["id"]: n["data"] for n in result["workflow"]["graph"]["nodes"]}
    assert source == original
    assert output["answer"]["model"]["name"] == "existing-model"
    assert output["answer"]["prompt_template"][1]["text"] == "existing {{#data_context.output#}}"
    assert result["workflow"]["features"] == source["workflow"]["features"]
    assert (
        publication_settings(workflow_content(yaml.safe_dump(result)), CEILING, BASE)["callback_version"] == 2
    )
    source["workflow"]["graph"]["nodes"].append({"id": "custom", "data": {"type": "llm"}})
    with pytest.raises(ValueError, match="拒绝覆盖"):
        module.upgrade(source, BASE)
