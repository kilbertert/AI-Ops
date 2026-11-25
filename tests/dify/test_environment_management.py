"""合同E1–E3：目录升级不丢数据，管理权限按环境绑定，旧版本写入不能覆盖新授权。"""

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.dify import environment_management as management
from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.dify.self_service_store import SetupConflict
from aiops_diagnostics.gateway import environment_registry as registry


def environments(tmp_path):
    return [
        dict(
            id=e,
            enabled=True,
            gateway_url=f"https://{e}.example.test",
            cpms_base_url=f"https://{e}.example.test/charging-pile",
            config_ref=str(tmp_path / f"{e}.env"),
            tenants=[dict(id="same", enabled=True)],
        )
        for e in ("a", "b")
    ]


def test_explicit_migration_preserves_audit_and_refuses_downgrade(tmp_path):
    path = tmp_path / "registry" / "catalogue.json"
    rows = environments(tmp_path)
    registry.apply(path, rows, expected_revision=0, actor="operator")
    original = registry.load(path)
    result = CliRunner().invoke(
        admin_app, ["environment-registry", "migrate", "--registry", str(path), "--expected-revision", "1"]
    )
    assert result.exit_code == 0, result.output
    upgraded = registry.load(path)
    assert upgraded["schema"] == registry.MANAGED_SCHEMA and upgraded["revision"] == 2
    assert upgraded["audit"][0] == original["audit"][0] and upgraded["managers"] == []
    assert json.loads(path.with_name("catalogue.json.v1-r1.backup").read_text()) == original
    assert registry.EnvironmentRegistry(path, "a").require("same")["id"] == "a"
    with pytest.raises(registry.RegistryError, match="DOWNGRADE_REFUSED"):
        registry.apply(path, rows, expected_revision=2, actor="old-cli")
    assert registry.load(path) == upgraded


@pytest.fixture
def managed(tmp_path, monkeypatch):
    path = tmp_path / "registry" / "catalogue.json"
    rows = environments(tmp_path)
    for r in rows:
        r.update(name=r["id"].upper(), company_gateway_url="https://company.example.test/api")
        r["tenants"][0]["name"] = "同号租户"
    managers = [dict(environment="a", tenant_id="platform", user_id="admin", environments=["a"])]
    registry.apply(path, rows, expected_revision=0, actor="operator", managers=managers)
    context = SimpleNamespace(settings=SimpleNamespace(environment="a", environment_registry_file=str(path)))
    identity = auth.ManagerIdentity("admin", "platform", True, "never-output-token")
    monkeypatch.setattr(auth, "identity", lambda *a, **kw: identity)
    app = FastAPI()
    for error, status in [(auth.SetupRefused, 403), (SetupConflict, 409)]:

        async def handler(_request, exc, status=status):
            return JSONResponse(status_code=status, content={"error": {"code": str(exc)}})

        app.add_exception_handler(error, handler)
    management.register(app, context, lambda: "session")
    with TestClient(app) as client:
        yield context, path, client, monkeypatch


def test_scoped_projection_and_wrong_environment_identity(managed):
    context, path, client, _ = managed
    response = client.get("/v1/dify/management/environments")
    assert response.status_code == 200
    assert [r["id"] for r in response.json()["environments"]] == ["a"]
    assert all(
        value not in response.text
        for value in ("example.test", str(path.parent), "never-output-token", "admin")
    )
    context.settings.environment = "b"
    assert client.get("/v1/dify/management/environments").status_code == 403


def test_update_revocation_and_conflict_preserve_other_environment(managed):
    _, path, client, _ = managed
    payload = dict(environment="a", revision=1, name="客户甲", enabled=False, tenant_id="same")
    assert client.post("/v1/dify/management/environments", json=payload).json() == {"revision": 2}
    with pytest.raises(registry.RegistryError, match="NOT_ENABLED"):
        registry.EnvironmentRegistry(path, "a").require("same")
    assert registry.EnvironmentRegistry(path, "b").require("same")["enabled"]
    assert client.post("/v1/dify/management/environments", json=payload).status_code == 409
    # 独立运维授权能恢复已停用准入，不通过浏览器提供新的授权主体。
    payload.update(revision=2, enabled=True)
    assert client.post("/v1/dify/management/environments", json=payload).status_code == 200
    body = registry.load(path)
    assert body["managers"][0]["environments"] == ["a"] and body["audit"][-1]["actor"].startswith("console-")


@pytest.mark.parametrize(
    "extra",
    [
        dict(environment="b"),
        dict(tenant_id="unknown"),
        dict(gateway_url="https://evil.test"),
        dict(managers=[]),
        dict(enabled="false"),
        dict(revision=True),
        dict(name=" bad "),
    ],
)
def test_illegal_changes_never_write(managed, extra):
    _, path, client, _ = managed
    old = path.read_bytes()
    payload = dict(environment="a", revision=1, name="客户", enabled=False)
    payload.update(extra)
    assert client.post("/v1/dify/management/environments", json=payload).status_code in (403, 422)
    assert path.read_bytes() == old


