"""客户端依赖与业务连接（R11）：开通就把实际依赖准备到**可回读**，而不是只复制 DSL。

独立验收的反例：只为租户 A 登记了 CPMS，开通租户 B 仍返回 `completed/published=true`，
而 `ConsumerToolGrants._cpms_base(B)` 报 `TENANT_ENDPOINT_UNRESOLVED` —— 客户第一次查单才会
发现。这里断言：有已批准连接 ⇒ 登记并解析；没有 ⇒ **这一端直接失败**，不谎报就绪。
"""

from types import SimpleNamespace

import pytest

from aiops_diagnostics.consumer.consumer_knowledge_grants import ConsumerEndpoints, approved_endpoint
from aiops_diagnostics.consumer.consumer_tool_grants import ConsumerToolGrants, ConsumerToolRefused
from aiops_diagnostics.dify import self_service_prepare as prepare
from aiops_diagnostics.dify.self_service_identity import SetupRefused


def settings(tmp_path, **overrides):
    body = {
        "database_file": str(tmp_path / "gateway.db"),
        "execution_environment": "env-a",
        "charging_status_base_url": "",
    }
    body.update(overrides)
    return SimpleNamespace(**body)


def test_approved_environment_connection_registers_the_target_tenant(tmp_path):
    """裸 URL = 本环境已批准的统一连接：登记到目标租户，解析得到同一个地址。"""
    s = settings(tmp_path, charging_status_base_url="https://cpms.example.test")
    assert prepare.consumer_endpoint(SimpleNamespace(settings=s), "TENANT-B") == ("https://cpms.example.test")
    grants = ConsumerToolGrants(SimpleNamespace(), s)
    assert grants._cpms_base("TENANT-B") == "https://cpms.example.test"


def test_per_tenant_map_only_authorizes_the_listed_tenant(tmp_path):
    s = settings(tmp_path, charging_status_base_url="TENANT-A:https://a.example.test")
    assert approved_endpoint(s.charging_status_base_url, "TENANT-A") == "https://a.example.test"
    assert approved_endpoint(s.charging_status_base_url, "TENANT-B") == ""
    with pytest.raises(SetupRefused, match="SETUP_BUSINESS_CONNECTION_REQUIRED"):
        prepare.consumer_endpoint(SimpleNamespace(settings=s), "TENANT-B")
    # 没有登记 ⇒ 运行时也拒绝，不回退到"某个全局 URL"
    with pytest.raises(ConsumerToolRefused) as refused:
        ConsumerToolGrants(SimpleNamespace(), s)._cpms_base("TENANT-B")
    assert refused.value.code == "TENANT_ENDPOINT_UNRESOLVED"


def test_registration_is_idempotent_and_overwrites_with_the_approved_connection(tmp_path):
    s = settings(tmp_path, charging_status_base_url="https://cpms.example.test")
    context = SimpleNamespace(settings=s)
    prepare.consumer_endpoint(context, "TENANT-B")
    prepare.consumer_endpoint(context, "TENANT-B")
    assert ConsumerEndpoints(s.database_file).get("TENANT-B", "env-a") == "https://cpms.example.test"


class Console:
    def __init__(self, installed=None, credentials=None, model_credentials=None):
        self.installed = list(installed or [])
        self.credentials: dict[str, list[dict]] = credentials or {}
        self.model_credentials = model_credentials or {}
        self.calls = []

    def install_plugins(self, workspace_id, identifiers):
        self.calls.append(("install", identifiers))
        for item in identifiers:
            if item not in self.installed:
                self.installed.append(item)

    def _call(self, workspace_id, path, *, method="GET", body=None, follow_redirects=True):
        self.calls.append((method, path))
        provider = ""
        if path.startswith("/console/api/workspaces/current/tool-provider"):
            # .../tool-provider/builtin/<provider>/credentials
            provider = path.split("/")[7]
        if path.startswith("/console/api/workspaces/current/tool-provider"):
            if path.endswith("/credentials"):
                return {"credentials": self.credentials.get(provider, [])}
            if path.endswith("/add"):
                self.credentials.setdefault(provider, []).append({"id": "c1", **(body or {})})
                return {"result": "success"}
        if path.startswith("/console/api/workspaces/current/model-providers/"):
            # .../model-providers/<provider 可含斜杠>/models/credentials
            head = "/console/api/workspaces/current/model-providers/"
            name = path[len(head) :].split("/models/credentials")[0]
            return {"current_credential_id": self.model_credentials.get(name, "")}
        raise AssertionError(f"unexpected call {method} {path}")


