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
from aiops_diagnostics.dify.self_service_api import SetupRequest
from aiops_diagnostics.dify.self_service_store import SelfServiceStore, SetupConflict, public_job
from aiops_diagnostics.execution import chat_execution
from aiops_diagnostics.publication.agent_manifest import (
    EnvironmentManifest,
    ManifestError,
    reconcile_dify_registry,
)

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
    mc = {
        "pre_prompt": "SOP_ONE",
        "model": {"name": "aiops-api"},
        "agent_mode": {
            "tools": [
                {
                    "provider_id": "provider-id",
                    "provider_type": "api",
                    "tool_name": "cpms_order_detail",
                    "enabled": True,
                },
                {
                    "provider_id": "provider-id",
                    "provider_type": "api",
                    "tool_name": "cpms_charging_samples",
                    "enabled": True,
                },
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
    return f, service, caller, mc, calls


def request(tenant="TENANT-B", key="request-000000001"):
    return SetupRequest(request_key=key, source_app_id=APP, source_version=2, tenant_id=tenant, name="新管家")


def test_one_operation_creates_real_v1_and_leaves_source_route(managed):
    f, service, caller, _, calls = managed
    old = service.route("TENANT-A")
    job = service.create(caller, request())
    assert service.create(caller, request())["id"] == job["id"]
    service.run(job["id"], "normal-session")
    completed = public_job(service.store.get(job["id"]))
    assert completed["state"] == "completed", completed
    assert completed["version_no"] == 1 and completed["published"]
    route = service.route("TENANT-B")
    snapshot = f["app"].state.gateway.agent_manager.store.version(route.agent_id, "TENANT-B", 1).snapshot
    assert snapshot["kind"] == "node-sop" and "SOP_ONE" in snapshot["node_manifest"]
    assert service.route("TENANT-A") == old
    service.run(job["id"], "normal-session")
    assert len(calls) == 1


def test_existing_tenant_gets_trial_without_replacing_route(managed):
    f, service, caller, _, _ = managed
    old = service.route("TENANT-A")
    job = service.create(caller, request("TENANT-A"))
    service.run(job["id"], "normal-session")
    result = public_job(service.store.get(job["id"]))
    assert result["state"] == "completed" and not result["published"], result
    assert service.route("TENANT-A") == old
    trial = chat_execution.trial_binding(f["settings"], result["app_id"])
    assert trial and trial.app_id == result["app_id"] and trial.key.startswith("operator-trial:")
    assert chat_execution.binding_for_key(f["settings"], trial.key) == trial


def test_failed_dependency_reuses_job_and_workspace(managed, monkeypatch):
    _, service, caller, _, _ = managed
    count = []

    def dependencies(*args):
        count.append(1)
        if len(count) == 1:
            raise ValueError("secret upstream response")
        return "provider-id"

    monkeypatch.setattr(prepare, "dependencies", dependencies)
    job = service.create(caller, request())
    service.run(job["id"], "normal-session")
    failed = service.store.get(job["id"])
    assert failed["state"] == "failed" and failed["error_code"] == "SETUP_STEP_FAILED"
    assert "secret" not in json.dumps(public_job(failed))
    service.run(job["id"], "normal-session")
    assert service.store.get(job["id"])["state"] == "completed"


def test_cross_tenant_create_requires_platform(managed, monkeypatch):
    _, service, _, _, calls = managed
    monkeypatch.setattr(auth, "tenants", lambda *_: [])
    with pytest.raises(auth.SetupRefused, match="PLATFORM_PERMISSION_REQUIRED"):
        service.create(auth.ManagerIdentity("tenant-user", "TENANT-A", False, "private"), request())
    assert calls == []


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


def test_old_manifest_cannot_replace_managed_source(managed):
    f, service, caller, _, _ = managed
    job = service.create(caller, request())
    service.run(job["id"], "normal-session")
    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding, DifyAppRegistry

    registry = DifyAppRegistry(f["settings"].database_file)
    manifest = EnvironmentManifest((), (DifyAppBinding("TENANT-B", "operator", APP, "other"),))
    with pytest.raises(ManifestError, match="页面管理"):
        reconcile_dify_registry(registry, manifest)


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


def test_catalogue_labels_source_by_customer_and_dify_app_name(managed):
    """来源下拉要回答「这是谁的哪个应用」：客户名 + **Dify 应用名**。

    智能体名（`operator-node`、`operator-<app_id>`）只在 Gateway 里存在，运营在工作室
    从来没看到过；只写它等于给出一个无法对照的字符串。
    """
    f, _, _, _, _ = managed
    calls = []

    def call(workspace_id, path):
        calls.append((workspace_id, path))
        return {"name": "优链充－平台全链路验收"}

    f["ctx"].self_service.console = SimpleNamespace(_call=call)
    f["settings"].dify_publish_api_key = "management-key"
    headers = {"Authorization": "Bearer management-key", "X-AIOps-Console-Session": "session"}
    source = next(
        s
        for s in f["client"].get("/v1/dify/management/catalogue", headers=headers).json()["sources"]
        if s["app_id"] == APP
    )
    assert source["tenant_id"] == "TENANT-A" and source["tenant_name"] == "客户A"
    assert source["app_name"] == "优链充－平台全链路验收"
    assert source["name"] == "客户A / 优链充－平台全链路验收"
    # 应用名几乎不变，而工作室每打开一次就拉一次目录：第二次不再问 Dify。
    f["client"].get("/v1/dify/management/catalogue", headers=headers)
    assert len(calls) == 1


def test_catalogue_falls_back_to_agent_name_when_dify_is_unreadable(managed):
    """读不到应用名只降级标签，不让「复制并开通」入口整个消失。"""
    f, _, _, _, _ = managed

    def explode(*_):
        raise RuntimeError("dify unreachable")

    f["ctx"].self_service.console = SimpleNamespace(_call=explode)
    f["settings"].dify_publish_api_key = "management-key"
    headers = {"Authorization": "Bearer management-key", "X-AIOps-Console-Session": "session"}
    response = f["client"].get("/v1/dify/management/catalogue", headers=headers)
    assert response.status_code == 200
    source = next(s for s in response.json()["sources"] if s["app_id"] == APP)
    assert source["name"] == "客户A / operator-node"


def test_http_management_rejects_injected_fields_and_missing_key(managed):
    f, _, _, _, _ = managed
    client = f["client"]
    assert client.post("/v1/dify/management/status", json={"app_id": APP}).status_code == 403
    f["settings"].dify_publish_api_key = "management-key"
    response = client.post(
        "/v1/dify/management/operations",
        headers={"Authorization": "Bearer management-key"},
        json={**request().model_dump(), "credential": "forged"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_unpublished_copy_runs_own_trial_without_changing_formal_route(managed, monkeypatch):
    f, service, caller, mc, _ = managed
    job = service.create(caller, request("TENANT-A"))
    service.run(job["id"], "normal-session")
    app_id = public_job(service.store.get(job["id"]))["app_id"]
    before = service.route("TENANT-A")
    f["settings"].dify_trial_api_key = "trial-key"
    monkeypatch.setattr(
        f["ctx"].operator_cpms,
        "console_identity",
        lambda _: {"token": "operator-private", "user_id": "OPERATOR-A", "tenant_id": "TENANT-A"},
    )
    r = f["client"].post(
        "/v1/dify/trial",
        headers={"Authorization": "Bearer trial-key", "X-AIOps-Console-Session": "normal"},
        json={
            "app_id": app_id,
            "order_no": "ORDER-1001",
            "question": "diagnose",
            "operator_preview": {"pre_prompt": mc["pre_prompt"], "tools": mc["agent_mode"]["tools"]},
        },
    )
    assert r.status_code == 202, r.text
    run = f["ctx"].agent_run_store.by_qa(r.json()["trial_id"])
    assert run.chat_binding == "operator-trial:" + app_id
    assert service.route("TENANT-A") == before


def test_retract_restore_and_source_registry_are_consistent(managed):
    f, service, caller, _, _ = managed
    job = service.create(caller, request())
    service.run(job["id"], "normal")
    app_id = public_job(service.store.get(job["id"]))["app_id"]
    route = service.route("TENANT-B")
    assert service.versions(caller, app_id) == [1]
    result = service.change_version(caller, app_id, route.revision)
    assert result["state"] == "retracted"
    restored = service.change_version(caller, app_id, result["revision"], 1)
    assert restored["state"] == "active" and restored["version_no"] == 1
    assert f["ctx"].runtime.dify_app_registry.lookup("TENANT-B", "operator").app_id == app_id
    with pytest.raises(SetupConflict):
        service.change_version(caller, app_id, route.revision)


def test_restart_exposes_retry_without_losing_resources(tmp_path):
    store = SelfServiceStore(tmp_path / "db")
    job = store.create("user", "request", {"tenant_id": "T", "tenant_name": "T", "name": "N"})
    store.claim(job["id"], "worker")
    store.save(job["id"], "worker", step="application", resources={"workspace_id": "WS"})
    store.recover()
    row = store.get(job["id"])
    assert row["state"] == "failed" and json.loads(row["resources_json"])["workspace_id"] == "WS"
    assert store.claim(job["id"], "new-worker")


def test_bridge_real_http_preserves_identity_and_rejects_cross_origin(monkeypatch):
    import threading

    import httpx

    from aiops_diagnostics.dify import dify_console_publish as bridge
    from aiops_diagnostics.dify.dify_console_publish import BridgeSettings, make_server

    seen = []

    def upstream(url, **kwargs):
        seen.append((url, kwargs))
        if url.endswith("/summary"):
            return 200, {"role": "editor", "id": "workspace"}
        if "/apps/" in url:
            return 200, {"id": APP}
        return 200, {"managed": True}

    monkeypatch.setattr(bridge, "_request", upstream)
    server = make_server(
        BridgeSettings(
            "127.0.0.1", 0, "http://dify", "http://gateway", "secret", "http://console.example", "workspace"
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        headers = {
            "Origin": "http://console.example",
            "Referer": "http://console.example/apps",
            "Cookie": "access_token=session; csrf_token=csrf",
            "X-CSRF-Token": "csrf",
        }
        response = httpx.post(
            f"http://127.0.0.1:{server.server_port}/__aiops/manage",
            headers=headers,
            json={"action": "status", "app_id": APP},
        )
        assert response.status_code == 200, response.text
        assert seen[-1][0].endswith("/v1/dify/management/status")
        assert seen[-1][1]["headers"]["X-AIOps-Console-Session"] == "session"
        count = len(seen)
        response = httpx.post(
            f"http://127.0.0.1:{server.server_port}/__aiops/manage",
            headers={**headers, "Origin": "http://foreign.example"},
            json={"action": "create", "tenant_id": "other"},
        )
        assert response.status_code == 403 and len(seen) == count
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_running_gateway_creates_and_reads_operation_over_http(managed):
    import time

    import httpx

    f, service, _, _, _ = managed
    f["ctx"].self_service.console = service.console
    f["settings"].dify_publish_api_key = "management-key"
    headers = {"Authorization": "Bearer management-key", "X-AIOps-Console-Session": "normal-session"}
    base = f["gateway_base"] + "/v1/dify/management"
    response = httpx.post(base + "/operations", headers=headers, json=request().model_dump())
    assert response.status_code == 202, response.text
    job = response.json()["id"]
    for _ in range(50):
        status = httpx.get(base + "/operations/" + job, headers=headers)
        if status.json()["state"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert status.json()["state"] == "completed", status.text
    assert status.json()["version_no"] == 1
    again = httpx.post(base + "/operations", headers=headers, json=request().model_dump())
    assert again.json()["id"] == job


def test_source_version_conflict_creates_no_operation(managed):
    _, service, caller, _, _ = managed
    payload = request().model_copy(update={"source_version": 1})
    with pytest.raises(SetupConflict, match="SETUP_SOURCE_CHANGED"):
        service.create(caller, payload)
    assert not service.store.recent(caller.user_id)


def test_interrupted_creation_does_not_publish_modified_tools(managed):
    _, service, caller, mc, _ = managed
    job = service.create(caller, request())
    mc["agent_mode"]["tools"][1]["enabled"] = False
    service.run(job["id"], "normal-session")
    result = public_job(service.store.get(job["id"]))
    assert result["state"] == "failed" and result["error_code"] == "SETUP_DRAFT_CHANGED"
    assert service.route("TENANT-B") is None


def test_tenant_native_copy_is_registered_without_replacing_formal_route(managed):
    _, service, caller, mc, _ = managed
    job = service.create(caller, request())
    service.run(job["id"], "normal-session")
    old = service.route("TENANT-B")
    for tool in mc["agent_mode"]["tools"]:
        tool["provider_name"] = "cpms-node"
    app_id = "aaaaaaaa-bbbb-cccc-dddd-ffffffffffff"
    tenant = auth.ManagerIdentity("tenant-editor", "TENANT-B", False, "private")
    assert service.adopt_draft(tenant, app_id)
    assert service.store.app(app_id)["tenant_id"] == "TENANT-B"
    assert service.route("TENANT-B") == old


def test_dynamic_profile_change_cannot_cross_environment(managed):
    from pathlib import Path

    from aiops_diagnostics.execution.node_publication import all_targets

    f, service, caller, _, _ = managed
    job = service.create(caller, request())
    service.run(job["id"], "normal-session")
    path = Path(f["settings"].operator_profiles_file)
    body = json.loads(path.read_text())
    body["profiles"][0]["target"]["environment"] = "other"
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="profile environment mismatch"):
        all_targets(f["settings"])
    with pytest.raises(ValueError, match="profile environment mismatch"):
        chat_execution.trial_binding(f["settings"], public_job(service.store.get(job["id"]))["app_id"])


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
    monkeypatch.setattr(prepare, "ensure_node_provider", lambda *_: None)
    original = REAL_DEPENDENCIES
    assert original(console, "workspace", profile, "https://gateway.example") == "provider"
    assert original(console, "workspace", profile, "https://gateway.example") == "provider"
    assert len(saved) == len(switches) == 1


def test_bridge_module_entry_rejects_without_disconnect(tmp_path):
    import os
    import re
    import subprocess
    import sys
    import time

    import httpx

    env = {k: v for k, v in os.environ.items() if not k.startswith("AIOPS_")}
    env.update(
        {
            "AIOPS_CONSOLE_BRIDGE_HOST": "127.0.0.1",
            "AIOPS_CONSOLE_BRIDGE_PORT": "0",
            "AIOPS_CONSOLE_BRIDGE_DIFY_URL": "http://127.0.0.1:1",
            "AIOPS_CONSOLE_BRIDGE_GATEWAY_URL": "http://127.0.0.1:1",
            "AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY": "fixture-only",
            "AIOPS_CONSOLE_BRIDGE_CONSOLE_ORIGIN": "https://dify.example",
            "AIOPS_CONSOLE_BRIDGE_WORKSPACE_ID": "fixture",
        }
    )
    log = tmp_path / "bridge.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "aiops_diagnostics.dify.dify_console_publish"],
            env=env,
            stdout=output,
            stderr=output,
        )
        try:
            match = None
            for _ in range(100):
                match = re.search(r"listening on 127\.0\.0\.1:(\d+)", log.read_text())
                if match:
                    break
                time.sleep(0.05)
            assert match, log.read_text()
            response = httpx.post(f"http://127.0.0.1:{match[1]}/__aiops/manage", json={"action": "catalogue"})
            assert response.status_code == 403
            assert response.headers["cache-control"] == "no-store"
            assert "请从Dify控制台操作" in response.text
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_retry_after_lost_completion_does_not_restore_retracted_route(managed, monkeypatch):
    _, service, caller, _, _ = managed
    job = service.create(caller, request())
    original = service.store.save

    def lose_complete(*args, **kwargs):
        if kwargs.get("state") == "completed":
            raise RuntimeError("lost response")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.store, "save", lose_complete)
    service.run(job["id"], "normal-session")
    route = service.route("TENANT-B")
    service.change_version(caller, route.app_id, route.revision)
    retracted = service.route("TENANT-B")
    monkeypatch.setattr(service.store, "save", original)
    service.run(job["id"], "normal-session")
    assert public_job(service.store.get(job["id"]))["state"] == "completed"
    assert service.route("TENANT-B") == retracted


def test_setup_cannot_complete_without_knowledge_space(managed, monkeypatch):
    _, service, caller, _, _ = managed

    def unavailable(*_):
        raise auth.SetupRefused("SETUP_KNOWLEDGE_FAILED")

    monkeypatch.setattr(prepare, "knowledge", unavailable, raising=False)
    job = service.create(caller, request())
    service.run(job["id"], "normal-session")
    result = public_job(service.store.get(job["id"]))
    assert result["state"] == "failed" and result["step"] == "knowledge"
    assert service.route("TENANT-B") is None


def test_legacy_completed_setup_becomes_retryable_without_losing_app(managed):
    _, service, caller, _, _ = managed
    job = service.create(caller, request())
    service.run(job["id"], "normal-session")
    before = public_job(service.store.get(job["id"]))
    with service.store.connection() as db:
        db.execute(
            "UPDATE operator_setup_jobs SET resources_json=json_remove(resources_json,'$.knowledge_ready') "
            "WHERE id=?",
            (job["id"],),
        )
    service.store.recover()
    result = public_job(service.store.get(job["id"]))
    assert result["state"] == "failed" and result["step"] == "knowledge"
    assert result["app_id"] == before["app_id"]
    service.run(job["id"], "normal-session")
    assert public_job(service.store.get(job["id"]))["state"] == "completed"


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
    from aiops_diagnostics.dify import dify_console_publish, dify_tenant_access

    f["settings"].dify_publish_api_key = "management-key"
    monkeypatch.setattr(f["ctx"].self_service, "workspace", lambda *_: "TENANT-B")
    monkeypatch.setattr(
        dify_console_publish,
        "_request",
        lambda *_, **__: (
            200,
            {"company_user_id": caller.user_id, "dify_account_id": "aaaaaaaa-bbbb-cccc-dddd-aaaaaaaaaaaa"},
        ),
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
    monkeypatch.setattr(kb_onboarding, "KbAdminClient", lambda _: client)
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

    from aiops_diagnostics.dify import dify_console_publish as bridge

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
