import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest
from operations.test_operator_cpms import APP
from operations.test_operator_cpms import operator as operator

from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify import self_service_prepare as prepare
from aiops_diagnostics.dify.self_service import SelfService
from aiops_diagnostics.dify.self_service_store import SelfServiceStore, SetupConflict

REAL_DEPENDENCIES = prepare.dependencies
REAL_KNOWLEDGE = prepare.knowledge


@pytest.fixture
def managed(operator, monkeypatch, tmp_path):
    f = operator
    context = f["app"].state.gateway
    settings = f["settings"]
    settings.environment = "verify"
    target = f["target"]
    secret = tmp_path / "model.json"
    secret.write_text(
        json.dumps(
            {
                "api_key": "fixture",
                "endpoint_url": "https://model.example/v1",
                "max_tokens_to_sample": "4096",
                "function_calling_type": "tool_call",
                "stream_function_calling": "supported",
            }
        )
    )
    secret.chmod(0o600)
    profile = {
        "id": "default",
        "name": "标准管家",
        "target": {k: v for k, v in asdict(target).items() if k not in {"app_id", "tenant_id"}},
        "model": {
            "plugin": "model@fixture",
            "provider": "model/provider",
            "name": "aiops-api",
            "credential_file": str(secret),
            "completion_params": {"max_tokens": 4096},
        },
    }
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"schema": "operator-profiles/1", "profiles": [profile]}))
    path.chmod(0o600)
    settings.operator_profiles_file = str(path)
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    ws = DifyWorkspaces(settings.database_file)
    ws.claim("TENANT-A", settings.environment, owner="fixture", lease_seconds=60)
    ws.mark_created("TENANT-A", settings.environment, "workspace-a")
    service = SelfService(context)
    caller = auth.ManagerIdentity("admin", "platform", True, "private")
    monkeypatch.setattr(auth, "identity", lambda *_: caller)
    monkeypatch.setattr(
        auth, "tenants", lambda *_: [{"id": "TENANT-A", "name": "客户A"}, {"id": "TENANT-B", "name": "客户B"}]
    )

    def workspace(settings, tenant, *_):
        ws.claim(tenant, settings.environment, owner="fixture", lease_seconds=60)
        ws.mark_created(tenant, settings.environment, "workspace-b")
        return "workspace-b"

    monkeypatch.setattr(prepare, "knowledge", lambda *_: None)
    monkeypatch.setattr(prepare, "knowledge_connection", lambda *_: "workspace-api")
    monkeypatch.setattr(prepare, "workspace", workspace)
    monkeypatch.setattr(prepare, "dependencies", lambda *_: "provider-id")
    # 复制的源版本由operator夹具发布；合成Dify草稿必须使用同一份已选工具集合。
    mc = {
        "pre_prompt": f["candidate"]["prompt"],
        "model": {"name": "aiops-api"},
        "agent_mode": {
            "tools": [
                {"provider_id": "provider-id", "provider_type": "api", "tool_name": name, "enabled": True}
                for name in f["candidate"]["enabled"]
            ]
        },
    }
    service.console = SimpleNamespace(_call=lambda *_: {"model_config": mc})
    calls = []

    def create(*args, **kwargs):
        calls.append(args[2])
        return str(uuid5(UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb"), args[1] + ":" + args[2]))

    monkeypatch.setattr(prepare, "create_app", create)
    # Fixture的真实发布器读取一个合成Dify客户端，不绕开candidate验证。
    from aiops_diagnostics.execution import node_publication

    monkeypatch.setattr(
        node_publication,
        "DifyDslClient",
        lambda _: SimpleNamespace(
            export_dsl=lambda: json.dumps({"app": {"mode": "agent-chat"}, "model_config": mc}),
            tool_provider_ids=lambda: {"cpms-node": "provider-id"},
        ),
    )
    from execution.test_node_capabilities import record_ready

    record_ready(settings.database_file, f["target"].config_file)

    return f, service, caller, mc, calls


def test_job_lease_and_changed_request_are_rejected(tmp_path):
    store = SelfServiceStore(tmp_path / "db")
    body = {"tenant_id": "B", "tenant_name": "B", "name": "管家"}
    row = store.create("u", "same", body)
    with pytest.raises(SetupConflict):
        store.create("u", "same", {**body, "name": "不同"})
    assert store.claim(row["id"], "one")
    assert not store.claim(row["id"], "two")
    with pytest.raises(SetupConflict):
        store.save(row["id"], "two", step="publish", resources={})


def test_company_identity_requires_both_platform_permissions(monkeypatch):
    context = SimpleNamespace(
        operator_cpms=SimpleNamespace(
            console_identity=lambda _: {"token": "private", "user_id": "U", "tenant_id": "-1"}
        )
    )
    info = {"sysUser": {"id": "U", "tenantId": "-1", "type": "-1"}, "permissions": ["aiops:agent:edit"]}
    monkeypatch.setattr(auth, "company_get", lambda *_: info)
    with pytest.raises(auth.SetupRefused, match="PLATFORM_PERMISSION"):
        auth.identity(context, "normal-session")
    info["permissions"].append("aiops:agent:publish")
    assert auth.identity(context, "normal-session").platform
    info["sysUser"]["id"] = "other"
    with pytest.raises(auth.SetupRefused, match="IDENTITY_MISMATCH"):
        auth.identity(context, "normal-session")


def test_tenant_directory_is_not_a_tenant_user_privilege():
    with pytest.raises(auth.SetupRefused, match="PLATFORM_PERMISSION"):
        auth.tenants(None, auth.ManagerIdentity("U", "TENANT-A", False, "private"))


def test_import_retries_use_same_tenant_scoped_identity():
    class Console:
        calls = []

        def _call(self, workspace, path, **kwargs):
            if kwargs.get("method") != "POST":
                return {"aiops_idempotent_import": 1}
            self.calls.append(kwargs["body"])
            identity = str(
                uuid5(
                    UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb"),
                    workspace + ":" + kwargs["body"]["request_id"],
                )
            )
            if len(self.calls) == 1:
                raise TimeoutError("response lost after creation")
            return {"status": "completed", "app_id": identity}

    console = Console()
    profile = {"model": {"name": "M", "provider": "P", "completion_params": {"max_tokens": 4096}}}
    body = {"name": "N", "prompt": "SOP_ONE", "tools": ["cpms_order_detail"]}
    job = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(TimeoutError):
        prepare.create_app(console, "WS", job, body, profile, "PROVIDER")
    result = prepare.create_app(console, "WS", job, body, profile, "PROVIDER")
    assert result and console.calls[0] == console.calls[1]


def test_publish_conflict_does_not_change_pointer(managed):
    _, service, caller, _, _ = managed
    before = service.route("TENANT-A")
    with pytest.raises(SetupConflict, match="DRAFT_CHANGED"):
        service.publish(caller, APP, before.revision, expected_draft="0" * 64)
    assert service.route("TENANT-A") == before


def test_restart_exposes_retry_without_losing_resources(tmp_path):
    store = SelfServiceStore(tmp_path / "db")
    job = store.create("user", "request", {"tenant_id": "T", "tenant_name": "T", "name": "N"})
    store.claim(job["id"], "worker")
    store.save(job["id"], "worker", step="application", resources={"workspace_id": "WS"})
    store.recover()
    row = store.get(job["id"])
    assert row["state"] == "failed" and json.loads(row["resources_json"])["workspace_id"] == "WS"
    assert store.claim(job["id"], "new-worker")


def test_lost_lease_prevents_old_worker_writes(tmp_path):
    store = SelfServiceStore(tmp_path / "db")
    job = store.create("user", "key", {"tenant_id": "T"})
    assert store.claim(job["id"], "old")
    with store.connection() as db:
        db.execute("UPDATE operator_setup_jobs SET lease_until=0 WHERE id=?", (job["id"],))
    assert store.claim(job["id"], "new")
    with pytest.raises(SetupConflict, match="SETUP_LEASE_LOST"):
        store.save(job["id"], "old", step="complete", resources={})
    assert store.get(job["id"])["lease_owner"] == "new"


def test_dependencies_activate_model_before_marking_ready(managed, monkeypatch):
    from aiops_diagnostics.dify.self_service_config import profiles

    f, _, _, _, _ = managed
    profile = profiles(f["settings"])["default"]
    from pathlib import Path

    values = json.loads(Path(profile["model"]["credential_file"]).read_text())
    saved, active, switches = [], [], []

    def call(workspace, path, method="GET", body=None):
        assert workspace == "workspace"
        if "tool-providers" in path:
            return [{"name": "cpms-node", "id": "provider"}]
        if path.endswith("/switch"):
            switches.append(body)
            active.append(body["credential_id"])
            return {}
        if method == "POST":
            saved.append({"credential_name": body["name"], "credential_id": "credential"})
            return {}
        return {
            "available_credentials": saved,
            "current_credential_id": next(iter(active), None),
            "credentials": values if active else {},
            "load_balancing": {"enabled": False},
        }

    console = SimpleNamespace(_call=call, install_plugins=lambda *_: None)
    monkeypatch.setattr(prepare, "ensure_node_provider", lambda *_, **kw: None)
    original = REAL_DEPENDENCIES
    assert original(console, "workspace", profile, "https://gateway.example") == "provider"
    assert original(console, "workspace", profile, "https://gateway.example") == "provider"
    assert len(saved) == len(switches) == 1


def test_workspace_access_requires_own_tenant_or_platform_directory(managed):
    f, service, caller, _, _ = managed
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    ws = DifyWorkspaces(f["settings"].database_file)
    ws.claim("TENANT-B", "verify", owner="test", lease_seconds=60)
    ws.mark_created("TENANT-B", "verify", "workspace-b")
    assert service.workspace(caller, "workspace-b") == "TENANT-B"
    tenant = auth.ManagerIdentity("a", "TENANT-A", False, "token")
    assert service.workspace(tenant, "workspace-a") == "TENANT-A"
    with pytest.raises(auth.SetupRefused):
        service.workspace(tenant, "workspace-b")


def test_platform_member_join_rejects_another_dify_account(managed, monkeypatch):
    f, _, caller, _, _ = managed
    from aiops_diagnostics.common import bounded_http
    from aiops_diagnostics.dify import dify_tenant_access

    f["settings"].dify_publish_api_key = "management-key"
    monkeypatch.setattr(f["ctx"].self_service, "workspace", lambda *_: "TENANT-B")
    monkeypatch.setattr(
        bounded_http,
        "request_json",
        lambda *_, **__: {
            "company_user_id": caller.user_id,
            "dify_account_id": "aaaaaaaa-bbbb-cccc-dddd-aaaaaaaaaaaa",
        },
    )
    joined = []
    monkeypatch.setattr(dify_tenant_access, "grant_tenant_access", lambda **kw: joined.append(kw))
    payload = {
        "workspace_id": APP,
        "account_id": "aaaaaaaa-bbbb-cccc-dddd-bbbbbbbbbbbb",
        "email": "fixture@example.invalid",
    }
    headers = {"Authorization": "Bearer management-key", "X-AIOps-Console-Session": "normal-session"}
    result = f["client"].post("/v1/dify/management/workspace/member", json=payload, headers=headers)
    assert result.status_code == 403 and not joined
    payload["account_id"] = "aaaaaaaa-bbbb-cccc-dddd-aaaaaaaaaaaa"
    assert (
        f["client"].post("/v1/dify/management/workspace/member", json=payload, headers=headers).status_code
        == 200
    )
    assert joined[0]["tenant_id"] == "TENANT-B" and joined[0]["account_id"] == payload["account_id"]


def test_platform_member_join_fails_closed_when_login_proxy_errors(managed, monkeypatch):
    """登录代理连接失败或返回非 JSON / 5xx 时，member 端点应通过真实 HTTP 客户端 fail-closed 返回 403。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from aiops_diagnostics.dify import dify_tenant_access

    f, _, _, _, _ = managed
    f["settings"].dify_publish_api_key = "management-key"
    monkeypatch.setattr(f["ctx"].self_service, "workspace", lambda *_: "TENANT-B")
    joined = []
    monkeypatch.setattr(dify_tenant_access, "grant_tenant_access", lambda **kw: joined.append(kw))

    class _BrokenProxy(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = b"not-json"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _BrokenProxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        f["settings"].operator_trial_login_url = f"http://127.0.0.1:{server.server_port}"
        payload = {
            "workspace_id": APP,
            "account_id": "aaaaaaaa-bbbb-cccc-dddd-aaaaaaaaaaaa",
            "email": "fixture@example.invalid",
        }
        headers = {"Authorization": "Bearer management-key", "X-AIOps-Console-Session": "normal-session"}
        result = f["client"].post("/v1/dify/management/workspace/member", json=payload, headers=headers)
        assert result.status_code == 403
        assert result.json()["error"]["code"] == "SETUP_IDENTITY_MISMATCH"
        assert not joined
    finally:
        server.shutdown()
        server.server_close()


def test_knowledge_step_creates_verified_tenant_with_persistent_credential(managed, monkeypatch):
    from aiops_diagnostics.knowledge import kb_onboarding

    f, service, _, _, _ = managed
    settings = f["settings"]
    settings.kb_service_base_url = "https://kb.example"
    settings.ragflow_credential_key = "x" * 32
    settings.ragflow_embedding_base_url = "https://model.example/v1"
    settings.ragflow_embedding_api_key = "synthetic"
    seen = []
    client = SimpleNamespace(
        register_tenant=lambda **kw: seen.append(kw),
        repair_models=lambda _: {},
        verify_login=lambda **_: None,
    )

    def make_client(_, *, environment):
        assert environment == ""
        return client

    monkeypatch.setattr(kb_onboarding, "KbAdminClient", make_client)
    monkeypatch.setattr(kb_onboarding, "verify_embedding", lambda **_: 3072)
    # fixture为其余流程隔离过knowledge，直接使用模块原始函数。
    REAL_KNOWLEDGE(service.context, "TENANT-B")
    REAL_KNOWLEDGE(service.context, "TENANT-B")
    row = f["ctx"].store.get_ragflow_tenant("TENANT-B", "verify")
    assert row["model_checked_at"] and len(seen) == 1
    assert seen[0]["tenant_id"] == "TENANT-B"
    assert (
        kb_onboarding.decrypt_credential(row["credential_ciphertext"], settings.ragflow_credential_key)
        == seen[0]["password"]
    )


def test_workspace_bridge_uses_current_profile_and_switches_only_after_authorization(monkeypatch):
    import threading

    import httpx
    from dify_console_bridge import bridge

    seen = []

    def upstream(url, **kw):
        seen.append((url, kw))
        if url.endswith("/summary"):
            return 200, {"id": "platform", "role": "editor"}
        if url.endswith("/management/workspace"):
            return 200, {"workspace_id": APP, "platform": True}
        if url.endswith("/account/profile"):
            return 200, {"id": "account", "email": "current@example.invalid"}
        if url.endswith("/workspaces"):
            return 200, {"workspaces": []}
        if url.endswith("/workspace/member"):
            return 200, {"workspace_id": APP}
        if url.endswith("/workspaces/switch"):
            return 200, {"result": "success"}
        raise AssertionError(url)

    monkeypatch.setattr(bridge, "_request", upstream)
    server = bridge.make_server(
        bridge.BridgeSettings(
            "127.0.0.1", 0, "http://dify", "http://gateway", "key", "https://console.example", "platform"
        )
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = httpx.post(
            f"http://127.0.0.1:{server.server_port}/__aiops/manage",
            json={"action": "workspace", "workspace_id": APP},
            headers={
                "Origin": "https://console.example",
                "Referer": "https://console.example/apps",
                "Cookie": "access_token=session; csrf_token=csrf",
                "X-CSRF-Token": "csrf",
            },
        )
        assert r.status_code == 200, r.text
        join = next(kw for url, kw in seen if url.endswith("/workspace/member"))
        assert join["body"] == {
            "workspace_id": APP,
            "account_id": "account",
            "email": "current@example.invalid",
        }
        assert seen[-1][0].endswith("/workspaces/switch")
        assert seen[-1][1]["body"] == {"tenant_id": APP}
    finally:
        server.shutdown()
        server.server_close()


def test_consumer_model_approval_validates_private_credentials_and_duplicate_identity(managed):
    from pathlib import Path

    from aiops_diagnostics.dify.self_service_config import profiles

    f, *_ = managed
    path = Path(f["settings"].operator_profiles_file)
    body = json.loads(path.read_text())
    model = body["profiles"][0]["model"]
    body["profiles"][0]["consumer_models"] = [{**model, "name": "consumer-model"}]
    path.write_text(json.dumps(body))
    assert profiles(f["settings"])["default"]["consumer_models"][0]["name"] == "consumer-model"
    body["profiles"][0]["consumer_models"] = [model]
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="duplicate approved model"):
        profiles(f["settings"])
