"""未发布副本可以先预览：试用绑定当前受管草稿，不要求先有正式路由。

发布的顺序不能反过来被"想试用"绑架：客户改完提示词、想先跑一遍再决定发不发。
所以这里断言的不是"试用能跑"（那由已发布路径的用例覆盖同一段执行代码），
而是**构造试用时不需要正式路由**，且它仍然绑在这个客户的这个副本上。
"""

import pytest
from execution.test_app_runtime_dify_entry import dify_chat

from aiops_diagnostics.consumer.consumer_trial import ConsumerTrial
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from consumer.test_consumer_flow_contract import BASE
from consumer.test_consumer_knowledge_picker import picker_graph

APP = "aaaaaaaa-1111-2222-3333-444444444444"


@pytest.fixture
def draft_env(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        settings = env["settings"]
        settings.chat_execution_bindings_file = ""
        settings.chat_route_bindings_file = str(env["db"])
        settings.public_base_url = BASE
        settings.dify_console_base_url = "http://dify.invalid"
        settings.dify_console_api_key = "console-key"
        settings.consumer_publication_targets_file = ""
        context = env["app"].state.gateway
        monkeypatch.setattr(context.dify_workspaces, "resolve", lambda *args: "workspace-a")
        SelfServiceStore(settings.database_file).register(
            app_id=APP,
            tenant_id="TENANT-A",
            workspace_id="workspace-a",
            agent_name="consumer-" + APP,
            profile_id="",
            actor="fixture",
            entry="consumer",
            environment=settings.execution_environment,
            targets={
                "knowledge_base_ids": ["kb-own"],
                "live_order_status": True,
                "live_billing_record": True,
            },
        )
        env["flow"] = picker_graph(["kb-own"])
        env["context"] = context
        yield env


def test_unpublished_managed_copy_can_be_previewed(draft_env):
    from aiops_diagnostics.gateway import gateway_api

    context = draft_env["context"]
    binding, route, workspace = gateway_api._managed_app_draft(context, APP)
    assert route.state == "draft" and route.revision == 0 and route.app_id == APP
    assert route.agent_version_no == 0 and route.agent_id == ""
    assert binding.tenant_id == "TENANT-A" and workspace == "workspace-a"
    # 合成路由**不是**一条正式绑定：正式指针表里仍然什么都没有
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    assert ChatRouteBindingStore(draft_env["settings"].chat_route_bindings_file).all() == ()


def test_draft_preview_requires_the_copy_to_still_be_managed(draft_env):
    from aiops_diagnostics.gateway import gateway_api

    context = draft_env["context"]
    _, route, _ = gateway_api._managed_app_draft(context, APP)
    trial = ConsumerTrial("", None, route, {"content": {}, "knowledge_base_ids": ["kb-own"]}, "q", draft=True)
    trial.require_current(draft_env["settings"])
    with draft_env["settings"].database_file.open("rb"):
        pass
    store = SelfServiceStore(draft_env["settings"].database_file)
    with store.connection() as db:
        db.execute("DELETE FROM operator_apps WHERE app_id=?", (APP,))
    with pytest.raises(ValueError, match="no longer managed"):
        trial.require_current(draft_env["settings"])


def test_draft_preview_is_refused_for_an_unknown_or_operator_app(draft_env):
    from aiops_diagnostics.gateway import gateway_api

    context = draft_env["context"]
    with pytest.raises(gateway_api.StandardAPIError):
        gateway_api._managed_app_draft(context, "bbbbbbbb-1111-2222-3333-444444444444")
    SelfServiceStore(draft_env["settings"].database_file).register(
        app_id="cccccccc-1111-2222-3333-444444444444",
        tenant_id="TENANT-A",
        workspace_id="workspace-a",
        agent_name="operator-x",
        profile_id="default",
        actor="fixture",
        entry="operator",
        environment=draft_env["settings"].execution_environment,
    )
    with pytest.raises(gateway_api.StandardAPIError, match="draft preview is for consumer apps"):
        gateway_api._managed_app_draft(context, "cccccccc-1111-2222-3333-444444444444")
