"""从问答和发布入口验证绑定来源、版本更新与撤权。"""

import copy
from dataclasses import replace

import pytest
from execution.test_app_runtime_dify_entry import dify_chat
from execution.test_app_runtime_node_entry import _Resolver

from aiops_diagnostics.consumer import consumer_binding
from aiops_diagnostics.execution import chat_execution
from consumer.test_consumer_chat_service import ask, configure, done
from consumer.test_consumer_knowledge_flow import Workflow
from consumer.test_consumer_knowledge_flow import ask as ask_knowledge
from consumer.test_consumer_publication import Console, prepare, publish


def test_file_binding_survives_an_empty_management_store(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        env["settings"].chat_route_bindings_file = str(env["db"])
        response = ask(env)
        assert response.status_code == 202
        final = done(env, response.json()["qa_id"])
        assert final.status_code == 200 and final.json()["status"] == "completed", final.text


@pytest.mark.parametrize("stage", ["before_read", "after_read", "completed"])
def test_publication_keeps_pinned_answer_but_retraction_refuses_it(tmp_path, monkeypatch, stage):
    from consumer.test_consumer_publication import APP, PUBLISH

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        prepare(env, monkeypatch, tmp_path)
        monkeypatch.setattr(Console, "draft", copy.deepcopy(Console.draft))
        assert publish(env).status_code == 200

        def next_version():
            Console.draft["nodes"][0]["data"]["prompt"] = "SECOND_STYLE"
            response = publish(env)
            assert response.status_code == 200, response.text

        if stage == "before_read":
            original = Workflow.run

            def run(self, **kwargs):
                next_version()
                return original(self, **kwargs)

            monkeypatch.setattr(Workflow, "run", run)
        elif stage == "after_read":
            Workflow.after = next_version
        qa = ask_knowledge(env).json()["qa_id"]
        final = done(env, qa)
        assert final.status_code == 200 and final.json()["status"] == "completed", final.text
        if stage == "completed":
            next_version()
        assert done(env, qa).json()["result"] == final.json()["result"]
        assert (
            env["client"].post("/v1/dify/retract", headers=PUBLISH, json={"app_id": APP}).status_code == 200
        )
        assert done(env, qa).status_code == 410


@pytest.mark.parametrize("change", ["app_id", "workflow_key_file", "knowledge_base_ids", "live_order_status"])
def test_new_revision_cannot_hide_authorization_changes(tmp_path, monkeypatch, change):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _, _, _ = prepare(env, monkeypatch, tmp_path)
        assert publish(env).status_code == 200
        caller = _Resolver().resolve("token-a", required_scope="aiops:orders:read")
        old = chat_execution.select_binding(runtime.gateway_settings, caller, scenario="consumer_service")
        value = {
            "app_id": "other-app",
            "workflow_key_file": "/other/key",
            "knowledge_base_ids": (),
            "live_order_status": False,
        }[change]
        selected = replace(old, route_revision=old.route_revision + 1, **{change: value})
        monkeypatch.setattr(chat_execution, "select_binding", lambda *a, **kw: selected)
        with pytest.raises(chat_execution.ChatRunUnavailable):
            consumer_binding.require_pinned(runtime, caller, old)
