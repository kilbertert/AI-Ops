"""防止跨环境错配、未开通租户放行与并发丢失；判据见环境登记验收合同。"""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.consumer.consumer_knowledge_grants import approved_business_endpoint
from aiops_diagnostics.gateway.environment_registry import EnvironmentRegistry, RegistryError, apply, load
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord


def rows(tmp_path):
    return [
        {
            "id": name,
            "enabled": True,
            "gateway_url": f"https://{name}.example.test",
            "cpms_base_url": f"https://{name}.example.test/charging-pile",
            "config_ref": str(tmp_path / f"{name}.env"),
            "tenants": [{"id": "same-tenant", "enabled": True}],
        }
        for name in ["env-a", "env-b"]
    ]


def test_same_tenant_resolves_only_in_selected_environment(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")
    for name in ["env-a", "env-b"]:
        settings = SimpleNamespace(environment_registry_file=str(path), environment=name)
        assert (
            approved_business_endpoint(settings, "same-tenant")
            == f"https://{name}.example.test/charging-pile"
        )
    with pytest.raises(RegistryError):
        EnvironmentRegistry(path, "env-c")
    with pytest.raises(RegistryError):
        EnvironmentRegistry(path, "env-a").require("unregistered")


def test_concurrent_updates_have_one_winner_and_atomic_audit(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")

    def publish(actor):
        try:
            return apply(path, rows(tmp_path), expected_revision=1, actor=actor)
        except RegistryError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(publish, ["first", "second"]))
    assert sorted(map(str, result)) == ["2", "ENVIRONMENT_REGISTRY_REVISION_CONFLICT"]
    body = load(path)
    assert [a["revision"] for a in body["audit"]] == [1, 2]
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("kind", ["duplicate", "http", "missing-key", "duplicate-tenant", "bool-string"])
def test_invalid_update_preserves_existing_registry(tmp_path, kind):
    path = tmp_path / "registry" / "directory.json"
    original = rows(tmp_path)
    apply(path, original, expected_revision=0, actor="operator")
    bad = copy.deepcopy(original)
    if kind == "duplicate":
        bad[1]["id"] = bad[0]["id"]
    elif kind == "http":
        bad[0]["cpms_base_url"] = "http://remote.example.test"
    elif kind == "missing-key":
        del bad[0]["config_ref"]
    elif kind == "duplicate-tenant":
        bad[0]["tenants"] *= 2
    else:
        bad[0]["enabled"] = "false"
    with pytest.raises(ValueError):
        apply(path, bad, expected_revision=1, actor="operator")
    assert load(path)["environments"] == original
    assert load(path)["revision"] == 1


class Resolver:
    def resolve(self, token, **kwargs):
        subject = SubjectRecord(b_user_id="same-user", c_user_id="customer", tenant_id="same-tenant")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="same-tenant",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({"aiops:orders:read"}),
        )


class Orders:
    def __init__(self):
        self.calls = 0

    def can_access(self, context, order_no):
        self.calls += 1
        return True


@pytest.mark.parametrize("disabled", ["tenant", "environment", "unreadable"])
def test_live_http_admission_rechecks_without_restart(tmp_path, disabled):
    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    config = tmp_path / "env-a.env"
    config.write_text("# isolated\n")
    config.chmod(0o600)
    apply(path, data, expected_revision=0, actor="operator")
    settings = GatewayServerSettings(
        data_home=tmp_path / "state",
        database_file=tmp_path / "state/gateway.db",
        server_config_file=config,
        environment="env-a",
        environment_registry_file=str(path),
    )
    orders = Orders()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=SimpleNamespace(shutdown=lambda: None),
        caller_resolver=Resolver(),
        order_authorizer=orders,
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer valid-company-session"}
        assert client.get("/v1/orders/order/access", headers=headers).status_code == 200
        assert orders.calls == 1
        if disabled == "unreadable":
            path.unlink()
        else:
            target = data[0]["tenants"][0] if disabled == "tenant" else data[0]
            target["enabled"] = False
            apply(path, data, expected_revision=1, actor="operator")
        response = client.get("/v1/orders/order/access", headers=headers)
        assert response.status_code == (503 if disabled == "unreadable" else 403)
        assert orders.calls == 1


