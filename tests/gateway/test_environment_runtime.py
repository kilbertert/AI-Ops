"""D30：运行工件的复合归属、私有输出及原候选应用拒绝边界。"""

import copy
import hashlib
import json
import tarfile
from pathlib import Path

import pytest
from dify.test_dify_ensure_workspace import upstream as workspace_upstream  # noqa: F401
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.dify import environment_runtime as runtime
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.operator import operator_agent, operator_tools
from gateway.test_candidate_knowledge_connection import connected  # noqa: F401
from gateway.test_environment_consumer import consumer_app, execute, submit  # noqa: F401
from gateway.test_environment_knowledge import knowledge  # noqa: F401
from gateway.test_environment_tasks import prepared  # noqa: F401


@pytest.fixture
def wiring(consumer_app, tmp_path, monkeypatch):  # noqa: F811
    state = consumer_app
    template = state[7]
    template.update(plugin="fixture/model", completion_params={"max_tokens": 4096, "temperature": 0.3})
    monkeypatch.setattr(runtime.environment_operator, "model", lambda _: template)
    response = submit(state)
    assert response.status_code == 200, response.text
    result = execute(state, response.json()["id"])
    assert result["state"] == "observed"
    fixture = state[0][0]
    path, references, _, settings = fixture
    settings.environment_registry_file = ""
    with gateway_db.connection(drafts._database(path), write=True) as db:
        row = dict(db.execute("SELECT * FROM environment_tasks WHERE id=?", (result["id"],)).fetchone())
        request = json.loads(row["request"])
        operator = copy.deepcopy(request)
        operator["action"] = "operator-prepare"
        operator["binding"] = runtime.environment_operator.binding(settings)
        operator["resources"] = {
            "provider_id": "provider-fixture",
            "expected_app_id": "operator-app",
            "name": "operator-fixture",
            "attempted": True,
        }
        operator_result = {**result["result"], "app_id": "operator-app"}
        operator_task = "task_" + "1" * 32
        db.execute(
            "INSERT INTO environment_tasks(id,environment,request,state,result,updated_at) "
            "VALUES(?,?,?,'observed',?,?)",
            (operator_task, "a", json.dumps(operator), json.dumps(operator_result), row["updated_at"]),
        )
    payload = {
        "identity": ["control", "platform", "admin"],
        "plan_id": request["plan"]["id"],
        "tenant_id": "same",
        "operator_task": operator_task,
        "consumer_task": result["id"],
    }
    original_console = runtime.environment_operator.DifyConsoleAdmin
    operator_app = {
        "id": "operator-app",
        "name": "operator-fixture",
        "mode": "agent-chat",
        "enable_api": False,
        "enable_site": False,
        "model_config": {
            "pre_prompt": operator_agent.sop(),
            "model": {
                "name": template["name"],
                "provider": template["provider"],
                "completion_params": template["completion_params"],
            },
            "agent_mode": {
                "enabled": True,
                "strategy": "function_call",
                "max_iteration": 10,
                "tools": [
                    {
                        "provider_id": "provider-fixture",
                        "provider_type": "api",
                        "enabled": True,
                        "tool_name": t.name,
                    }
                    for t in operator_tools.tools()
                ],
            },
        },
    }

    class Console(original_console):
        def _call(self, ws, route, method="GET", **kwargs):
            assert method == "GET"
            if route == "/console/api/apps/operator-app":
                return operator_app
            if route == "/console/api/apps/operator-app/api-keys":
                return {"data": []}
            return super()._call(ws, route, method=method, **kwargs)

    monkeypatch.setattr(runtime, "DifyConsoleAdmin", Console)
    monkeypatch.setattr(runtime, "ensure_node_provider", lambda *a, **kw: None)
    model_config = write_private_text(
        tmp_path / "manifest.json",
        json.dumps(
            {
                "schema": "m3-exec-config/1",
                "name": "fixture",
                "history_policy": "none",
                "model": {
                    "provider_key": "openai",
                    "adapter_key": "openai-compatible",
                    "model_key": "old",
                    "endpoint": "https://model-proxy:9443/v1",
                },
                "model_options": {},
                "system_messages": ["old"],
                "tools": [{"name": "charging_status_snapshot", "description": "old", "input_schema": "{}"}],
            }
        ),
    )
    config = write_private_text(
        tmp_path / "agent-runs.json",
        json.dumps(
            {
                "environment": "a",
                "snail_base_url": "https://execution.example.test",
                "snail_app_id": "environment-a",
                "snail_app_token": "synthetic-node-secret",
                "snail_agent_id": 7,
                "snail_open_id": "fixture",
                "expected_node_id": "node-a",
                "nodes": [
                    {
                        "node_id": "node-a",
                        "environment": "a",
                        "token_sha256": hashlib.sha256(b"node-a").hexdigest(),
                        "active": True,
                    }
                ],
            }
        ),
    )
    node = write_private_text(
        tmp_path / "node.json",
        json.dumps(
            {
                "environment": "a",
                "config_file": str(config),
                "manifest_file": str(model_config),
                "proxy_base_url": "https://gateway-egress:9443",
            }
        ),
    )
    output_dir = tmp_path / "private-artifacts"
    output_dir.mkdir(mode=0o700)
    return settings, payload, node, output_dir / "runtime.tar", fixture, operator_app, state


