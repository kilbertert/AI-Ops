from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES, OPS_TOOL_NAMES
from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.dify_app_registry import DifyAppBinding
from aiops_diagnostics.dify_dsl_pull import DifyDslSource, map_dsl_to_config, resolve_pulled_config
from aiops_diagnostics.dify_tools_catalog import partition_aiops_tools
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import (
    GatewayServerSettings,
    charging_endpoint,
    parse_charging_endpoints,
)
from aiops_diagnostics.ops_bindings import load_ops_bindings, token_digest
from aiops_diagnostics.ops_provider import ensure_ops_provider, ops_openapi
from aiops_diagnostics.ops_tools import ChargingInfoInput, OpsToolError, OpsToolService, OrderListInput
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import MySQLSource

KEY = "synthetic-ops-service-key-at-least-32-characters"


@pytest.fixture
def provision(tmp_path):
    db = tmp_path / "gateway.db"
    manager = AgentManager(AgentStore(db))
    ctx = SimpleNamespace(
        effective_tenant_id="tenant-a",
        roles={"ROLE_AGENT_ADMIN"},
        subject=SimpleNamespace(b_user_id="operator"),
    )
    config = AgentConfig(
        "operations", "核对订单状态", (), "aiops-api", "diagnosis-v1", allowed_tools=OPS_TOOL_NAMES
    )
    agent = manager.create(ctx, name="ops", description="", config=config)
    manager.publish(ctx, agent.agent_id, expected_revision=agent.revision)
    path = tmp_path / "ops.json"
    rows = [
        {
            "key_sha256": token_digest(KEY),
            "environment": "test",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-a",
            "app_id": "app-a",
            "agent_id": agent.agent_id,
            "active": True,
        }
    ]
    path.write_text(json.dumps(rows))
    path.chmod(0o600)
    env = tmp_path / "config.env"
    env.write_text("# temporary\n")
    env.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=db,
        server_config_file=env,
        ops_bindings_file=str(path),
        environment="test",
        execution_environment="test",
        charging_status_base_url="tenant-a:http://127.0.0.1:9",
    )
    service = OpsToolService(settings, manager)
    service.workspaces.claim("tenant-a", "test", owner="fixture", lease_seconds=30)
    service.workspaces.mark_created("tenant-a", "test", "ws-a")
    service.registry.put(DifyAppBinding("tenant-a", "operator", "app-a", "ops"))
    return SimpleNamespace(
        settings=settings,
        manager=manager,
        ctx=ctx,
        agent_id=agent.agent_id,
        service=service,
        path=path,
        rows=rows,
    )


def write_binding(p, **changes):
    p.rows[0].update(changes)
    p.path.write_text(json.dumps(p.rows))


def publish(p, tools):
    a = p.manager.get(p.ctx, p.agent_id)
    if a.status != "draft":
        a = p.manager.fork_draft(p.ctx, a.agent_id, expected_revision=a.revision)
    a = p.manager.update(
        p.ctx,
        a.agent_id,
        expected_revision=a.revision,
        name=a.name,
        description=a.description,
        config=replace(a.config, allowed_tools=tools),
    )
    return p.manager.publish(p.ctx, a.agent_id, expected_revision=a.revision)


def test_draft_then_publish_empty_revokes_both_tools(provision):
    p = provision
    p.manager.fork_draft(p.ctx, p.agent_id, expected_revision=2)
    assert p.service.authorize(KEY, "tenant-a", "order_list")[1] == 1
    publish(p, ())
    for tool in OPS_TOOL_NAMES:
        with pytest.raises(OpsToolError, match="OPS_TOOL_NOT_PUBLISHED"):
            p.service.authorize(KEY, "tenant-a", tool)
    assert AgentConfig("operations", "x", (), "aiops-api", "diagnosis-v1").allowed_tools == ALL_TOOL_NAMES


@pytest.mark.parametrize(
    "change",
    [
        {"active": False},
        {"environment": "other"},
        {"workspace_id": "ws-b"},
        {"app_id": "app-b"},
        {"agent_id": "missing"},
    ],
)
def test_binding_denials_do_not_read_business(provision, monkeypatch, change):
    p = provision
    monkeypatch.setattr(p.service, "_read", lambda *args: pytest.fail("unauthorized read"))
    write_binding(p, **change)
    with pytest.raises(OpsToolError):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())