def test_admin_cli_summary_never_prints_connections(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    incoming = tmp_path / "input.json"
    incoming.write_text(json.dumps(rows(tmp_path)))
    incoming.chmod(0o600)
    runner = CliRunner()
    result = runner.invoke(
        admin_app, ["environment-registry", "apply", "--registry", str(path), "--file", str(incoming)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"revision": 1, "environments": 2, "tenants": 2}
    result = runner.invoke(
        admin_app, ["environment-registry", "apply", "--registry", str(path), "--file", str(incoming)]
    )
    assert result.exit_code == 1
    assert "REVISION_CONFLICT" in result.output
    assert "example.test" not in result.output


def test_configuration_change_requires_rebind_not_silent_retarget(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    apply(path, data, expected_revision=0, actor="operator")
    registry = EnvironmentRegistry(path, "env-a")
    data[0]["cpms_base_url"] = "https://replacement.example.test"
    apply(path, data, expected_revision=1, actor="operator")
    with pytest.raises(RegistryError, match="CONFIGURATION_CHANGED"):
        registry.require("same-tenant")


def test_login_refuses_before_workspace_or_account_side_effects(tmp_path, monkeypatch):
    from aiops_diagnostics.dify import dify_login_service as login
    from aiops_diagnostics.dify.dify_login_company import CompanyIdentity
    from aiops_diagnostics.dify.dify_login_proxy import PermissionDenied

    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    data[0]["tenants"][0]["enabled"] = False
    apply(path, data, expected_revision=0, actor="operator")
    service = login.LoginService(
        company=None,
        dify=None,
        store=None,
        policy=None,
        owner_user_id="owner",
        environment_registry=EnvironmentRegistry(path, "env-a"),
    )
    monkeypatch.setattr(
        login,
        "company_login",
        lambda *a, **kw: CompanyIdentity(
            user_id="user", username="user", tenant_id="same-tenant", token="token"
        ),
    )
    monkeypatch.setattr(
        login, "read_permissions", lambda *a: pytest.fail("disabled tenant reached permission/workspace flow")
    )
    with pytest.raises(PermissionDenied, match="未开通"):
        service.login(username="user", password="unused", random_str="captcha", code="unused")


@pytest.mark.parametrize("revision", [True, 1.0, "1", -1])
def test_invalid_revision_cannot_corrupt_registry(tmp_path, revision):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")
    original = path.read_bytes()
    with pytest.raises(RegistryError):
        apply(path, rows(tmp_path), expected_revision=revision, actor="operator")
    assert path.read_bytes() == original


@pytest.mark.parametrize("unsafe", ["symlink", "writable"])
def test_registry_parent_is_private(tmp_path, unsafe):
    path = tmp_path / "registry" / "directory.json"
    apply(path, rows(tmp_path), expected_revision=0, actor="operator")
    if unsafe == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        path = alias / path.name
    else:
        path.parent.chmod(0o777)
    with pytest.raises(RegistryError):
        load(path)


def test_settings_bound_endpoint_refuses_live_retarget(tmp_path):
    path = tmp_path / "registry" / "directory.json"
    data = rows(tmp_path)
    apply(path, data, expected_revision=0, actor="operator")
    settings = SimpleNamespace(environment_registry_file=str(path), environment="env-a")
    assert approved_business_endpoint(settings, "same-tenant") == data[0]["cpms_base_url"]
    data[0]["cpms_base_url"] = "https://replacement.example.test"
    apply(path, data, expected_revision=1, actor="operator")
    with pytest.raises(RegistryError, match="CONFIGURATION_CHANGED"):
        approved_business_endpoint(settings, "same-tenant")


def test_tool_refusal_is_http_403_not_unhandled_500(tmp_path):
    from aiops_diagnostics.consumer.consumer_tool_grants import ConsumerToolRefused

    config = tmp_path / "test.env"
    config.write_text("# isolated")
    config.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path, database_file=tmp_path / "gateway.db", server_config_file=config
    )
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=SimpleNamespace(shutdown=lambda: None),
        caller_resolver=Resolver(),
    )

    @app.get("/test-tool-refusal")
    def refused():
        raise ConsumerToolRefused("TENANT_ENDPOINT_UNRESOLVED", "private detail")

    with TestClient(app) as client:
        response = client.get("/test-tool-refusal")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "TENANT_ENDPOINT_UNRESOLVED"
        assert "private detail" not in response.text
        assert response.headers["Cache-Control"] == "no-store"
