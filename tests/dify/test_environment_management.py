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
    with pytest.raises(auth.SetupRefused, match="PERMISSION_REQUIRED"):
        management.catalogue(context, "session")