def test_disabled_agent_and_wrong_tenant_key_are_denied(provision, monkeypatch):
    p = provision
    monkeypatch.setattr(p.service, "_read", lambda *args: pytest.fail("unauthorized read"))
    for token, tenant in [("", "tenant-a"), ("wrong", "tenant-a"), (KEY, "tenant-b")]:
        with pytest.raises(OpsToolError):
            p.service.execute(token, tenant, "order_list", OrderListInput())
    a = p.manager.get(p.ctx, p.agent_id)
    p.manager.disable(p.ctx, p.agent_id, expected_revision=a.revision)
    with pytest.raises(OpsToolError):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())


def test_revocation_during_read_discards_result(provision, monkeypatch):
    p = provision

    def read(*args):
        write_binding(p, active=False)
        return {"source": "fixture", "orders": [{"order_no": "secret"}]}

    monkeypatch.setattr(p.service, "_read", read)
    with pytest.raises(OpsToolError):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())
    with sqlite3.connect(p.settings.database_file) as db:
        log = db.execute("SELECT evidence_json FROM ops_tool_audit").fetchone()[0]
    assert KEY not in log and "secret" not in log and '"outcome": "refused"' in log


def test_success_is_bound_to_result_and_audit_failure_hides_body(provision, monkeypatch):
    p = provision
    result = {"source": "fixture", "orders": []}
    monkeypatch.setattr(p.service, "_read", lambda *args: result)
    answer = p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())
    assert answer["result"] == result
    assert (
        answer["evidence"]["result_sha256"]
        == hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )

    def unavailable(*args):
        raise sqlite3.OperationalError("disk")

    monkeypatch.setattr(p.service, "_record", unavailable)
    with pytest.raises(OpsToolError, match="OPS_EVIDENCE_UNAVAILABLE"):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())


def test_endpoints_are_exact_and_old_scalar_is_not_ops_fallback():
    assert charging_endpoint("http://127.0.0.1:9", "tenant-a") == "http://127.0.0.1:9"
    for value, tenant in [("http://127.0.0.1:9", "tenant-a"), ("tenant-a:http://127.0.0.1:9", "b")]:
        with pytest.raises(ValueError, match="UNRESOLVED"):
            charging_endpoint(value, tenant, require_registration=True)
    for value in ["a:https://x,a:https://y", "a:http://remote.invalid", "a:https://x/?q=1", "bad"]:
        with pytest.raises(ValueError):
            parse_charging_endpoints(value)


def test_ops_endpoint_can_be_enabled_without_consumer_status(provision, monkeypatch):
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

    p = provision
    p.settings.charging_status_base_url = ""
    p.settings.execution_environment = ""
    p.settings.ops_charging_status_base_url = "tenant-a:http://127.0.0.1:9"
    p.settings.validate()
    reads = []

    def read(binding, tool, inputs, base):
        reads.append(base)
        return {"orders": [], "source": "fixture"}

    monkeypatch.setattr(p.service, "_read", read)
    assert p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())["result"]["orders"] == []
    assert reads == ["http://127.0.0.1:9"]

    subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="tenant-a")
    caller = ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset(),
    )
    app = create_gateway_app(
        settings=p.settings,
        agent_manager=p.manager,
        runtime=SimpleNamespace(kb_search_client=None),
        caller_resolver=SimpleNamespace(resolve=lambda *args, **kwargs: caller),
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/orders/O-1/charging-status",
            headers={"Authorization": "Bearer consumer-fixture", "X-Business-Entry": "consumer"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CHARGING_STATUS_DISABLED"


def test_ops_endpoint_settings_read_private_config_and_prefer_environment(provision, monkeypatch):
    p = provision
    p.settings.server_config_file.write_text(
        "AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL=tenant-a:https://cpms.invalid/charging-pile\n"
    )
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(p.settings.server_config_file))
    monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", "test")
    monkeypatch.delenv("AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL", raising=False)
    assert (
        GatewayServerSettings.from_env().ops_charging_status_base_url
        == "tenant-a:https://cpms.invalid/charging-pile"
    )
    monkeypatch.setenv("AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL", "tenant-a:https://override.invalid")
    settings = GatewayServerSettings.from_env()
    assert settings.ops_charging_status_base_url == "tenant-a:https://override.invalid"
    settings.ops_charging_status_base_url = "tenant-a:http://remote.invalid"
    with pytest.raises(ValueError):
        settings.validate()


def test_ops_endpoint_missing_tenant_does_not_fall_back_to_legacy(provision, monkeypatch):
    p = provision
    p.settings.ops_charging_status_base_url = "tenant-b:https://cpms.invalid"
    monkeypatch.setattr(p.service, "_read", lambda *args: pytest.fail("unregistered endpoint read"))
    with pytest.raises(OpsToolError, match="TENANT_ENDPOINT_UNRESOLVED"):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())