def plugin_settings():
    return SimpleNamespace(
        consumer_knowledge_plugin="trendpower/consumer_knowledge:0.0.1@sha",
        public_base_url="https://api.example.test",
    )


def test_plugin_installation_writes_the_workspace_catalogue_credential_once():
    console = Console()
    provider = prepare.consumer_plugin(console, "workspace-target", plugin_settings(), "ckc_token")
    assert provider == "trendpower/consumer_knowledge:0.0.1"
    assert console.installed == ["trendpower/consumer_knowledge:0.0.1@sha"]
    added = [c for c in console.calls if c[0] == "POST" and c[1].endswith("/add")]
    assert len(added) == 1
    # 再跑一次：已存在同样的凭据 ⇒ 不再写
    prepare.consumer_plugin(console, "workspace-target", plugin_settings(), "ckc_token")
    added = [c for c in console.calls if c[0] == "POST" and c[1].endswith("/add")]
    assert len(added) == 1


def test_plugin_without_configuration_refuses_instead_of_publishing():
    console = Console()
    with pytest.raises(SetupRefused, match="SETUP_CONSUMER_PLUGIN_REQUIRED"):
        prepare.consumer_plugin(
            console,
            "workspace-target",
            SimpleNamespace(consumer_knowledge_plugin="", public_base_url="https://api.example.test"),
            "ckc_token",
        )


def document(provider="langgenius/openai_api_compatible/openai_api_compatible", model="deepseek-v4-flash"):
    return {
        "workflow": {
            "graph": {
                "nodes": [
                    {"id": "answer", "data": {"type": "llm", "model": {"provider": provider, "name": model}}}
                ]
            }
        }
    }


def test_all_models_of_the_source_graph_must_have_a_credential():
    """源编排用到的每个模型都要在本工作区有凭据；缺一个就不说"资源就绪"。"""
    profile = {"model": {"provider": "langgenius/openai_api_compatible/openai_api_compatible"}}
    console = Console(model_credentials={"langgenius/openai_api_compatible/openai_api_compatible": "mc-1"})
    assert prepare.consumer_models(console, "workspace-target", document(), profile) == [
        "langgenius/openai_api_compatible/openai_api_compatible/deepseek-v4-flash"
    ]
    console.model_credentials.pop("langgenius/openai_api_compatible/openai_api_compatible")
    with pytest.raises(SetupRefused, match="SETUP_MODEL_CONFLICT"):
        prepare.consumer_models(console, "workspace-target", document(), profile)


def test_model_of_an_unapproved_provider_is_refused_not_silently_copied():
    profile = {"model": {"provider": "langgenius/openai_api_compatible/openai_api_compatible"}}
    console = Console(model_credentials={})
    with pytest.raises(SetupRefused, match="SETUP_MODEL_CONNECTION_REQUIRED"):
        prepare.consumer_models(
            console, "workspace-target", document(provider="someone/else/provider"), profile
        )


def test_no_llm_node_is_refused():
    with pytest.raises(SetupRefused, match="SETUP_MODEL_CONNECTION_REQUIRED"):
        prepare.consumer_models(
            Console(), "workspace-target", {"workflow": {"graph": {"nodes": []}}}, {"model": {}}
        )