def test_platform_identity_does_not_grant_environment_access(managed):
    _, path, client, monkeypatch = managed
    monkeypatch.setattr(
        auth, "identity", lambda *a, **kw: auth.ManagerIdentity("other", "platform", True, "token")
    )
    assert client.get("/v1/dify/management/environments").status_code == 403
    body = registry.load(path)
    registry.apply(path, body["environments"], expected_revision=1, actor="operator", managers=[])
    assert client.get("/v1/dify/management/environments").status_code == 403


def test_bridge_real_http_environment_contract(monkeypatch):
    import threading

    import httpx

    from aiops_diagnostics.dify import dify_console_publish as bridge

    forwarded = []

    def upstream(url, **kwargs):
        if url.endswith("/summary"):
            return 200, {"role": "editor"}
        forwarded.append((url, kwargs))
        return 200, {"revision": 2}

    monkeypatch.setattr(bridge, "_request", upstream)
    server = bridge.make_server(
        bridge.BridgeSettings(
            "127.0.0.1",
            0,
            "http://dify",
            "http://gateway",
            "fixture-secret",
            "http://console.example",
            "workspace",
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/__aiops/manage"
    headers = {
        "Origin": "http://console.example",
        "Referer": "http://console.example/apps",
        "Cookie": "access_token=session; csrf_token=csrf",
        "X-CSRF-Token": "csrf",
    }
    try:
        with httpx.Client() as client:
            payload = dict(action="environment-update", environment="a", name="甲", enabled=False, revision=1)
            assert client.post(url, headers=headers, json=payload).status_code == 200
            assert forwarded[-1][0].endswith("/management/environments")
            assert forwarded[-1][1]["headers"]["X-AIOps-Console-Session"] == "session"
            count = len(forwarded)
            for change in ({"gateway_url": "https://evil.test"}, {"enabled": "true"}, {"revision": True}):
                assert client.post(url, headers=headers, json={**payload, **change}).status_code == 400
            assert (
                client.post(url, headers={**headers, "Origin": "http://evil.test"}, json=payload).status_code
                == 403
            )
            assert len(forwarded) == count
            candidate = dict(
                id="new",
                name="新环境",
                enabled=True,
                gateway_url="https://new.example.test",
                cpms_base_url="https://new.example.test/cpms",
                company_gateway_url="https://new.example.test",
                deployment_target="host",
                credential_ref="credentials",
                timezone="UTC",
                tenants=[],
            )
            draft_payload = dict(
                action="environment-draft-save", revision=0, registry_revision=1, draft=candidate
            )
            assert client.post(url, headers=headers, json=draft_payload).status_code == 200
            assert forwarded[-1][0].endswith("/management/environment-drafts")
            count = len(forwarded)
            assert (
                client.post(
                    url, headers=headers, json={**draft_payload, "registry_revision": True}
                ).status_code
                == 400
            )
            assert (
                client.post(
                    url, headers={**headers, "Origin": "http://evil.test"}, json=draft_payload
                ).status_code
                == 403
            )
            assert len(forwarded) == count
            assert (
                client.post(
                    url, headers=headers, json={**draft_payload, "draft": {**candidate, "secret": "bad"}}
                ).status_code
                == 400
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_management_auth_uses_live_company_identity_even_when_admission_disabled(tmp_path, monkeypatch):
    path = tmp_path / "registry" / "directory.json"
    rows = environments(tmp_path)
    for row in rows:
        row.update(name=row["id"], company_gateway_url="")
        row["tenants"][0].update(name="同号租户")
    rows[0]["enabled"] = False
    registry.apply(
        path,
        rows,
        expected_revision=0,
        actor="operator",
        managers=[dict(environment="a", tenant_id="same", user_id="admin", environments=["a"])],
    )
    context = SimpleNamespace(
        settings=SimpleNamespace(environment="a", environment_registry_file=str(path)),
        operator_cpms=SimpleNamespace(
            console_identity=lambda session: {"token": "live-token", "user_id": "admin", "tenant_id": "same"}
        ),
    )
    info = {
        "sysUser": {"id": "admin", "tenantId": "same", "type": -1},
        "permissions": ["aiops:agent:edit", "aiops:agent:publish"],
    }
    monkeypatch.setattr(auth, "company_get", lambda *a: info)
    assert management.catalogue(context, "session")["environments"][0]["enabled"] is False
    with pytest.raises(auth.SetupRefused, match="NOT_ENABLED"):
        auth.identity(context, "session")
    info["sysUser"]["tenantId"] = "foreign"
    with pytest.raises(auth.SetupRefused, match="IDENTITY_MISMATCH"):
        management.catalogue(context, "session")
    info["sysUser"]["tenantId"] = "same"
    info["permissions"] = []
    # 环境运维授权独立于编辑内容权限，普通应用管理仍保持原权限检查。
    assert management.catalogue(context, "session")["environments"][0]["id"] == "a"
    with pytest.raises(auth.SetupRefused, match="PERMISSION_REQUIRED"):
        auth.identity(context, "session", require_admission=False)


def test_upgrade_requires_backup_and_retries_after_interrupted_publish(tmp_path, monkeypatch):
    path = tmp_path / "registry" / "directory.json"
    rows = environments(tmp_path)
    registry.apply(path, rows, expected_revision=0, actor="operator")
    old = registry.load(path)
    for row in rows:
        row.update(name=row["id"], company_gateway_url="")
        row["tenants"][0]["name"] = "租户"
    with pytest.raises(registry.RegistryError, match="MIGRATION_REQUIRED"):
        registry.apply(path, rows, expected_revision=1, actor="operator", managers=[])
    write = registry.write_private_text

    def interrupted(target, content):
        if target == path:
            raise OSError("simulated publication interruption")
        return write(target, content)

    monkeypatch.setattr(registry, "write_private_text", interrupted)
    with pytest.raises(OSError):
        registry.migrate(path, expected_revision=1, actor="operator")
    assert registry.load(path) == old
    backup = path.with_name("directory.json.v1-r1.backup")
    assert json.loads(backup.read_text()) == old
    monkeypatch.setattr(registry, "write_private_text", write)
    assert registry.migrate(path, expected_revision=1, actor="operator") == 2
    assert registry.load(path)["managers"] == []


def test_draft_api_saves_connection_without_publishing(managed):
    _, path, client, _ = managed
    original = path.read_bytes()
    current = client.get("/v1/dify/management/environment-drafts")
    assert current.status_code == 200 and not current.json()["can_create"]
    row = current.json()["effective"][0]
    assert "config_ref" not in row
    row.update(deployment_target="host-a", credential_ref="env-a-secrets", timezone="Asia/Shanghai")
    row["gateway_url"] = "https://new.example.test"
    payload = dict(draft=row, revision=0, registry_revision=1)
    saved = client.post("/v1/dify/management/environment-drafts", json=payload)
    assert saved.status_code == 200 and saved.json()["state"] == "draft"
    assert saved.headers["cache-control"] == "no-store"
    assert path.read_bytes() == original
    assert client.get("/v1/dify/management/environment-drafts").json()["drafts"][0]["draft"] == row
    assert client.post("/v1/dify/management/environment-drafts", json=payload).status_code == 409
    row["id"] = "b"
    assert client.post("/v1/dify/management/environment-drafts", json=payload).status_code == 403


def test_draft_api_rejects_secrets_and_expired_company_session(managed):
    _, _, client, monkeypatch = managed
    row = client.get("/v1/dify/management/environment-drafts").json()["effective"][0]
    row.update(deployment_target="host-a", credential_ref="env-a-secrets", timezone="Asia/Shanghai")
    row["gateway_url"] = "https://user:secret@example.test"
    response = client.post(
        "/v1/dify/management/environment-drafts", json=dict(draft=row, revision=0, registry_revision=1)
    )
    assert response.status_code == 403 and "secret" not in response.text

    def expired(*a, **kw):
        raise auth.SetupRefused("SETUP_IDENTITY_MISMATCH")

    monkeypatch.setattr(auth, "identity", expired)
    assert client.get("/v1/dify/management/environment-drafts").status_code == 403


def test_plan_http_is_idempotent_and_refuses_stale_or_forged_inputs(managed):
    _, path, client, _ = managed
    row = client.get("/v1/dify/management/environment-drafts").json()["effective"][0]
    row.update(deployment_target="host-a", credential_ref="config-a", timezone="Asia/Shanghai")
    assert (
        client.post(
            "/v1/dify/management/environment-drafts",
            json={"draft": row, "revision": 0, "registry_revision": 1},
        ).status_code
        == 200
    )
    payload = {"environment": "a", "revision": 1, "registry_revision": 1}
    original = path.read_bytes()
    first = client.post("/v1/dify/management/environment-plans", json=payload)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json()["state"] == "blocked" and first.json()["executable"] is False
    assert (
        client.post("/v1/dify/management/environment-plans", json=payload).json()["id"] == first.json()["id"]
    )
    assert (
        client.post("/v1/dify/management/environment-plans", json={"id": first.json()["id"]}).status_code
        == 200
    )
    assert (
        client.post("/v1/dify/management/environment-plans", json={**payload, "revision": 2}).status_code
        == 409
    )
    assert (
        client.post("/v1/dify/management/environment-plans", json={**payload, "environment": "b"}).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/dify/management/environment-plans",
            json={**payload, "passed": True, "config_path": "/etc/passwd"},
        ).status_code
        == 403
    )
    assert path.read_bytes() == original


def test_plan_bridge_forwards_only_fixed_request(monkeypatch):
    import threading

    import httpx

    from aiops_diagnostics.dify import dify_console_publish as bridge

    calls = []

    def upstream(url, **kwargs):
        if url.endswith("/summary"):
            return 200, {"role": "editor"}
        calls.append((url, kwargs.get("body")))
        return 200, {"id": "plan_" + "a" * 32, "executable": False}

    monkeypatch.setattr(bridge, "_request", upstream)
    server = bridge.make_server(
        bridge.BridgeSettings(
            "127.0.0.1",
            0,
            "http://127.0.0.1:9",
            "http://127.0.0.1:9",
            "fixture",
            "https://console.example",
            "ws",
        )
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    headers = {
        "Cookie": "access_token=fixture; csrf_token=csrf",
        "X-CSRF-Token": "csrf",
        "Origin": "https://console.example",
        "Referer": "https://console.example/aiops/environments",
        "Sec-Fetch-Site": "same-origin",
    }
    payload = {"action": "environment-plan", "environment": "a", "revision": 1, "registry_revision": 1}
    try:
        url = f"http://127.0.0.1:{server.server_port}/__aiops/manage"
        response = httpx.post(url, json=payload, headers=headers)
        assert response.status_code == 200
        assert calls[-1] == (
            "http://127.0.0.1:9/v1/dify/management/environment-plans",
            {"environment": "a", "revision": 1, "registry_revision": 1},
        )
        before = len(calls)
        assert (
            httpx.post(url, json={**payload, "config_path": "/etc/passwd"}, headers=headers).status_code
            == 400
        )
        assert len(calls) == before
    finally:
        server.shutdown()
        server.server_close()


def test_self_service_identity_admits_platform_sentinel_only_when_env_enabled(tmp_path, monkeypatch):
    """ADR-0332：自助管理身份对平台哨兵（type=-1 且 tenantId=-1）按环境启用准入，目标租户仍逐租户核验。"""
    path = tmp_path / "registry" / "directory.json"
    rows = environments(tmp_path)
    for row in rows:
        row.update(name=row["id"], company_gateway_url="")
        row["tenants"][0].update(name="同号租户", enabled=False)
    registry.apply(
        path,
        rows,
        expected_revision=0,
        actor="operator",
        managers=[dict(environment="a", tenant_id="-1", user_id="admin", environments=["a"])],
    )
    context = SimpleNamespace(
        settings=SimpleNamespace(environment="a", environment_registry_file=str(path)),
        operator_cpms=SimpleNamespace(
            console_identity=lambda session: {"token": "live-token", "user_id": "admin", "tenant_id": "-1"}
        ),
    )
    info = {
        "sysUser": {"id": "admin", "tenantId": "-1", "type": -1},
        "permissions": ["aiops:agent:edit", "aiops:agent:publish"],
    }

    def fake_company_get(_context, _token, req_path):
        if req_path == "/tenant/list/my":
            return [{"id": "same", "name": "同号租户"}]
        return info

    monkeypatch.setattr(auth, "company_get", fake_company_get)

    # 启用环境：平台哨兵通过 identity，但 require_target 对停用业务租户仍拒绝。
    caller = auth.identity(context, "session")
    assert caller.platform is True and caller.tenant_id == "-1"
    with pytest.raises(auth.SetupRefused, match="NOT_ENABLED"):
        auth.require_target(context, caller, "same")

    # 非平台类型伪造 tenantId=-1 在启用环境也必须拒绝。
    info["sysUser"]["type"] = 1
    with pytest.raises(auth.SetupRefused, match="NOT_ENABLED"):
        auth.identity(context, "session")
    info["sysUser"]["type"] = -1

    # 停用环境：即使在 managers 中，普通自助 identity(require_admission=True) 仍拒绝，仅目录管理可用。
    rows[0]["enabled"] = False
    registry.apply(
        path,
        rows,
        expected_revision=1,
        actor="operator",
        managers=[dict(environment="a", tenant_id="-1", user_id="admin", environments=["a"])],
    )
    assert management.catalogue(context, "session")["environments"][0]["enabled"] is False
    with pytest.raises(auth.SetupRefused, match="NOT_ENABLED"):
        auth.identity(context, "session")

    # 同一目录管理员会话可直接通过目录恢复接口重新启用环境，恢复后普通自助身份立即恢复。
    assert management.change(
        context,
        "session",
        management.EnvironmentChange(environment="a", revision=2, name="a", enabled=True),
    ) == {"revision": 3}
    assert auth.identity(context, "session").tenant_id == "-1"
