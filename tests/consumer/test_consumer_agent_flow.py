"""新版发布不能把回调授权或库选择交给模型。"""

import copy
import hashlib
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


@pytest.mark.parametrize("suffix", ["", "\n运营要求：核心结论应安抚并说明有无问题；不用Markdown，末尾明细。"])
@pytest.mark.parametrize("version", [0, 1, 2, 3, 4, 5, 6])
def test_order_composition_migration_is_exact_idempotent_and_preserves_business(suffix, version):
    from aiops_diagnostics.consumer import consumer_agent_flow as flow

    source = {"workflow": candidate(), "dependencies": [{"unchanged": True}]}
    instruction = source["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]
    business = "运营自定义：请使用原有措辞。\n\n"
    instruction["value"] = business + flow.BASE_INSTRUCTION + suffix
    if version == 1:
        instruction["value"] = (
            business + flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V1 + suffix
        )
    elif version == 2:
        # 已发布1.7.2的完整v2字节合同，避免改动旧块后真实图无法升级。
        assert hashlib.sha256(flow.ORDER_COMPOSITION_INSTRUCTION_V2.encode()).hexdigest() == (
            "08647e7f1a6030c00f28dc80aa977487f6bfb5f4e23bc41c3b15c14be401eded"
        )
        instruction["value"] += ("\n" if suffix else "") + flow.ORDER_COMPOSITION_INSTRUCTION_V2
    elif version == 3:
        # 已发布1.7.3的完整v3字节合同，保留真实图升级输入。
        assert hashlib.sha256(flow.ORDER_COMPOSITION_INSTRUCTION_V3.encode()).hexdigest() == (
            "ba5a4472490c4cce1d349eaead1d4086580f60aa5bce271eaf3c8453e1f2fd4c"
        )
        instruction["value"] += ("\n" if suffix else "") + flow.ORDER_COMPOSITION_INSTRUCTION_V3
    elif version == 4:
        assert hashlib.sha256(flow.ORDER_COMPOSITION_INSTRUCTION_V4.encode()).hexdigest() == (
            "b229955ccf22f797afa77f52b116d0e9c4c77eff2807b63b65c6affd48dfe4fd"
        )
        instruction["value"] += ("\n" if suffix else "") + flow.ORDER_COMPOSITION_INSTRUCTION_V4
    elif version == 5:
        # 已发布1.7.5的完整v5字节合同，保留真实图升级输入。
        assert hashlib.sha256(flow.ORDER_COMPOSITION_INSTRUCTION_V5.encode()).hexdigest() == (
            "5289614b36a7eb83d22676a6a5e023a43c1e28c21458465a9e73e7996cbfc8ad"
        )
        instruction["value"] += ("\n" if suffix else "") + flow.ORDER_COMPOSITION_INSTRUCTION_V5
    elif version == 6:
        instruction["value"] += ("\n" if suffix else "") + flow.ORDER_COMPOSITION_INSTRUCTION
    original = copy.deepcopy(source)
    updated = upgrade.order_composition(source)
    assert source == original
    assert upgrade.order_composition(updated) == updated
    expected = copy.deepcopy(source)
    expected["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]["value"] = (
        business
        + flow.BASE_INSTRUCTION
        + suffix
        + ("\n" if suffix else "")
        + flow.ORDER_COMPOSITION_INSTRUCTION
    )
    assert updated == expected
    assert updated["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"][
        "value"
    ].endswith("<!-- /aiops-order-composition:6 -->\n")
    assert publish(updated["workflow"])["callback_version"] == 6
    for invalid in (
        flow.BASE_INSTRUCTION.replace("8次", "9次").replace("你是充电客服", "你是客服"),
        flow.BASE_INSTRUCTION * 2,
        flow.BASE_INSTRUCTION + "<!-- aiops-order-composition:2 -->",
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V1 * 2,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V2 * 2,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V3 * 2,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V4 * 2,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V5 * 2,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V4 + flow.ORDER_COMPOSITION_INSTRUCTION,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V3 + flow.ORDER_COMPOSITION_INSTRUCTION,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V2 + flow.ORDER_COMPOSITION_INSTRUCTION,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V1 + flow.ORDER_COMPOSITION_INSTRUCTION,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION_V5 + flow.ORDER_COMPOSITION_INSTRUCTION,
        flow.BASE_INSTRUCTION + flow.ORDER_COMPOSITION_INSTRUCTION.replace("币种", "单位", 1),
        flow.BASE_INSTRUCTION + "<!-- aiops-order-composition:3 -->",
        flow.BASE_INSTRUCTION + "<!-- aiops-order-composition:4 -->",
        flow.BASE_INSTRUCTION + "<!-- aiops-order-composition:5 -->",
        flow.BASE_INSTRUCTION + "<!-- aiops-order-composition:6 -->",
        flow.BASE_INSTRUCTION + "运营文本" * 4000,
    ):
        instruction["value"] = business + invalid + suffix
        with pytest.raises(ValueError):
            upgrade.order_composition(source)
    with pytest.raises(ValueError, match="v6"):
        upgrade.order_composition({"workflow": picker_graph(["kb-own"])})


def test_migration_removes_known_arithmetic_permission_without_replacing_business_text():
    from aiops_diagnostics.consumer import consumer_agent_flow as flow

    source = {"workflow": candidate()}
    instruction = source["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]
    business = (
        "金额核对只报告工具原值及可核对的算术差额。其余风格保留。\n"
        "简短依据：只报告工具原值及可核对的算术差额；未知原因如实说明。"
    )
    instruction["value"] = flow.BASE_INSTRUCTION + business + "\n"
    expected = copy.deepcopy(source)
    replacement = "只报告同名字段原值；金额不同仅并列原值，不计算差额"
    expected["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]["value"] = (
        flow.BASE_INSTRUCTION
        + business.replace("只报告工具原值及可核对的算术差额", replacement)
        + "\n"
        + flow.ORDER_COMPOSITION_INSTRUCTION
    )
    original = copy.deepcopy(source)
    changed = upgrade.order_composition(source)
    assert changed == expected
    assert source == original
    assert upgrade.order_composition(changed) == changed


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
        "missing_grant",
        "missing_query_context",
        "malformed_parameters",
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
    elif change == "malformed_parameters":
        tool["parameters"] = ["data_grant"]
    elif change == "missing_grant":
        tool["parameters"]["data_grant"] = {"auto": 0, "value": {"type": "mixed", "value": ""}}
    elif change == "missing_query_context":
        params["query"] = {"type": "constant", "value": "{{#sys.query#}}"}
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
    from aiops_diagnostics.dify.self_service_prepare import consumer_models, consumer_plugins

    flow = candidate()
    model = flow["graph"]["nodes"][1]["data"]["agent_parameters"]["model"]["value"]
    installed = []

    class Console:
        def install_plugins(self, workspace, identifiers):
            installed.extend(identifiers)

        def _call(self, *args):
            return {"current_credential_id": "existing-model"}

    installed.extend(consumer_plugins({"workflow": flow}))
    assert consumer_models(Console(), "target-workspace", {"workflow": flow}, {"model": {}}) == [
        model["provider"] + "/" + model["model"]
    ]
    assert installed == [AGENT_PLUGIN, "trendpower/consumer_knowledge:0.0.2@" + "a" * 64]


@pytest.mark.parametrize("query_type", ["mixed", "constant"])
def test_agent_editor_query_serialization_preserves_runtime_templates(query_type):
    flow = candidate()
    flow["graph"]["nodes"][1]["data"]["agent_parameters"]["query"]["type"] = query_type
    assert publish(flow)["callback_version"] == 6


def test_catalogue_failure_details_do_not_become_public_contract_errors(monkeypatch):
    marker = "SYNTHETIC_UPSTREAM_SECRET"

    from aiops_diagnostics.knowledge import knowledge_retrieval

    client = knowledge_retrieval.KbServiceClient("https://kb.example", tenant_id="fixture")
    monkeypatch.setattr(
        knowledge_retrieval,
        "request_json",
        lambda *a, **k: [{"id": "kb-own", "document_count": marker}],
    )
    with pytest.raises(ConsumerFlowError) as error:
        publication_settings(candidate(), CEILING, BASE, owned_libraries=client.list_knowledge_bases)
    assert marker not in str(error.value)
    assert "知识库目录暂不可用" in str(error.value)


def test_migration_replaces_known_reassurance_demand_without_losing_business_style():
    from aiops_diagnostics.consumer import consumer_agent_flow as flow

    source = {"workflow": candidate()}
    instruction = source["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]
    unsafe = (
        "核心结论：第一句给出对费用的解释结论，能够让消费者清楚知道订单是否有问题，并且简要概括问题来源，"
        "并作出说明，说明是用于安抚用户的话，不是站在用户角度批判金额核算内容。"
    )
    style = "不使用Markdown。简短依据、具体详情依次列出。"
    instruction["value"] = flow.BASE_INSTRUCTION + unsafe + style
    changed = upgrade.order_composition(source)
    text = changed["workflow"]["graph"]["nodes"][1]["data"]["agent_parameters"]["instruction"]["value"]
    assert unsafe not in text
    assert style in text and flow.BASE_INSTRUCTION in text
    assert upgrade.order_composition(changed) == changed
