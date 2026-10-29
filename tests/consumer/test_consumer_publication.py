"""从原发布入口控制WF01内容、能力与真实会话放行。"""

import copy
import json

import pytest
import yaml
from test_app_runtime_dify_entry import dify_chat
from test_node_publication import publication as publication
from test_node_publication import publish as publish_node

from aiops_diagnostics.chat_execution import SCENARIO_CONSUMER_SERVICE
from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.dify_app_registry import DifyAppBinding
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig
from aiops_diagnostics.publication.agent_manifest import admin_context
from consumer.test_consumer_chat_service import done
from consumer.test_consumer_knowledge_flow import ask, setup, wire

APP = "11111111-2222-3333-4444-555555555555"
PUBLISH = {"Authorization": "Bearer publication-key"}


class Console:
    versions = {}
    draft = {"nodes": [{"id": "sop", "data": {"type": "llm", "prompt": "V1"}}], "edges": []}
    publishes = 0

    def __init__(self, source):
        pass

    def export_dsl(self, app_id=None, *, workflow_id=None):
        graph = self.versions[workflow_id] if workflow_id else self.draft
        return yaml.safe_dump({"app": {"mode": "advanced-chat"}, "workflow": {"graph": graph}})

    def publish_workflow(self, app_id=None):
        Console.publishes += 1
        self.versions[self.published_workflow_id()] = copy.deepcopy(self.draft)
        return True

    def published_workflow_id(self, app_id=None):
        return f"00000000-0000-0000-0000-{self.publishes:012d}"


def prepare(env, monkeypatch, tmp_path):
    runtime, kb, content = setup(env, monkeypatch, tmp_path)
    wire(env, monkeypatch)
    context = env["app"].state.gateway
    env["settings"].chat_execution_bindings_file = ""
    env["settings"].chat_route_bindings_file = str(env["db"])
    env["settings"].chat_dify_key_dir = str(tmp_path / "keys")
    key = tmp_path / "keys" / (APP + ".key")
    key.parent.mkdir()
    key.write_text("test-key")
    key.chmod(0o600)
    env["settings"].dify_publish_api_key = "publication-key"
    env["settings"].dify_console_base_url = "http://dify.invalid"
    env["settings"].dify_console_api_key = "console-key"
    target = tmp_path / "consumer-targets.json"
    target.write_text(
        json.dumps(
            {
                "schema": "aiops-consumer-targets/1",
                "targets": [
                    {
                        "tenant_id": "TENANT-A",
                        "environment": "env-a",
                        "app_id": APP,
                        "content_file": str(content),
                        "knowledge_base_ids": ["kb-own"],
                        "live_order_status": True,
                        "live_billing_record": True,
                    }
                ],
            }
        )
    )
    target.chmod(0o600)
    env["settings"].consumer_publication_targets_file = str(target)
    monkeypatch.setattr(context.dify_workspaces, "resolve", lambda *args: "workspace-a")
    agent = context.agent_manager.create(
        admin_context("TENANT-A"),
        name="WF01",
        description="test",
        config=AgentConfig(
            agent_type="customer",
            prompt="original",
            knowledge_base_ids=(),
            model="aiops-api",
            output_contract="blocks-v1",
        ),
    )
    context.agent_manager.publish(admin_context("TENANT-A"), agent.agent_id, expected_revision=agent.revision)
    runtime.dify_app_registry.put(DifyAppBinding("TENANT-A", "consumer", APP, "WF01"))
    Console.versions, Console.publishes = {}, 0
    monkeypatch.setattr("aiops_diagnostics.dify_dsl_pull.DifyDslClient", Console)
    return runtime, kb, content, ChatRouteBindingStore(env["db"])


def publish(env):
    return env["client"].post("/v1/dify/publish", headers=PUBLISH, json={"app_id": APP})


