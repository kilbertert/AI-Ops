"""草稿不能改变业务准入，管理授权与版本冲突均须真实持久化判定。"""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.onboarding import drafts, registry

ADMIN = ("control", "platform", "admin")


def draft(name="a"):
    return dict(
        id=name,
        name=name.upper(),
        enabled=True,
        gateway_url=f"https://{name}.example.test",
        cpms_base_url=f"https://{name}.example.test/cpms",
        company_gateway_url="https://company.example.test",
        deployment_target="host-a",
        credential_ref="credentials-a",
        timezone="Asia/Shanghai",
        tenants=[dict(id="same", name="同号租户", enabled=True)],
    )


def initialize(tmp_path, rows=None, scope=None):
    path = tmp_path / "registry" / "directory.json"
    registry.apply(
        path,
        rows or [],
        expected_revision=0,
        actor="operator",
        managers=[
            dict(environment=ADMIN[0], tenant_id=ADMIN[1], user_id=ADMIN[2], environments=scope or ["*"])
        ],
    )
    return path


def test_draft_preserves_effective_and_survives_reopen(tmp_path):
    proposed = draft()
    effective = {
        k: v for k, v in proposed.items() if k not in {"timezone", "credential_ref", "deployment_target"}
    }
    effective["config_ref"] = str(tmp_path / "server.env")
    path = initialize(tmp_path, [effective])
    original = path.read_bytes()
    live = registry.EnvironmentRegistry(path, "a")
    proposed["gateway_url"] = "https://changed.example.test"
    saved = drafts.save(path, ADMIN, proposed, expected_revision=0, registry_revision=1)
    assert saved["state"] == "draft" and saved["acceptance"] == "unverified"
    assert path.read_bytes() == original and live.require("same")["gateway_url"] == effective["gateway_url"]
    assert drafts.catalogue(path, ADMIN)["drafts"][0]["draft"] == proposed
    assert str(tmp_path) not in json.dumps(drafts.catalogue(path, ADMIN))
    db = path.with_name(path.name + ".drafts.db")
    assert db.stat().st_mode & 0o077 == 0
    with gateway_db.connection(db, readonly=True) as c:
        assert c.execute("SELECT digest FROM environment_draft_audit").fetchone()[0] == saved["digest"]


def test_empty_directory_manager_does_not_enable_runtime(tmp_path):
    path = initialize(tmp_path)
    config = tmp_path / "production.env"
    config.write_text("# isolated configuration\n")
    config.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path / "data",
        database_file=tmp_path / "data/gateway.db",
        environment="control",
        environment_management_registry_file=str(path),
        server_config_file=config,
    )
    settings.validate()
    assert drafts.catalogue(path, ADMIN)["can_create"]
    assert drafts.save(path, ADMIN, draft(), expected_revision=0, registry_revision=1)["revision"] == 1
    with pytest.raises(registry.RegistryError, match="NOT_REGISTERED"):
        registry.EnvironmentRegistry(path, "a")
    with pytest.raises(registry.RegistryError, match="FORBIDDEN"):
        drafts.catalogue(path, ("other", *ADMIN[1:]))


def test_concurrent_draft_and_registry_revision_changes_are_rejected(tmp_path):
    path = initialize(tmp_path)

    def save(name):
        proposed = draft()
        proposed["name"] = name
        try:
            return drafts.save(path, ADMIN, proposed, expected_revision=0, registry_revision=1)["revision"]
        except registry.RegistryError as e:
            return str(e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["one", "two"]))
    assert sorted(map(str, results)) == ["1", "ENVIRONMENT_DRAFT_REVISION_CONFLICT"]
    old = registry.load(path)
    registry.apply(path, [], expected_revision=1, actor="operator", managers=old["managers"])
    assert drafts.catalogue(path, ADMIN)["drafts"][0]["registry_changed"]
    with pytest.raises(registry.RegistryError, match="REGISTRY_REVISION_CONFLICT"):
        drafts.save(path, ADMIN, draft(), expected_revision=1, registry_revision=1)
    registry.apply(path, [], expected_revision=2, actor="operator", managers=[])
    with pytest.raises(registry.RegistryError, match="FORBIDDEN"):
        drafts.save(path, ADMIN, draft(), expected_revision=1, registry_revision=3)


