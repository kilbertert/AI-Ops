"""旧运行时不可达，当前业务与试用不回退 Python。"""

import ast
import importlib.util
import sqlite3
import tomllib
from pathlib import Path

import pytest
from consumer.test_consumer_chat_service import Client, configure, done
from consumer.test_consumer_live_context import wire
from consumer.test_consumer_semantic_routing import semantic
from dify.test_dify_trial import ORDER, TRIAL_KEY, _client, _post
from execution.test_app_runtime_dify_entry import HEADERS, ORDER_A, dify_chat

RETIRED_MODULES = frozenset(
    [
        "__main__",
        "agent_contracts",
        "agent_engine",
        "agent_runner",
        "agent_validator",
        "agent_workspace",
        "cli",
        "codex_launcher",
        "codex_runtime",
        "conversation_context",
        "diagnostic_tools",
        "engine",
        "gateway_cli",
        "gateway_client",
        "gateway_tokens",
        "jev_decisions",
        "journal",
        "models",
        "ops_agents",
        "ops_bindings",
        "ops_tools",
        "parsing",
        "promo_agents",
        "render",
        "responses_adapter",
        "routing",
        "turn_recovery",
    ]
)


@pytest.mark.parametrize("module", sorted(RETIRED_MODULES))
def test_retired_modules_are_not_installed(module):
    assert importlib.util.find_spec("aiops_diagnostics." + module) is None


def test_current_code_does_not_import_retired_modules():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for folder in ("src", "tools", "tests"):
        for path in (root / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                    if node.module == "aiops_diagnostics":
                        names = [f"aiops_diagnostics.{alias.name}" for alias in node.names]
                else:
                    continue
                for name in names:
                    if name.startswith("aiops_diagnostics.") and name.split(".")[1] in RETIRED_MODULES:
                        offenders.append(f"{path.relative_to(root)}:{node.lineno}: {name}")
    assert not offenders, "\n".join(offenders)


def test_distribution_exposes_only_current_operations_and_services():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert "aiops" not in project["scripts"]
    assert project["scripts"]["aiops-admin"] == "aiops_diagnostics.admin_cli:admin_app"
    assert not any("codex" in dependency or "keyring" in dependency for dependency in project["dependencies"])


def test_independent_admin_entry_uses_selected_private_config(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app

    config = tmp_path / "server.env"
    config.write_text("AIOPS_AGENT_MODEL=selected-model\n")
    config.chmod(0o600)
    manifest = tmp_path / "environment.toml"
    manifest.write_text("agents = []\n")
    observed = []

    def models(settings):
        observed.append(settings.agent.model)
        return ("aiops-api",)

    monkeypatch.delenv("AIOPS_AGENT_MODEL", raising=False)
    monkeypatch.setattr("aiops_diagnostics.publication.agent_lifecycle.allowed_models_from_settings", models)
    result = CliRunner().invoke(
        admin_app,
        [
            "--config",
            str(config),
            "reconcile",
            str(manifest),
            "--db",
            str(tmp_path / "gateway.db"),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert observed == ["selected-model"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/enroll"),
        ("get", "/v1/runs"),
        ("post", "/v1/runs"),
        ("get", "/v1/runs/old/events"),
        ("get", "/v1/runs/old/evidence"),
        ("post", "/v1/standard/diagnoses"),
        ("get", "/v1/standard/diagnoses/old"),
        ("post", "/v1/standard/diagnoses/old/cancel"),
        ("post", "/v1/agents/old/debug-run"),
        ("post", "/v1/dify/tools/order_snapshot"),
        ("post", "/v1/dify/ops/tenant/order_list"),
    ],
)
def test_retired_routes_are_absent(tmp_path, method, path):
    client, _ = _client(tmp_path)
    assert client.request(method, path, json={}).status_code == 404


def test_consumer_order_shortcut_uses_its_workflow(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, "order_status", "explicit")
        response = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={
                "question": "帮我检测这个订单的充电异常",
                "order_no": ORDER_A,
                "shortcut_code": "smart_diagnosis",
            },
        )
        assert response.status_code == 202, response.text
        result = done(env, response.json()["qa_id"]).json()
        assert result["status"] == "completed", result
        assert Client.calls and env["upstream"].calls == 1
        with sqlite3.connect(env["db"]) as connection:
            assert (
                connection.execute("SELECT count(*) FROM standard_diagnoses WHERE kind='order'").fetchone()[0]
                == 0
            )


def test_unconfigured_business_route_does_not_create_a_python_job(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        env["settings"].consumer_chatflow_enabled = False
        response = env["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "怎么充电？"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHAT_ROUTE_UNAVAILABLE"
        assert not Client.calls


@pytest.mark.parametrize(
    "extra", [{"prompt": "旧草稿"}, {"allowed_tools": ["order_snapshot"]}, {"tenant_id": "other"}]
)
def test_trial_rejects_python_drafts_and_caller_scope(tmp_path, extra):
    client, runtime = _client(tmp_path)
    assert _post(client, **extra).status_code == 422
    assert runtime.started == []


def test_trial_authentication_and_binding_are_required(tmp_path):
    client, runtime = _client(tmp_path)
    body = {"order_no": ORDER, "app_id": "11111111-2222-3333-4444-555555555555"}
    assert client.post("/v1/dify/trial", json=body).status_code == 401
    assert (
        client.post("/v1/dify/trial", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 403
    )
    assert _post(client).status_code in (404, 409)
    assert runtime.started == []
    client, _ = _client(tmp_path, dify_trial_api_key="", dify_trial_debug_tenant="")
    assert (
        client.post("/v1/dify/trial", json=body, headers={"Authorization": "Bearer " + TRIAL_KEY}).status_code
        == 404
    )


def test_opening_old_database_preserves_retired_data(tmp_path):
    from aiops_diagnostics.gateway_store import GatewayStore

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE runs (run_id TEXT, result_json TEXT)")
        connection.execute("INSERT INTO runs VALUES ('old', 'evidence')")
    GatewayStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM runs").fetchall() == [("old", "evidence")]
    fresh = tmp_path / "fresh.db"
    GatewayStore(fresh)
    with sqlite3.connect(fresh) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables.intersection({"runs", "devices", "enrollment_codes", "events"})
