"""D40现场配置修复：原引用事务保留新源，不手改目标运行配置。"""

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops_diagnostics import admin_cli
from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_projection as projection
from aiops_diagnostics.gateway import environment_validation_worker as worker
from gateway.test_environment_plans import ADMIN, prepared  # noqa: F401


@pytest.fixture
def configuration(prepared, tmp_path):  # noqa: F811
    registry, draft, original, files = prepared
    reference = write_private_text(tmp_path / "onboarding-assets/references.json", original.read_text())
    before = reference.read_bytes()
    value = files["configuration"]
    key = "synth-target-key-independent-1234567890"
    source = write_private_text(
        tmp_path / "new/template.env",
        Path(value["path"]).read_text() + f'AIOPS_GATEWAY_ENVIRONMENT_TASK_KEY="{key}"\n',
    )
    request = dict(
        registry_path=registry,
        identity=ADMIN,
        environment="a",
        expected_sha256=hashlib.sha256(before).hexdigest(),
        configuration=dict(
            id=value["id"],
            path=str(source),
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_sha256=value["sha256"],
        ),
    )
    return reference, before, draft, request, key


def test_configuration_cas_preserves_other_inputs_and_next_projection(configuration):
    reference, before, draft, request, key = configuration
    result = plans.update_artifact_references(reference, **request)
    old, current = json.loads(before), json.loads(reference.read_text())
    item = current["targets"][0]["configurations"][0]
    preserved = json.loads(json.dumps(current))
    preserved["targets"][0]["configurations"] = old["targets"][0]["configurations"]
    assert preserved == old
    assert item["id"] == old["targets"][0]["configurations"][0]["id"]
    assert Path(item["path"]).read_text().endswith(f'AIOPS_GATEWAY_ENVIRONMENT_TASK_KEY="{key}"\n')
    assert Path(item["path"]).stat().st_mode & 0o777 == 0o600
    assert (
        Path(old["targets"][0]["configurations"][0]["path"]).read_text()
        == "AIOPS_MYSQL_PASSWORD='fixture-secret-configuration'\n"
    )
    assert (
        reference.with_name(reference.name + "." + request["expected_sha256"] + ".bak").read_bytes() == before
    )
    projected = projection.render(draft, item["path"], item["sha256"], "2026-11-22T00:00:00Z")
    assert key.encode() in projected["production.env"]
    assert json.loads(projected["environment.json"])["environments"][0]["enabled"] is False
    assert key not in json.dumps(result) and str(reference.parent) not in json.dumps(result)
    with pytest.raises(plans.PlanRefused):
        plans.update_artifact_references(reference, **request)


@pytest.mark.parametrize("fault", ["id", "old-hash", "new-hash", "permission", "reserved", "mixed"])
def test_bad_configuration_does_not_rewrite_reference(configuration, fault):
    reference, before, _, request, _ = configuration
    value = request["configuration"]
    if fault == "id":
        value["id"] = "another-config"
    elif fault == "old-hash":
        value["expected_sha256"] = "0" * 64
    elif fault == "new-hash":
        value["sha256"] = "0" * 64
    elif fault == "permission":
        Path(value["path"]).chmod(0o644)
    elif fault == "reserved":
        Path(value["path"]).write_text('AIOPS_GATEWAY_ENVIRONMENT="other"\n')
        value["sha256"] = hashlib.sha256(Path(value["path"]).read_bytes()).hexdigest()
    else:
        request["application"] = {}
    with pytest.raises((ValueError, RuntimeError)):
        plans.update_artifact_references(reference, **request)
    assert reference.read_bytes() == before
    assert not reference.with_name(reference.name + "." + request["expected_sha256"] + ".bak").exists()


def test_configuration_update_uses_original_cli_and_configured_paths(configuration, tmp_path, monkeypatch):
    from types import SimpleNamespace

    reference, _, _, request, key = configuration
    monkeypatch.setattr(
        admin_cli,
        "_settings_with_config_file",
        lambda _: SimpleNamespace(
            environment_onboarding_file=str(reference),
            environment_management_registry_file=str(request["registry_path"]),
        ),
    )
    file = write_private_text(
        tmp_path / "cli/input.json", json.dumps({k: v for k, v in request.items() if k != "registry_path"})
    )
    result = CliRunner().invoke(
        admin_cli.admin_app,
        [
            "environment-artifacts",
            "--registry",
            str(request["registry_path"]),
            "--references",
            str(reference),
            "--file",
            str(file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert key not in result.output


def test_worker_keeps_only_explicit_missing_key_code(tmp_path, monkeypatch):
    import subprocess

    (tmp_path / ".dev-host.toml").write_text('[[targets]]\nname="a"\nhost="a"\n')
    payload = {"action": "business-step", "capability": "order"}
    target = {"host": "a", "user": "aiops", "port": 18951}

    def missing(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1, ["fixture"], output='{"error":"ENVIRONMENT_VALIDATION_KEY_REQUIRED"}\n'
        )

    monkeypatch.setattr(worker.subprocess, "check_output", missing)
    result = worker.local_trial(tmp_path, target, "a", payload)
    assert result == {
        "capability": "order",
        "state": "blocked",
        "error_code": "ENVIRONMENT_VALIDATION_KEY_REQUIRED",
    }

    def unknown(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["fixture"], output="network PRIVATE")

    monkeypatch.setattr(worker.subprocess, "check_output", unknown)
    with pytest.raises(subprocess.CalledProcessError):
        worker.local_trial(tmp_path, target, "a", payload)


def test_local_dispatch_reports_missing_key_before_any_http(monkeypatch):
    import os
    import pwd
    from types import SimpleNamespace

    from aiops_diagnostics.common import private_files
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings

    monkeypatch.setattr(admin_cli.os, "getuid", lambda: 1000)
    monkeypatch.setattr(pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=1000))
    monkeypatch.setattr(private_files, "validate_private_file", lambda _: None)
    monkeypatch.setattr("dotenv.dotenv_values", lambda *_, **__: {})
    monkeypatch.setattr(
        GatewayServerSettings,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(environment="41", environment_task_key="")),
    )
    before = dict(os.environ)
    result = CliRunner().invoke(
        admin_cli.admin_app,
        ["--config", "/etc/aiops-41/gateway.env", "environment-trial-dispatch"],
        input="{}",
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "ENVIRONMENT_VALIDATION_KEY_REQUIRED"}
    assert dict(os.environ) == before


def test_configuration_reference_failure_keeps_original_and_private_version_reusable(
    configuration, monkeypatch
):
    reference, before, _, request, _ = configuration
    original_replace = Path.replace

    def fail_reference(self, target):
        if target == reference:
            raise OSError("synthetic interrupted update")
        return original_replace(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_reference)
        with pytest.raises(OSError):
            plans.update_artifact_references(reference, **request)
    assert reference.read_bytes() == before
    assert not reference.with_name(reference.name + "." + request["expected_sha256"] + ".bak").exists()
    plans.update_artifact_references(reference, **request)
    assert reference.read_bytes() != before
