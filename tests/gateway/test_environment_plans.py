"""P4/D20：重放同计划不重复，权限/配置漂移不得成为可执行计划。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_registry as registry

ADMIN = ("control", "platform", "admin")


@pytest.fixture
def prepared(tmp_path):
    path = tmp_path / "registry/directory.json"
    registry.apply(
        path,
        [],
        expected_revision=0,
        actor="fixture",
        managers=[dict(environment=ADMIN[0], tenant_id=ADMIN[1], user_id=ADMIN[2], environments=["*"])],
    )
    draft = dict(
        id="a",
        name="环境A",
        enabled=False,
        gateway_url="https://a.example.test",
        cpms_base_url="https://company.example.test/cpms",
        company_gateway_url="https://company.example.test",
        deployment_target="host-a",
        credential_ref="config-a",
        timezone="Asia/Shanghai",
        tenants=[dict(id="same", name="同号租户", enabled=False)],
    )
    drafts.save(path, ADMIN, draft, expected_revision=0, registry_revision=1)
    files = {}
    for name in ("application", "installer", "configuration"):
        file = write_private_text(tmp_path / name, "fixture-secret-" + name)
        files[name] = {"path": str(file), "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
        if name != "configuration":
            files[name]["revision"] = "a" * 40
        else:
            files[name]["id"] = "config-a"
    ref = write_private_text(
        tmp_path / "references.json",
        json.dumps(
            {
                "schema": "aiops-environment-onboarding/1",
                "targets": [
                    dict(
                        id="host-a",
                        host="managed-host",
                        user="aiops",
                        port=18951,
                        python="3.12",
                        application=files["application"],
                        installer=files["installer"],
                        configurations=[files["configuration"]],
                    )
                ],
            }
        ),
    )
    return path, draft, ref, files


def test_concurrent_same_plan_is_persistent_and_never_activates(prepared, tmp_path):
    path, _, ref, _ = prepared
    before = path.read_bytes()

    def create(_):
        return plans.preflight(path, ADMIN, "a", 1, 1, ref)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(8)))
    assert len({r["id"] for r in results}) == 1
    result = plans.read(path, ADMIN, results[0]["id"], ref)
    assert result["state"] == "blocked" and result["executable"] is False
    assert {c["id"] for c in result["checks"] if c["state"] == "pending"} == {
        "config-projection",
        "host-identity",
        "capacity",
        "network",
        "business",
    }
    assert path.read_bytes() == before
    assert "fixture-secret" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)
    request = write_private_text(tmp_path / "read.json", json.dumps({"identity": ADMIN, "id": result["id"]}))
    output = CliRunner().invoke(
        admin_app,
        ["environment-plan", "--registry", str(path), "--file", str(request), "--references", str(ref)],
    )
    assert output.exit_code == 0 and json.loads(output.output)["id"] == result["id"]


def test_changed_input_expires_and_wrong_identity_cannot_read(prepared):
    path, draft, ref, files = prepared
    first = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    with pytest.raises((plans.PlanRefused, registry.RegistryError)):
        plans.read(path, ("other", "platform", "admin"), first["id"], ref)
    config = files["configuration"]
    write_private_text(Path(config["path"]), "changed-secret")
    assert plans.read(path, ADMIN, first["id"], ref)["state"] == "expired"
    draft["name"] = "环境A新名"
    drafts.save(path, ADMIN, draft, expected_revision=1, registry_revision=1)
    assert plans.read(path, ADMIN, first["id"], ref)["state"] == "expired"
    with pytest.raises(plans.PlanRefused, match="REVISION_CONFLICT"):
        plans.preflight(path, ADMIN, "a", 1, 1, ref)


def test_unknown_reference_is_visible_blocker_and_injection_never_read(prepared):
    path, draft, ref, _ = prepared
    draft["credential_ref"] = "unregistered"
    drafts.save(path, ADMIN, draft, expected_revision=1, registry_revision=1)
    result = plans.preflight(path, ADMIN, "a", 2, 1, ref)
    assert {"id": "references", "state": "blocked"} in result["checks"]
    assert result["executable"] is False
    assert plans.preflight(path, ADMIN, "a", 2, 1, "")["checks"][0]["state"] == "blocked"
    with pytest.raises(plans.PlanRefused):
        plans.read(path, ADMIN, "../../etc/passwd", ref)


def test_already_blocked_file_drift_expires_frozen_plan(prepared):
    path, _, reference, files = prepared
    config = Path(files["configuration"]["path"])
    write_private_text(config, "first-mismatched-config")
    first = plans.preflight(path, ADMIN, "a", 1, 1, reference)
    assert {"id": "configuration", "state": "blocked"} in first["checks"]
    assert plans.read(path, ADMIN, first["id"], reference)["state"] == "blocked"
    write_private_text(config, "second-mismatched-config")
    assert plans.read(path, ADMIN, first["id"], reference)["state"] == "expired"
    second = plans.preflight(path, ADMIN, "a", 1, 1, reference)
    assert second["id"] != first["id"]