def test_publish_draft_retract_restore_control_the_original_app(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _, content, routes = prepare(env, monkeypatch, tmp_path)
        first = publish(env)
        assert first.status_code == 200, first.text
        version_one = first.json()["version_no"]
        row = routes.get("env-a", "TENANT-A", "consumer", SCENARIO_CONSUMER_SERVICE)
        assert row and row.workflow_id == Console(None).published_workflow_id()
        qa_one = ask(env).json()["qa_id"]
        assert done(env, qa_one).json()["result"]["blocks"][0]["text"] == "按设备提示启动。"
        draft = json.loads(content.read_text())
        draft["entries"]["KB-C-007"]["answers"]["zh"] = "这是审核后的第二版指引。"
        content.write_text(json.dumps(draft))
        unchanged = done(env, ask(env).json()["qa_id"]).json()
        assert unchanged["result"]["blocks"][0]["text"] == "按设备提示启动。"
        second = publish(env)
        assert second.status_code == 200 and second.json()["version_no"] != version_one, second.text
        qa_two = ask(env).json()["qa_id"]
        assert done(env, qa_two).json()["result"]["blocks"][0]["text"] == "这是审核后的第二版指引。"
        revision = routes.get("env-a", "TENANT-A", "consumer", SCENARIO_CONSUMER_SERVICE).revision
        assert publish(env).json()["action"] == "unchanged"
        assert routes.get("env-a", "TENANT-A", "consumer", SCENARIO_CONSUMER_SERVICE).revision == revision
        retracted = env["client"].post("/v1/dify/retract", headers=PUBLISH, json={"app_id": APP})
        assert retracted.status_code == 200, retracted.text
        assert ask(env).status_code == 410
        assert done(env, qa_two).status_code == 410
        restored = env["client"].post(
            "/v1/dify/restore", headers=PUBLISH, json={"app_id": APP, "version_no": version_one}
        )
        assert restored.status_code == 200, restored.text
        final = done(env, ask(env).json()["qa_id"]).json()
        assert final["result"]["blocks"][0]["text"] == "按设备提示启动。"
        assert routes.get("env-a", "TENANT-A", "consumer", "consumer_own_order_status") is None


def test_bad_draft_is_refused_without_replacing_published_content(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _, content, routes = prepare(env, monkeypatch, tmp_path)
        assert publish(env).status_code == 200
        before = routes.get("env-a", "TENANT-A", "consumer", SCENARIO_CONSUMER_SERVICE)
        draft = json.loads(content.read_text())
        draft["state"] = "draft"
        content.write_text(json.dumps(draft))
        assert publish(env).status_code == 409
        assert routes.get("env-a", "TENANT-A", "consumer", SCENARIO_CONSUMER_SERVICE) == before
        assert done(env, ask(env).json()["qa_id"]).json()["status"] == "completed"


@pytest.mark.parametrize(
    "retract_during_trial,disabled",
    [(False, False), (True, False), (False, True), (False, "latest"), (False, "delete-library")],
)
def test_chatflow_trial_uses_temporary_app_and_leaves_formal_state_unchanged(
    tmp_path, monkeypatch, retract_during_trial, disabled
):
    import time
    from pathlib import Path

    from test_app_runtime_node_entry import ORDER_A, _Resolver

    from consumer.test_consumer_knowledge_flow import Workflow

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, content, routes = prepare(env, monkeypatch, tmp_path)
        assert publish(env).status_code == 200
        before = routes.get("env-a", "TENANT-A", "consumer", SCENARIO_CONSUMER_SERVICE)
        draft = json.loads(content.read_text())
        draft["entries"]["KB-C-007"]["answers"]["zh"] = "试用草稿指引。"
        content.write_text(json.dumps(draft))
        if disabled and disabled != "delete-library":
            target_path = Path(env["settings"].consumer_publication_targets_file)
            targets = json.loads(target_path.read_text())
            targets["targets"][0]["live_order_status"] = False
            target_path.write_text(json.dumps(targets))
            monkeypatch.setattr(
                runtime,
                "classify_consumer",
                lambda *a, **k: {
                    "intent": "order_status",
                    "order_reference": "latest" if disabled == "latest" else "explicit",
                    "uses_context": False,
                },
            )
        caller = _Resolver().resolve("token-a", required_scope="aiops:orders:read")
        monkeypatch.setattr(runtime, "trial_context_for", lambda *args: caller)
        monkeypatch.setattr(runtime, "trial_run_context", lambda *args: caller)
        env["settings"].dify_trial_api_key = "trial-key"
        deleted = []

        class Admin:
            def __init__(self, *args):
                pass

            def import_app_dsl(self, *args, **kwargs):
                return "99999999-9999-9999-9999-999999999999"

            def app_service_key(self, *args):
                return "temporary-key"

            def delete_app(self, *args):
                deleted.append(args[-1])

        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyConsoleAdmin", Admin)
        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyDslClient", Console)
        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyChatflowClient", Workflow)
        if disabled == "delete-library":
            from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available

            libraries = [{"knowledge_base_id": "kb-own"}]
            checked = []
            monkeypatch.setattr(kb, "list_knowledge_bases", lambda: libraries, raising=False)
            Workflow.after = libraries.clear

            def check_selected(runtime, scope, binding):
                # 只改变试用版本；模型、回调、终态路径仍走真实生产装配。
                from dataclasses import replace

                checked.append(bool(libraries))
                return require_available(runtime, scope, replace(binding, callback_version=3))

            monkeypatch.setattr(
                "aiops_diagnostics.consumer.consumer_knowledge_selection.require_available", check_selected
            )
        if retract_during_trial:
            Workflow.after = lambda: env["client"].post(
                "/v1/dify/retract", headers=PUBLISH, json={"app_id": APP}
            )
        result = env["client"].post(
            "/v1/dify/trial",
            headers={"Authorization": "Bearer trial-key"},
            json={"app_id": APP, "order_no": ORDER_A, "question": "怎么开始充电？"},
        )
        assert result.status_code == 202, result.text
        trial_id = result.json()["trial_id"]
        future = runtime._futures.get(trial_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = runtime.get_trial(caller, trial_id)
            if record["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        if future is not None:
            future.result(timeout=5)
        if retract_during_trial:
            assert record["status"] == "cancelled", record
            assert record.get("result") is None
            assert deleted == ["99999999-9999-9999-9999-999999999999"]
            return
        if disabled:
            assert record["status"] == "failed", record
            read = env["client"].get(
                "/v1/dify/trial/" + trial_id, headers={"Authorization": "Bearer trial-key"}
            )
            if disabled == "delete-library":
                assert checked[-1] is False and all(checked[:-1])
                assert record.get("result") is None
                assert len(kb.calls) == 1
                return
            if disabled == "latest":
                assert read.json()["error_code"] == "TRIAL_ORDER_REQUIRED"
                assert (
                    "页面选定" in read.json()["error_message"]
                    and "最近一笔" not in read.json()["error_message"]
                )
            else:
                assert read.json()["error_code"] == "CHATFLOW_LIVE_DISABLED"
                assert "尚未开通" in read.json()["error_message"]
            assert env["upstream"].calls == 0 and not kb.calls
            return
        assert record["status"] == "completed", record
        assert record["result"]["report"]["summary"] == "试用草稿指引。"
        read = env["client"].get("/v1/dify/trial/" + trial_id, headers={"Authorization": "Bearer trial-key"})
        assert read.status_code == 200 and read.json()["result"]["report"]["summary"] == "试用草稿指引。"
        assert deleted == ["99999999-9999-9999-9999-999999999999"]
        assert routes.get("env-a", "TENANT-A", "consumer", SCENARIO_CONSUMER_SERVICE) == before
        assert done(env, ask(env).json()["qa_id"]).json()["result"]["blocks"][0]["text"] == "按设备提示启动。"
        env["client"].post("/v1/dify/retract", headers=PUBLISH, json={"app_id": APP})
        assert (
            env["client"]
            .get("/v1/dify/trial/" + trial_id, headers={"Authorization": "Bearer trial-key"})
            .status_code
            == 403
        )


def test_wf01_publishes_beside_an_existing_node_without_changing_it(publication, monkeypatch, tmp_path):
    env = publication
    old = publish_node(env, 0)
    _, _, _, routes = prepare(env, monkeypatch, tmp_path)
    row_before = routes.by_key(old.key)
    response = publish(env)
    assert response.status_code == 200, response.text
    assert routes.by_key(old.key) == row_before
    answer = done(env, ask(env).json()["qa_id"])
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"]["blocks"][0]["text"] == "按设备提示启动。"
    assert routes.by_key(old.key) == row_before


def test_old_node_still_needs_its_own_publication_target(publication, monkeypatch):
    env = publication
    old = publish_node(env, 0)
    env["settings"].chat_node_targets_file = ""
    env["settings"].dify_publish_api_key = "publication-key"
    env["settings"].dify_console_base_url = "http://dify.invalid"
    env["settings"].dify_console_api_key = "console-key"
    monkeypatch.setattr(env["context"].dify_workspaces, "resolve", lambda *args: "workspace-a")
    response = env["client"].post("/v1/dify/publish", headers=PUBLISH, json={"app_id": old.app_id})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NODE_TARGET_MISSING"


def test_same_app_cannot_silently_move_its_node_route_to_wf01(publication, monkeypatch, tmp_path):
    env = publication
    old = publish_node(env, 0)
    _, _, _, routes = prepare(env, monkeypatch, tmp_path)
    target = tmp_path / "consumer-targets.json"
    body = json.loads(target.read_text())
    body["targets"][0]["app_id"] = old.app_id
    target.write_text(json.dumps(body))
    env["context"].runtime.dify_app_registry.put(DifyAppBinding("TENANT-A", "consumer", old.app_id, "WF01"))
    before = routes.by_key(old.key)
    response = env["client"].post("/v1/dify/publish", headers=PUBLISH, json={"app_id": old.app_id})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONSUMER_ROUTE_MIGRATION_REQUIRED"
    assert routes.by_key(old.key) == before