@pytest.mark.parametrize(
    "change",
    [
        {"gateway_url": "https://user:password@example.test"},
        {"gateway_url": "http://remote.example.test"},
        {"credential_ref": "../private"},
        {"timezone": "../UTC"},
        {"config_ref": "/private"},
        {"enabled": "true"},
        {"tenants": [{"id": "same", "name": "one", "enabled": True}] * 2},
    ],
)
def test_invalid_draft_does_not_create_state(tmp_path, change):
    path = initialize(tmp_path)
    proposed = {**draft(), **copy.deepcopy(change)}
    with pytest.raises(ValueError):
        drafts.save(path, ADMIN, proposed, expected_revision=0, registry_revision=1)
    assert drafts.catalogue(path, ADMIN)["drafts"] == []


def test_scoped_manager_cannot_create_or_read_unassigned_environment(tmp_path):
    effective = {
        k: v
        for k, v in draft("control").items()
        if k not in {"timezone", "credential_ref", "deployment_target"}
    }
    effective["config_ref"] = str(tmp_path / "server.env")
    path = initialize(tmp_path, [effective], ["control"])
    assert (
        drafts.save(path, ADMIN, draft("control"), expected_revision=0, registry_revision=1)["revision"] == 1
    )
    with pytest.raises(registry.RegistryError, match="FORBIDDEN"):
        drafts.save(path, ADMIN, draft("b"), expected_revision=0, registry_revision=1)
    assert not drafts.catalogue(path, ADMIN)["can_create"]


def test_failed_initial_save_keeps_empty_directory_readable(tmp_path):
    path = initialize(tmp_path)
    with pytest.raises(registry.RegistryError, match="DRAFT_REVISION_CONFLICT"):
        drafts.save(path, ADMIN, draft(), expected_revision=1, registry_revision=1)
    assert drafts.catalogue(path, ADMIN)["drafts"] == []
    assert drafts.save(path, ADMIN, draft(), expected_revision=0, registry_revision=1)["revision"] == 1


def test_cli_can_bootstrap_management_and_cannot_silently_select_second_directory(tmp_path):
    from typer.testing import CliRunner

    from aiops_diagnostics.admin_cli import admin_app

    path = tmp_path / "directory" / "registry.json"
    source = tmp_path / "input.json"
    source.write_text(
        json.dumps(
            {
                "environments": [],
                "managers": [
                    dict(environment=ADMIN[0], tenant_id=ADMIN[1], user_id=ADMIN[2], environments=["*"])
                ],
            }
        )
    )
    source.chmod(0o600)
    result = CliRunner().invoke(
        admin_app, ["environment-registry", "apply", "--registry", str(path), "--file", str(source)]
    )
    assert result.exit_code == 0 and json.loads(result.output)["environments"] == 0
    assert drafts.catalogue(path, ADMIN)["can_create"]
    source.write_text(
        json.dumps({"identity": list(ADMIN), "draft": draft(), "revision": 0, "registry_revision": 1})
    )
    result = CliRunner().invoke(
        admin_app, ["environment-draft", "--registry", str(path), "--file", str(source)]
    )
    assert result.exit_code == 0 and json.loads(result.output)["state"] == "draft"
    assert "example.test" not in result.output
    assert registry.load(path)["environments"] == []
    settings = GatewayServerSettings(
        data_home=tmp_path / "data",
        database_file=tmp_path / "data/gateway.db",
        environment="control",
        environment_registry_file=str(tmp_path / "other.json"),
        environment_management_registry_file=str(path),
    )
    with pytest.raises(ValueError, match="same environment registry"):
        settings.validate()


def test_real_gateway_management_bootstraps_without_operator_profiles(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aiops_diagnostics.dify import self_service_identity as auth
    from aiops_diagnostics.gateway.gateway_api import create_gateway_app

    path = initialize(tmp_path)
    config = tmp_path / "production.env"
    config.write_text("# isolated\n")
    config.chmod(0o600)
    settings = GatewayServerSettings(
        data_home=tmp_path / "state",
        database_file=tmp_path / "state/gateway.db",
        environment="control",
        server_config_file=config,
        environment_management_registry_file=str(path),
        dify_publish_api_key="service-fixture",
        dify_console_base_url="http://127.0.0.1:1",
        dify_console_api_key="console-fixture",
    )
    app = create_gateway_app(settings=settings)
    monkeypatch.setattr(
        auth, "identity", lambda *a, **k: auth.ManagerIdentity("admin", "platform", True, "token")
    )
    with TestClient(app) as client:
        url = "/v1/dify/management/environment-drafts"
        assert client.get(url).status_code == 403
        headers = {"Authorization": "Bearer service-fixture", "X-AIOps-Console-Session": "normal-session"}
        assert client.get(url, headers=headers).json()["effective"] == []
        response = client.post(
            url, headers=headers, json={"draft": draft(), "revision": 0, "registry_revision": 1}
        )
        assert response.status_code == 200 and response.json()["state"] == "draft"
        assert registry.load(path)["environments"] == []