def test_private_binding_validation(provision):
    p = provision
    p.path.chmod(0o644)
    with pytest.raises(ValueError):
        load_ops_bindings(str(p.path))
    p.path.chmod(0o600)
    p.path.write_text(json.dumps(p.rows * 2))
    with pytest.raises(ValueError):
        load_ops_bindings(str(p.path))


def test_mysql_list_pushes_scope_and_bounds(provision, monkeypatch):
    from aiops_diagnostics.config import Settings

    captured = []

    class Cursor:
        def execute(self, sql, params):
            captured.append((sql, params))

        def fetchall(self):
            return []

    @contextmanager
    def cursor():
        yield Cursor()

    source = MySQLSource(Settings(), QueryScope(tenant_id="tenant-a", site_ids=("site-a",), user_id=None))
    monkeypatch.setattr(source, "_cursor", cursor)
    assert source.list_orders(page=2, page_size=5) == []
    sql, params = captured[0]
    assert "tenant_id" in sql and "site_id IN" in sql and "ORDER BY created_time DESC, id DESC" in sql
    assert params == ["tenant-a", "site-a", 6, 5]
    assert "tenant-a" not in sql
    source.scope = QueryScope(tenant_id="tenant-a", site_ids=(), user_id=None)
    assert source.list_orders(page=1, page_size=5) == [] and len(captured) == 1


def test_new_dsl_empty_and_old_provider_preservation(provision, monkeypatch):
    p = provision
    from aiops_diagnostics.dify_dsl_pull import DifyDslClient

    doc = {
        "version": "0.7.0",
        "kind": "app",
        "model_config": {
            "pre_prompt": "核对订单",
            "model": {"name": "aiops-api"},
            "agent_mode": {"tools": []},
        },
    }
    publish(p, (*ALL_TOOL_NAMES, *OPS_TOOL_NAMES))
    monkeypatch.setattr(DifyDslClient, "export_dsl", lambda self: yaml.safe_dump(doc))
    monkeypatch.setattr(DifyDslClient, "tool_provider_ids", lambda self: {"aiops": "old", "aiops-ops": "new"})
    source = DifyDslSource("http://127.0.0.1:9", "app-a", api_key="synthetic-console", ops_tools_enabled=True)
    config = resolve_pulled_config(
        p.manager, p.ctx, source, name="ops", agent_type="operations", tool_registry={}
    )
    assert config.allowed_tools == ALL_TOOL_NAMES
    doc["model_config"]["agent_mode"]["tools"] = [
        {"provider_type": "api", "provider_id": "new", "tool_name": "order_list", "enabled": True}
    ]
    config = resolve_pulled_config(
        p.manager, p.ctx, source, name="ops", agent_type="operations", tool_registry={}
    )
    assert config.allowed_tools == (*ALL_TOOL_NAMES, "order_list")
    doc["model_config"]["agent_mode"]["tools"][0]["enabled"] = False
    config = map_dsl_to_config(
        yaml.safe_dump(doc),
        agent_type="operations",
        ops_tools_enabled=True,
        tool_resolver=lambda names: partition_aiops_tools(
            names, aiops_provider_id="old", ops_provider_id="new", bindings={}
        )[0],
    )
    assert config.allowed_tools == ()


