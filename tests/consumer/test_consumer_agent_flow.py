"""新版发布不能把回调授权或库选择交给模型。"""

import copy
import importlib.util
from pathlib import Path

import pytest

from aiops_diagnostics.consumer.consumer_flow_contract import ConsumerFlowError, publication_settings
from consumer.test_consumer_flow_contract import BASE, CEILING
from consumer.test_consumer_knowledge_picker import picker_graph

spec = importlib.util.spec_from_file_location(
    "consumer_upgrade", Path(__file__).parents[2] / "tools/content/upgrade_consumer_flow.py"
)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def candidate():
    return upgrade.agent_conversation(
        {"workflow": picker_graph(["kb-own"])},
        "trendpower/consumer_knowledge:0.0.2@" + "a" * 64,
    )["workflow"]


def publish(flow):
    return publication_settings(
        flow, CEILING, BASE, owned_libraries=[{"knowledge_base_id": "kb-own", "audience": "customer"}]
    )


def test_agent_migration_preserves_business_prompt_model_and_permission_ceiling():
    flow = candidate()
    settings = publish(flow)
    assert settings["callback_version"] == 6 and settings["knowledge_base_ids"] == ["kb-own"]
    node = flow["graph"]["nodes"][1]["data"]
    assert node["agent_parameters"]["model"]["value"]["model"]
    assert "{{fact:" not in node["agent_parameters"]["instruction"]["value"]
    ceiling = publication_settings(
        flow,
        {**CEILING, "live_order_status": False},
        BASE,
        owned_libraries=[{"knowledge_base_id": "kb-own", "audience": "customer"}],
    )
    assert not ceiling["live_order_status"]


@pytest.mark.parametrize(
    "change",
    [
        "grant_auto",
        "grant_prompt",
        "foreign_tool",
        "foreign_library",
        "unbounded",
        "hidden_history",
        "grant_description",
    ],
)
def test_publication_refuses_broader_authority(change):
    flow = copy.deepcopy(candidate())
    node = flow["graph"]["nodes"][1]["data"]
    params = node["agent_parameters"]
    tool = params["tools"]["value"][0]
    if change == "grant_auto":
        tool["parameters"]["data_grant"]["auto"] = 1
    elif change == "grant_prompt":
        params["instruction"]["value"] += "{{#start.data_grant#}}"
    elif change == "grant_description":
        tool["extra"] = {"description": "{{#start.data_grant#}}"}
    elif change == "foreign_tool":
        tool["provider_name"] = "untrusted/tool/provider"
    elif change == "foreign_library":
        tool["settings"]["knowledge_base_ids"]["value"] = ["other-tenant"]
    elif change == "unbounded":
        params["maximum_iterations"]["value"] = 500
    else:
        node["memory"] = {"window": {"enabled": True, "size": 10}}
    with pytest.raises(ConsumerFlowError):
        publish(flow)


def test_new_tenant_copy_replaces_agent_library_and_bootstraps_model():
    from aiops_diagnostics.consumer.consumer_publication import answer_model, answer_prompt
    from aiops_diagnostics.dify.self_service_prepare import consumer_source_ids, rebind_knowledge

    document = {"workflow": candidate()}
    assert consumer_source_ids(document) == ("kb-own",)
    assert answer_model(document) and answer_prompt(document)
    copied = rebind_knowledge(document, ["kb-customer"], forbidden_ids=["kb-own"])
    assert consumer_source_ids(copied) == ("kb-customer",)


def test_legacy_protocol_is_not_silently_retained_by_agent_migration():
    source = {"workflow": picker_graph(["kb-own"])}
    answer = next(n["data"] for n in source["workflow"]["graph"]["nodes"] if n["id"] == "answer")
    answer["prompt_template"] = [
        {"role": "system", "text": "按用户语言简短答复。"},
        {"role": "system", "text": "<!-- aiops-protocol:2 -->仅输出JSON。"},
    ]
    flow = upgrade.agent_conversation(source, "trendpower/consumer_knowledge:0.0.2@" + "a" * 64)["workflow"]
    instruction = flow["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]["value"]
    assert instruction.startswith("按用户语言简短答复。")
    assert "<!-- aiops-protocol:" not in instruction and "仅输出JSON。" not in instruction


def test_new_tenant_installs_source_agent_and_tool_versions():
    from aiops_diagnostics.consumer.consumer_agent_flow import AGENT_PLUGIN
    from aiops_diagnostics.dify.self_service_prepare import consumer_models

    flow = candidate()
    model = flow["graph"]["nodes"][1]["data"]["agent_parameters"]["model"]["value"]
    installed = []

    class Console:
        def install_plugins(self, workspace, identifiers):
            installed.extend(identifiers)

        def _call(self, *args):
            return {"current_credential_id": "existing-model"}

    assert consumer_models(Console(), "target-workspace", {"workflow": flow}, {"model": {}}) == [
        model["provider"] + "/" + model["model"]
    ]
    assert installed == [AGENT_PLUGIN, "trendpower/consumer_knowledge:0.0.2@" + "a" * 64]
