"""节点发布使用真实版本/路由，保存与试用不改变正式指针。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslSource
from aiops_diagnostics.execution import chat_execution, node_publication
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from execution.test_app_runtime_dify_entry import _Resolver, dify_chat
from execution.test_app_runtime_node_entry import ORDER_A
from execution.test_m2_close_contract import _write_config
from execution.test_m3_gateway_exec_config import _write_manifest

APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def publication(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as f:
        settings = f["settings"]
        settings.chat_execution_bindings_file = ""
        settings.chat_route_bindings_file = str(f["db"])
        config = _write_config(tmp_path)
        body = json.loads(config.read_text())
        body["environment"] = "env-a"
        for node in body["nodes"]:
            node["environment"] = "env-a"
        config.write_text(json.dumps(body))
        manifest = _write_manifest(tmp_path)
        target = node_publication.NodeTarget(APP, "TENANT-A", "env-a", str(config), str(manifest))
        targets = tmp_path / "targets.json"
        targets.write_text(json.dumps({"schema": "aiops-node-targets/1", "targets": [target.__dict__]}))
        targets.chmod(0o600)
        settings.chat_node_targets_file = str(targets)
        context = f["app"].state.gateway
        manager = AgentManager(AgentStore(f["db"]))
        admin = SimpleNamespace(effective_tenant_id="TENANT-A", roles={"ROLE_PLATFORM_ADMIN"})
        agent = manager.create(
            admin,
            name="node-sop",
            description="test",
            config=AgentConfig(
                agent_type="customer",
                prompt="baseline",
                knowledge_base_ids=(),
                model="aiops-api",
                output_contract="blocks-v1",
            ),
        )
        manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
        binding = DifyAppBinding("TENANT-A", "consumer", APP, "node-sop")
        source = DifyDslSource("http://127.0.0.1:1", APP, "synthetic", "workspace")
        candidate = {"prompt": "VERSION_ONE"}
        monkeypatch.setattr(
            node_publication,
            "DifyDslClient",
            lambda _: SimpleNamespace(
                export_dsl=lambda: json.dumps(
                    {"app": {"mode": "chat"}, "model_config": {"pre_prompt": candidate["prompt"]}}
                )
            ),
        )
        context.agent_manager = manager
        context.runtime.dify_app_registry.put(binding)
        yield {
            **f,
            "target": target,
            "binding": binding,
            "source": source,
            "manager": manager,
            "admin": admin,
            "candidate": candidate,
            "context": context,
        }


def publish(f, revision):
    outcome = node_publication.publish(f["manager"], f["admin"], f["binding"], f["source"], f["target"])
    node_publication.advance(f["context"], outcome, f["binding"], f["target"], revision)
    return chat_execution.select_binding(f["settings"], _Resolver().resolve("token-a", required_scope="x"))


def test_node_publication_uses_frozen_sop_and_generation(publication):
    f = publication
    first = publish(f, 0)
    assert "VERSION_ONE" in first.manifest_text
    f["candidate"]["prompt"] = "VERSION_TWO"
    current = chat_execution.select_binding(f["settings"], _Resolver().resolve("token-a", required_scope="x"))
    assert current == first
    second = publish(f, first.route_revision)
    assert "VERSION_TWO" in second.manifest_text and second.route_revision > first.route_revision
    unchanged = publish(f, second.route_revision)
    assert unchanged == second


def test_published_run_can_fetch_only_its_own_config(publication):
    f = publication
    binding = publish(f, 0)
    scope = _Resolver().resolve("token-a", required_scope="x")
    store = f["context"].agent_run_store
    run, config = chat_execution.issue_for_store(
        store=store,
        settings=f["settings"],
        binding=binding,
        caller=scope,
        order_no=ORDER_A,
        gateway_version="test",
    )
    store.bind_qa(run.run_id, "qa-synthetic")
    from execution.test_m2_close_contract import NODE_A_TOKEN

    path = "/v1/internal/charging-status/execution-config/" + run.run_id
    assert f["client"].post(path, json={}).status_code == 403
    ok = f["client"].post(path, json={}, headers={"Authorization": "Bearer " + NODE_A_TOKEN})
    assert ok.status_code == 200, ok.text
    assert ok.json()["manifest"] == binding.manifest_text
    publish(f, binding.route_revision)  # same content is a no-op
    assert chat_execution.callback_config(store, run.run_id, f["settings"]) is not None
    f["candidate"]["prompt"] = "VERSION_TWO"
    publish(f, binding.route_revision)
    assert (
        f["client"].post(path, json={}, headers={"Authorization": "Bearer " + NODE_A_TOKEN}).status_code
        == 403
    )


@pytest.mark.parametrize(
    "fault", ["wrong_node", "owner_changed", "expired", "revoked", "node_revoked_during_auth"]
)
def test_execution_config_is_refused_before_model_or_business(publication, fault, monkeypatch):
    from execution.test_m2_close_contract import NODE_A2_TOKEN, NODE_A_TOKEN

    f = publication
    binding = publish(f, 0)
    scope = _Resolver().resolve("token-a", required_scope="x")
    store = f["context"].agent_run_store
    run, _ = chat_execution.issue_for_store(
        store=store,
        settings=f["settings"],
        binding=binding,
        caller=scope,
        order_no=ORDER_A,
        gateway_version="test",
    )
    store.bind_qa(run.run_id, "qa-synthetic")
    token = NODE_A_TOKEN
    if fault == "wrong_node":
        token = NODE_A2_TOKEN
    elif fault == "owner_changed":
        f["orders"].ownership[ORDER_A] = "different-owner"
    elif fault == "expired":
        with store._connection(write=True) as connection:
            connection.execute(
                "UPDATE agent_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE run_id=?", (run.run_id,)
            )
    elif fault == "revoked":
        store.revoke(run.run_id)
    else:
        original = f["orders"].can_access

        def revoke_node(*args):
            result = original(*args)
            config = json.loads(Path(binding.config_file).read_text())
            config["nodes"][0]["active"] = False
            Path(binding.config_file).write_text(json.dumps(config))
            return result

        monkeypatch.setattr(f["orders"], "can_access", revoke_node)
    result = f["client"].post(
        "/v1/internal/charging-status/execution-config/" + run.run_id,
        json={},
        headers={"Authorization": "Bearer " + token},
    )
    assert result.status_code in {403, 404}, result.text
    assert "manifest" not in result.json()
    assert f["upstream"]["calls"] == 0


def test_late_publication_cannot_overwrite_retraction(publication):
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    f = publication
    first = publish(f, 0)
    f["candidate"]["prompt"] = "new draft"
    outcome = node_publication.publish(f["manager"], f["admin"], f["binding"], f["source"], f["target"])
    routes = ChatRouteBindingStore(f["db"])
    routes.retract(
        environment=first.environment, tenant_id=first.tenant_id, entry=first.entry, scenario=first.scenario
    )
    with pytest.raises(node_publication.NodeRouteConflict):
        node_publication.advance(f["context"], outcome, f["binding"], f["target"], first.route_revision)
    assert not routes.by_key(first.key).active