def test_provider_create_repeat_and_conflict(provision):
    binding = load_ops_bindings(str(provision.path))[0]

    class Console:
        def __init__(self):
            self.schema = None
            self.writes = 0

        def _call(self, workspace, path, **kwargs):
            assert workspace == "ws-a" and "switch" not in path
            if "tool-providers?" in path:
                return [{"name": "aiops-ops"}] if self.schema else []
            if "/get?" in path:
                return {"schema": self.schema}
            assert path.endswith("/add")
            self.writes += 1
            self.schema = kwargs["body"]["schema"]
            assert kwargs["body"]["credentials"]["api_key_value"] == KEY
            return {"result": "success"}

    console = Console()
    assert ensure_ops_provider(console, binding, KEY, "https://gateway.invalid")["action"] == "created"
    assert ensure_ops_provider(console, binding, KEY, "https://gateway.invalid")["action"] == "unchanged"
    assert console.writes == 1
    with pytest.raises(ValueError, match="schema"):
        ensure_ops_provider(console, binding, KEY, "https://other.invalid")
    assert set(ops_openapi("https://gateway.invalid", "tenant-a")["paths"]) == {
        "/v1/dify/ops/tenant-a/charging_info",
        "/v1/dify/ops/tenant-a/order_list",
    }


def test_provider_http_and_cli_use_target_workspace(provision, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app
    from aiops_diagnostics.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin

    p = provision
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body):
            assert self.headers.get("X-WORKSPACE-ID") == "ws-a"
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if "tool-providers?" in self.path:
                self.reply([{"name": "aiops-ops"}] if payloads else [])
            else:
                self.reply({"schema": payloads[0]["schema"]})

        def do_POST(self):
            assert self.path.endswith("/api/add")
            payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.reply({"result": "success"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        console = DifyConsoleAdmin(ConsoleApiSettings(base, "synthetic-console-key"))
        binding = load_ops_bindings(str(p.path))[0]
        assert ensure_ops_provider(console, binding, KEY, "https://gateway.invalid")["action"] == "created"
        assert payloads[0]["credentials"]["api_key_value"] == KEY
        monkeypatch.setattr(
            "aiops_diagnostics.admin_cli._dify_console_and_db",
            lambda ctx, db: (
                replace(p.settings, dify_console_base_url=base, dify_console_api_key="synthetic-console-key"),
                db,
            ),
        )
        token_file = p.path.parent / "token"
        token_file.write_text(KEY)
        token_file.chmod(0o600)
        answer = CliRunner().invoke(
            admin_app,
            [
                "ensure-ops-provider",
                "--tenant",
                "tenant-a",
                "--token-file",
                str(token_file),
                "--gateway-url",
                "https://gateway.invalid",
                "--db",
                str(p.settings.database_file),
            ],
        )
        assert answer.exit_code == 0, answer.output
        assert json.loads(answer.output)["action"] == "unchanged"
        assert len(payloads) == 1 and KEY not in answer.output
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_gateway_uses_real_authorization_and_rejects_extra_identity(provision, monkeypatch):
    p = provision
    calls = []
    monkeypatch.setattr(
        OpsToolService, "_read", lambda *args: calls.append(1) or {"source": "fixture", "orders": []}
    )
    app = create_gateway_app(
        settings=p.settings, runtime=SimpleNamespace(kb_search_client=None), agent_manager=p.manager
    )
    with TestClient(app) as client:
        route = "/v1/dify/ops/tenant-a/order_list"
        assert client.post(route, json={}).status_code == 401
        response = client.post(route, json={}, headers={"Authorization": f"Bearer {KEY}"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert calls == [1]
        publish(p, ())
        refused = client.post(route, json={}, headers={"Authorization": f"Bearer {KEY}"})
        assert refused.status_code == 403
        assert refused.headers["cache-control"] == "no-store"
        assert calls == [1]
        assert (
            client.post(
                route, json={"tenant_id": "b"}, headers={"Authorization": f"Bearer {KEY}"}
            ).status_code
            == 422
        )


def test_charging_reads_authorized_order_and_checks_scope_after_http(provision, monkeypatch):
    import aiops_diagnostics.ops_tools as module

    p = provision
    reads = []

    @contextmanager
    def source(settings, scope):
        assert scope == QueryScope(tenant_id="tenant-a", site_ids=None, user_id=None)
        yield SimpleNamespace(get_orders=lambda order: reads.append(order) or [{"order_no": order}])

    monkeypatch.setattr(module, "scoped_mysql_source", source)
    monkeypatch.setattr(
        module,
        "read_charging_status",
        lambda base, order, **kwargs: {
            "order_no": order,
            "status_code": 1,
            "status_role": "finished",
            "source": "cpms.chargingInfo",
            "observed_at": "now",
        },
    )
    answer = p.service.execute(KEY, "tenant-a", "charging_info", ChargingInfoInput(order_no="O-1"))
    assert answer["result"]["order_no"] == "O-1" and reads == ["O-1", "O-1"]

    @contextmanager
    def transferred(settings, scope):
        values = iter([[{"order_no": "O-1"}], []])
        yield SimpleNamespace(get_orders=lambda order: next(values))

    monkeypatch.setattr(module, "scoped_mysql_source", transferred)
    with pytest.raises(OpsToolError, match="ORDER_NOT_FOUND"):
        p.service.execute(KEY, "tenant-a", "charging_info", ChargingInfoInput(order_no="O-1"))


def test_existing_shared_key_cannot_become_ops_key(provision):
    p = provision
    p.settings.dify_tools_api_key = KEY
    with pytest.raises(OpsToolError, match="OPS_CREDENTIAL_REJECTED"):
        p.service.authorize(KEY, "tenant-a", "order_list")


def test_missing_registration_and_changed_publication_during_read(provision, monkeypatch):
    p = provision
    p.settings.charging_status_base_url = "http://127.0.0.1:9"
    monkeypatch.setattr(p.service, "_read", lambda *args: pytest.fail("unregistered read"))
    with pytest.raises(OpsToolError, match="TENANT_ENDPOINT_UNRESOLVED"):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())
    p.settings.charging_status_base_url = "tenant-a:http://127.0.0.1:9"

    def republish(*args):
        publish(p, OPS_TOOL_NAMES)
        return {"source": "fixture"}

    monkeypatch.setattr(p.service, "_read", republish)
    with pytest.raises(OpsToolError, match="OPS_AUTHORIZATION_CHANGED"):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())


def test_list_result_projects_database_types_and_caps_page(provision, monkeypatch):
    import aiops_diagnostics.ops_tools as module

    p = provision
    rows = [
        {
            "order_no": "O-1",
            "status": 1,
            "site_id": 10,
            "created_time": datetime(2026, 1, 1),
            "password": "must-not-leave",
        }
    ] * 3

    @contextmanager
    def source(settings, scope):
        yield SimpleNamespace(list_orders=lambda **kw: rows)

    monkeypatch.setattr(module, "scoped_mysql_source", source)
    answer = p.service.execute(KEY, "tenant-a", "order_list", OrderListInput(page_size=2))
    assert len(answer["result"]["orders"]) == 2 and answer["result"]["has_more"]
    assert answer["result"]["orders"][0]["site_id"] == "10"
    assert "must-not-leave" not in json.dumps(answer)


@pytest.mark.parametrize("error", [ValueError("configuration secret"), OSError("path secret")])
def test_errors_have_stable_safe_codes(provision, monkeypatch, error):
    p = provision

    def fail(*args):
        raise error

    monkeypatch.setattr(p.service, "_read", fail)
    with pytest.raises(OpsToolError, match="OPS_CONFIGURATION_UNAVAILABLE") as info:
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())
    assert "secret" not in str(info.value)


def test_source_error_is_not_success(provision, monkeypatch):
    from aiops_diagnostics.sources import SourceError

    p = provision

    def fail(*args):
        raise SourceError("private database host")

    monkeypatch.setattr(p.service, "_read", fail)
    with pytest.raises(OpsToolError, match="OPS_SOURCE_UNAVAILABLE"):
        p.service.execute(KEY, "tenant-a", "order_list", OrderListInput())