def test_export_keeps_target_environment_and_secrets_out_of_receipt(wiring):
    settings, payload, node, output, *_ = wiring
    before = Path(settings.database_file).read_bytes()
    result = runtime.export(settings, payload, node, output)
    assert result["scope"] == "runtime-wiring-only" and result["installed"] is False
    assert result["activated"] is False and result["executable"] is False
    assert "synthetic-node-secret" not in json.dumps(result)
    assert Path(settings.database_file).read_bytes() == before
    with tarfile.open(output) as bundle:
        config = json.load(bundle.extractfile("agent-runs.json"))
        manifest = json.load(bundle.extractfile("operator-manifest.json"))
        registrations = json.load(bundle.extractfile("registrations.json"))
        assert config["environment"] == "a" and config["expected_node_id"] == "node-a"
        assert manifest["schema"] == operator_agent.SCHEMA
        assert manifest["model"]["model_key"] == wiring[6][7]["name"]
        assert {t["name"] for t in manifest["tools"]} == set(operator_tools.names())
        assert registrations["enabled"] is False and registrations["tenant_id"] == "same"
        assert all(m.mode == 0o600 for m in bundle.getmembers())
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        runtime.export(settings, payload, node, output)


@pytest.mark.parametrize(
    "change", ["node-environment", "tenant", "cancel", "app-content", "enabled", "output-permission"]
)
def test_invalid_wiring_never_leaves_half_artifact(wiring, change):
    settings, payload, node, output, fixture, app, _ = wiring
    if change == "node-environment":
        body = json.loads(node.read_text())
        body["environment"] = "b"
        node.write_text(json.dumps(body))
    elif change == "tenant":
        payload["tenant_id"] = "other"
    elif change == "cancel":
        with gateway_db.connection(drafts._database(fixture[0]), write=True) as db:
            db.execute("UPDATE environment_tasks SET cancel=1 WHERE id=?", (payload["operator_task"],))
    elif change == "app-content":
        app["model_config"]["pre_prompt"] += "外部修改"
    elif change == "enabled":
        app["enable_api"] = True
    else:
        output.parent.chmod(0o755)
    with pytest.raises((ValueError, RuntimeError)):
        runtime.export(settings, payload, node, output)
    assert not output.exists() and list(output.parent.iterdir()) == []


def test_admin_runtime_entry_uses_same_export_without_secret_output(wiring, monkeypatch):
    settings, payload, node, output, *_ = wiring
    monkeypatch.setattr("aiops_diagnostics.admin_cli._settings_with_config_file", lambda _: settings)
    request = write_private_text(output.parent / "request.json", json.dumps(payload))
    result = CliRunner().invoke(
        admin_app,
        ["environment-runtime", "--file", str(request), "--node", str(node), "--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["environment"] == "a" and "synthetic-node-secret" not in result.output


def test_missing_key_list_cannot_prove_candidate_has_zero_keys(wiring, monkeypatch):
    settings, payload, node, output, *_ = wiring
    original = runtime.DifyConsoleAdmin._call

    def missing(self, ws, path, **kwargs):
        if path.endswith("/api-keys"):
            return {}
        return original(self, ws, path, **kwargs)

    monkeypatch.setattr(runtime.DifyConsoleAdmin, "_call", missing)
    with pytest.raises(ValueError, match="active entries"):
        runtime.export(settings, payload, node, output)
    assert not output.exists()


@pytest.mark.parametrize("change", ["expired", "other-plan"])
def test_task_observation_must_be_fresh_and_from_the_selected_plan(wiring, change):
    settings, payload, node, output, fixture, *_ = wiring
    with gateway_db.connection(drafts._database(fixture[0]), write=True) as db:
        row = db.execute(
            "SELECT request,result FROM environment_tasks WHERE id=?", (payload["operator_task"],)
        ).fetchone()
        request, result = json.loads(row[0]), json.loads(row[1])
        if change == "expired":
            result["expires_at"] = 0
        else:
            request["plan"]["id"] = "plan_" + "f" * 32
        db.execute(
            "UPDATE environment_tasks SET request=?,result=? WHERE id=?",
            (json.dumps(request), json.dumps(result), payload["operator_task"]),
        )
    with pytest.raises(ValueError, match="observation required"):
        runtime.export(settings, payload, node, output)
    assert not output.exists()
