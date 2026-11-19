"""P4/D20：重放同计划不重复，权限/配置漂移不得成为可执行计划。"""

import hashlib
import io
import json
import os
import tarfile
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
        content = (
            "AIOPS_MYSQL_PASSWORD='fixture-secret-configuration'\n"
            if name == "configuration"
            else "fixture-secret-" + name
        )
        file = write_private_text(tmp_path / name, content)
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


def test_matching_outer_hash_does_not_validate_non_archive_contents(prepared):
    path, _, ref, _ = prepared
    result = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    checks = {c["id"]: c["state"] for c in result["checks"]}
    assert checks["application"] == checks["installer"] == "passed"
    assert checks["application-contents"] == checks["installer-contents"] == "blocked"
    assert checks["embedded-revision"] == "blocked" and result["executable"] is False


@pytest.mark.parametrize("key", ["application", "installer"])
def test_truncated_gzip_with_correct_outer_hash_is_blocked_and_readable(prepared, key):
    import gzip

    path, _, ref, files = prepared
    old = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    file = Path(files[key]["path"])
    write_private_text(file, "")
    file.write_bytes(gzip.compress(b"archive" * 2000)[:15])
    files[key]["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
    body = json.loads(ref.read_text())
    body["targets"][0][key] = files[key]
    write_private_text(ref, json.dumps(body))
    result = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    assert {"id": key, "state": "passed"} in result["checks"]
    assert {"id": key + "-contents", "state": "blocked"} in result["checks"]
    assert plans.read(path, ADMIN, result["id"], ref)["state"] == "blocked"
    assert plans.read(path, ADMIN, old["id"], ref)["state"] == "expired"


@pytest.mark.parametrize("key", ["application", "installer"])
def test_invalid_deflate_after_valid_member_header_is_blocked(prepared, key):
    import zlib

    path, _, ref, files = prepared
    old = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    member = tarfile.TarInfo("manifest.json" if key == "application" else "install.py")
    member.size = 32768
    compressor = zlib.compressobj(wbits=-15)
    partial = compressor.compress(member.tobuf() + b"x" * 16384) + compressor.flush(zlib.Z_FULL_FLUSH)
    # gzip header, valid first block, then DEFLATE's reserved block type (RFC1951).
    raw = b"\x1f\x8b\x08\x00" + b"\x00" * 6 + partial + b"\x07" + b"\x00" * 8
    file = Path(files[key]["path"])
    file.write_bytes(raw)
    files[key]["sha256"] = hashlib.sha256(raw).hexdigest()
    body = json.loads(ref.read_text())
    body["targets"][0][key] = files[key]
    write_private_text(ref, json.dumps(body))
    result = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    assert {"id": key + "-contents", "state": "blocked"} in result["checks"]
    assert plans.read(path, ADMIN, result["id"], ref)["state"] == "blocked"
    assert plans.read(path, ADMIN, old["id"], ref)["state"] == "expired"


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


def test_plan_checks_embedded_artifact_identity_without_executing_it(prepared, tmp_path):
    path, _, ref, files = prepared
    revision = "a" * 40
    application = {
        "manifest.json": json.dumps(
            dict(
                schema=1,
                revision=revision,
                python="3.12",
                platform="linux-x86_64",
                version="1.0.0+" + revision[:12],
            )
        ).encode(),
        "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"opaque wheel",
    }
    sentinel = tmp_path / "executed"
    installer = {
        "REVISION": revision.encode(),
        **{
            name: f"from pathlib import Path; Path({str(sentinel)!r}).touch()".encode()
            for name in ("install.py", "runtime_check.py", "probe.py")
        },
    }

    def pack(key, contents):
        contents = {
            **contents,
            "SHA256SUMS": "".join(
                hashlib.sha256(value).hexdigest() + "  " + name + "\n" for name, value in contents.items()
            ).encode(),
        }
        with tarfile.open(files[key]["path"], "w:gz") as tar:
            for name, value in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
        Path(files[key]["path"]).chmod(0o600)
        files[key]["sha256"] = hashlib.sha256(Path(files[key]["path"]).read_bytes()).hexdigest()
        body = json.loads(ref.read_text())
        body["targets"][0][key] = files[key]
        write_private_text(ref, json.dumps(body))

    pack("application", application)
    pack("installer", installer)
    first = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    checks = {c["id"]: c["state"] for c in first["checks"]}
    assert all(
        checks[k] == "passed" for k in ("application-contents", "installer-contents", "embedded-revision")
    )
    assert first["executable"] is False and not sentinel.exists()
    # An internally valid archive from a different revision must not satisfy the declared plan.
    pack("installer", {**installer, "REVISION": ("b" * 40).encode()})
    second = plans.preflight(path, ADMIN, "a", 1, 1, ref)
    assert {"id": "installer-contents", "state": "passed"} in second["checks"]
    assert {"id": "embedded-revision", "state": "blocked"} in second["checks"]
    assert plans.read(path, ADMIN, first["id"], ref)["state"] == "expired"
    assert second["executable"] is False and not sentinel.exists()


def _reference_archive(tmp_path, name, members):
    from aiops_diagnostics.gateway.environment_artifacts import installer_manifest, manifest

    contents = dict(members)
    contents["SHA256SUMS"] = "".join(
        hashlib.sha256(value).hexdigest() + "  " + member + chr(10) for member, value in members.items()
    ).encode()
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as bundle:
        for member, value in contents.items():
            info = tarfile.TarInfo(member)
            info.size = len(value)
            bundle.addfile(info, io.BytesIO(value))
    path.chmod(0o600)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    revision = (
        manifest(path, sha, "3.11" if "311" in name else "3.12")["revision"]
        if name.startswith("app")
        else installer_manifest(path, sha)
    )
    return {"path": str(path), "sha256": sha, "revision": revision}


def test_artifact_reference_update_is_cas_atomic_and_preserves_config(prepared, tmp_path):
    registry_path, _, reference, _ = prepared
    revision = "a" * 40
    asset_dir = tmp_path / "input"
    asset_dir.mkdir(mode=0o700)
    app = _reference_archive(
        asset_dir,
        "app.tar.gz",
        {
            "manifest.json": json.dumps(
                dict(
                    schema=1,
                    revision=revision,
                    python="3.12",
                    platform="linux-x86_64",
                    version="1.0.0+" + revision[:12],
                )
            ).encode(),
            "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"opaque wheel",
        },
    )
    installer = _reference_archive(
        asset_dir,
        "installer.tar.gz",
        {
            "REVISION": revision.encode(),
            "install.py": b"pass",
            "runtime_check.py": b"pass",
            "probe.py": b"pass",
        },
    )
    reference_dir = tmp_path / "onboarding-assets"
    reference_dir.mkdir(mode=0o700)
    reference = reference_dir / "references.json"
    reference.write_bytes(
        json.dumps(
            {
                "schema": "aiops-environment-onboarding/1",
                "targets": [
                    {
                        "id": "host-a",
                        "host": "managed-host",
                        "user": "aiops",
                        "port": 18951,
                        "python": "3.12",
                        "application": {"path": "/old/app.tar.gz", "sha256": "b" * 64, "revision": "b" * 40},
                        "installer": {
                            "path": "/old/installer.tar.gz",
                            "sha256": "b" * 64,
                            "revision": "b" * 40,
                        },
                        "configurations": [
                            {"id": "config-a", "path": "/private/config.env", "sha256": "c" * 64}
                        ],
                    }
                ],
            }
        ).encode()
    )
    reference.chmod(0o600)
    before = reference.read_bytes()
    result = plans.update_artifact_references(
        reference,
        registry_path=registry_path,
        identity=ADMIN,
        environment="a",
        expected_sha256=hashlib.sha256(before).hexdigest(),
        application=app,
        installer=installer,
    )
    updated = json.loads(reference.read_text())
    assert result["environment"] == "a"
    expected_application = reference.parent / revision / "application-3.12.tar.gz"
    expected_installer = reference.parent / revision / "installer.tar.gz"
    assert updated["targets"][0]["application"] == {**app, "path": str(expected_application)}
    assert updated["targets"][0]["installer"] == {**installer, "path": str(expected_installer)}
    assert expected_application.read_bytes() == Path(app["path"]).read_bytes()
    assert expected_installer.read_bytes() == Path(installer["path"]).read_bytes()
    assert updated["targets"][0]["configurations"] == json.loads(before)["targets"][0]["configurations"]
    backup = reference.with_name(f"{reference.name}.{hashlib.sha256(before).hexdigest()}.bak")
    assert backup.read_bytes() == before
    assert result["backup_sha256"] == hashlib.sha256(before).hexdigest()
    assert reference.stat().st_mode & 0o077 == backup.stat().st_mode & 0o077 == 0
    application_311 = _reference_archive(
        asset_dir,
        "app-311.tar.gz",
        {
            "manifest.json": json.dumps(
                dict(
                    schema=1,
                    revision=revision,
                    python="3.11",
                    platform="linux-x86_64",
                    version="1.0.0+" + revision[:12],
                )
            ).encode(),
            "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"opaque wheel 311",
        },
    )
    checked_311 = plans.manifest(Path(application_311["path"]), application_311["sha256"], "3.11")
    checked_312 = plans.manifest(expected_application, updated["targets"][0]["application"]["sha256"], "3.12")
    assert checked_311["revision"] == checked_312["revision"] == revision
    assert expected_application != Path(application_311["path"])
    with pytest.raises(plans.PlanRefused, match="ALREADY_CURRENT"):
        plans.update_artifact_references(
            reference,
            registry_path=registry_path,
            identity=ADMIN,
            environment="a",
            expected_sha256=result["references_sha256"],
            application=app,
            installer=installer,
        )


def _current_test_artifacts(tmp_path):
    revision = "e" * 40
    directory = tmp_path / "current-artifacts"
    directory.mkdir(mode=0o700)
    app = _reference_archive(
        directory,
        "application.tar.gz",
        {
            "manifest.json": json.dumps(
                {
                    "schema": 1,
                    "revision": revision,
                    "python": "3.12",
                    "platform": "linux-x86_64",
                    "version": "1.0.0+" + revision[:12],
                }
            ).encode(),
            "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"wheel",
        },
    )
    installer = _reference_archive(
        directory,
        "installer.tar.gz",
        {
            "REVISION": revision.encode(),
            "install.py": b"pass",
            "runtime_check.py": b"pass",
            "probe.py": b"pass",
        },
    )
    return app, installer


def test_artifact_reference_update_can_change_only_configuration(prepared, tmp_path):
    registry_path, _, reference, _ = prepared
    reference = write_private_text(
        tmp_path / "onboarding-assets" / "references.json",
        reference.read_text(),
    )
    source = write_private_text(
        tmp_path / "configuration.env",
        "AIOPS_MYSQL_PASSWORD=fixture-secret\nAIOPS_GATEWAY_ENVIRONMENT_TASK_KEY=" + "k" * 48 + "\n",
    )
    original = reference.read_bytes()
    app, installer = _current_test_artifacts(tmp_path)
    body = json.loads(reference.read_text())
    body["targets"][0]["application"] = app
    body["targets"][0]["installer"] = installer
    write_private_text(reference, json.dumps(body))
    original = reference.read_bytes()
    result = plans.update_artifact_references(
        reference,
        registry_path=registry_path,
        identity=ADMIN,
        environment="a",
        expected_sha256=hashlib.sha256(original).hexdigest(),
        application=app,
        installer=installer,
        configuration={"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
    )
    updated = json.loads(reference.read_text())["targets"][0]
    assert updated["application"]["sha256"] == app["sha256"]
    assert updated["installer"]["sha256"] == installer["sha256"]
    assert Path(updated["application"]["path"]).read_bytes() == Path(app["path"]).read_bytes()
    assert Path(updated["installer"]["path"]).read_bytes() == Path(installer["path"]).read_bytes()
    assert updated["configurations"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert Path(updated["configurations"][0]["path"]).read_bytes() == source.read_bytes()
    assert result["configuration_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_same_digest_configuration_migrates_legacy_path_to_fixed_copy(prepared, tmp_path):
    registry_path, _, reference, _ = prepared
    reference = write_private_text(
        tmp_path / "onboarding-assets" / "references.json",
        reference.read_text(),
    )
    source = write_private_text(
        tmp_path / "configuration.env",
        "AIOPS_MYSQL_PASSWORD=fixture-secret\nAIOPS_GATEWAY_ENVIRONMENT_TASK_KEY=" + "k" * 48 + "\n",
    )
    body = json.loads(reference.read_text())
    body["targets"][0]["configurations"] = [
        {
            "id": "config-a",
            "path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    ]
    write_private_text(reference, json.dumps(body))
    application, installer = _current_test_artifacts(tmp_path)
    body = json.loads(reference.read_text())
    body["targets"][0]["application"] = application
    body["targets"][0]["installer"] = installer
    write_private_text(reference, json.dumps(body))
    original = reference.read_bytes()

    plans.update_artifact_references(
        reference,
        registry_path=registry_path,
        identity=ADMIN,
        environment="a",
        expected_sha256=hashlib.sha256(original).hexdigest(),
        application=application,
        installer=installer,
        configuration={"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
    )

    updated = json.loads(reference.read_text())["targets"][0]["configurations"][0]
    assert Path(updated["path"]).name == updated["sha256"] + ".env"
    assert Path(updated["path"]).parent.name == "configurations"
    assert Path(updated["path"]).read_bytes() == source.read_bytes()


def test_configuration_source_drift_during_copy_refuses_reference_update(prepared, tmp_path, monkeypatch):
    registry_path, _, reference, _ = prepared
    reference = write_private_text(
        tmp_path / "onboarding-assets" / "references.json",
        reference.read_text(),
    )
    source = write_private_text(
        tmp_path / "configuration.env",
        "AIOPS_MYSQL_PASSWORD=fixture-secret\nAIOPS_GATEWAY_ENVIRONMENT_TASK_KEY=" + "k" * 48 + "\n",
    )
    application, installer = _current_test_artifacts(tmp_path)
    body = json.loads(reference.read_text())
    body["targets"][0]["application"] = application
    body["targets"][0]["installer"] = installer
    write_private_text(reference, json.dumps(body))
    original = reference.read_bytes()
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    real_copyfileobj = plans.shutil.copyfileobj

    def replace_after_copy(reader, writer, *args, **kwargs):
        real_copyfileobj(reader, writer, *args, **kwargs)
        write_private_text(source, "AIOPS_MYSQL_PASSWORD=changed\n")

    monkeypatch.setattr(plans.shutil, "copyfileobj", replace_after_copy)
    with pytest.raises(plans.PlanRefused, match="CONFIGURATION_CHANGED"):
        plans.update_artifact_references(
            reference,
            registry_path=registry_path,
            identity=ADMIN,
            environment="a",
            expected_sha256=hashlib.sha256(original).hexdigest(),
            application=application,
            installer=installer,
            configuration={"path": str(source), "sha256": expected},
        )
    assert reference.read_bytes() == original


def test_configuration_reference_update_restores_old_file_if_directory_sync_fails(
    prepared, tmp_path, monkeypatch
):
    registry_path, _, reference, _ = prepared
    app, installer = _current_test_artifacts(tmp_path)
    body = json.loads(reference.read_text())
    body["targets"][0]["application"] = app
    body["targets"][0]["installer"] = installer
    write_private_text(reference, json.dumps(body))
    reference = write_private_text(
        tmp_path / "onboarding-assets" / "references.json",
        reference.read_text(),
    )
    source = write_private_text(
        tmp_path / "configuration.env",
        "AIOPS_MYSQL_PASSWORD=fixture-secret\nAIOPS_GATEWAY_ENVIRONMENT_TASK_KEY=" + "k" * 48 + "\n",
    )
    original = reference.read_bytes()
    real_fsync, failed = os.fsync, False
    parent = str(reference.parent)

    def fail_reference_dir(fd):
        nonlocal failed
        if (
            not failed
            and os.path.realpath(f"/proc/self/fd/{fd}") == parent
            and os.path.isdir(f"/proc/self/fd/{fd}")
        ):
            failed = True
            raise OSError("injected references directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(plans.os, "fsync", fail_reference_dir)
    with pytest.raises(OSError, match="references directory sync failure"):
        plans.update_artifact_references(
            reference,
            registry_path=registry_path,
            identity=ADMIN,
            environment="a",
            expected_sha256=hashlib.sha256(original).hexdigest(),
            application=app,
            installer=installer,
            configuration={"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        )
    assert failed and reference.read_bytes() == original
    assert not reference.with_name(f"{reference.name}.{hashlib.sha256(original).hexdigest()}.bak").exists()


def test_artifact_reference_update_supports_installed_runtime_directory(prepared, tmp_path):
    registry_path, _, _, _ = prepared
    revision = "f" * 40
    source_dir = tmp_path / "input"
    source_dir.mkdir(mode=0o700)
    application = _reference_archive(
        source_dir,
        "app.tar.gz",
        {
            "manifest.json": json.dumps(
                dict(
                    schema=1,
                    revision=revision,
                    python="3.12",
                    platform="linux-x86_64",
                    version="1.0.0+" + revision[:12],
                )
            ).encode(),
            "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"runtime layout",
        },
    )
    installer = _reference_archive(
        source_dir,
        "installer.tar.gz",
        {
            "REVISION": revision.encode(),
            "install.py": b"pass",
            "runtime_check.py": b"pass",
            "probe.py": b"pass",
        },
    )
    reference_dir = tmp_path / "onboarding-assets" / "runtime-install-aaaaaaaaaaaa"
    reference_dir.mkdir(mode=0o700, parents=True)
    reference_dir.parent.chmod(0o700)
    reference = reference_dir / "references.json"
    original = json.dumps(
        {
            "schema": "aiops-environment-onboarding/1",
            "targets": [
                {
                    "id": "host-a",
                    "host": "managed-host",
                    "user": "aiops",
                    "port": 18951,
                    "python": "3.12",
                    "application": {"path": "/old/app.tar.gz", "sha256": "b" * 64, "revision": "b" * 40},
                    "installer": {"path": "/old/installer.tar.gz", "sha256": "b" * 64, "revision": "b" * 40},
                    "configurations": [{"id": "config-a", "path": "/private/config.env", "sha256": "c" * 64}],
                }
            ],
        }
    ).encode()
    reference.write_bytes(original)
    reference.chmod(0o600)
    result = plans.update_artifact_references(
        reference,
        registry_path=registry_path,
        identity=ADMIN,
        environment="a",
        expected_sha256=hashlib.sha256(original).hexdigest(),
        application=application,
        installer=installer,
    )
    updated = json.loads(reference.read_text())["targets"][0]
    expected_dir = reference_dir.parent / "runtime-install-ffffffffffff"
    assert updated["application"]["path"] == str(expected_dir / "application-3.12.tar.gz")
    assert updated["installer"]["path"] == str(expected_dir / "installer.tar.gz")
    assert result["environment"] == "a"


@pytest.mark.parametrize(
    "failure", ["stale", "mixed-revision", "missing-target", "existing-backup", "unauthorized"]
)
def test_artifact_reference_update_rejects_invalid_changes_without_overwrite(prepared, tmp_path, failure):
    registry_path, _, _, _ = prepared
    revision = "a" * 40
    source_dir = tmp_path / "input"
    source_dir.mkdir(mode=0o700)
    app = _reference_archive(
        source_dir,
        "app-" + failure + ".tar.gz",
        {
            "manifest.json": json.dumps(
                dict(
                    schema=1,
                    revision=revision,
                    python="3.12",
                    platform="linux-x86_64",
                    version="1.0.0+" + revision[:12],
                )
            ).encode(),
            "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"opaque wheel",
        },
    )
    install_revision = "b" * 40 if failure == "mixed-revision" else revision
    installer = _reference_archive(
        source_dir,
        "installer-" + failure + ".tar.gz",
        {
            "REVISION": install_revision.encode(),
            "install.py": b"pass",
            "runtime_check.py": b"pass",
            "probe.py": b"pass",
        },
    )
    reference_dir = tmp_path / "onboarding-assets"
    reference_dir.mkdir(mode=0o700)
    reference = reference_dir / "references.json"
    original = json.dumps(
        {
            "schema": "aiops-environment-onboarding/1",
            "targets": [
                {
                    "id": "host-a",
                    "host": "managed-host",
                    "user": "aiops",
                    "port": 18951,
                    "python": "3.12",
                    "application": app,
                    "installer": installer,
                    "configurations": [{"id": "config-a", "path": "/private/config.env", "sha256": "c" * 64}],
                }
            ],
        }
    ).encode()
    reference.write_bytes(original)
    reference.chmod(0o600)
    backup = reference.with_name(f"{reference.name}.{hashlib.sha256(original).hexdigest()}.bak")
    if failure == "existing-backup":
        backup.write_text("keep")
    environment = "missing" if failure == "missing-target" else "a"
    expected = "0" * 64 if failure == "stale" else hashlib.sha256(original).hexdigest()
    expected_error = registry.RegistryError if failure == "unauthorized" else plans.PlanRefused
    with pytest.raises(expected_error):
        plans.update_artifact_references(
            reference,
            registry_path=registry_path,
            identity=("other", "platform", "admin") if failure == "unauthorized" else ADMIN,
            environment=environment,
            expected_sha256=expected,
            application=app,
            installer=installer,
        )
    assert reference.read_bytes() == original
    if failure == "existing-backup":
        assert backup.read_text() == "keep"
    assert not (reference_dir / revision).exists()


def test_reference_update_cli_only_uses_configured_reference_path(prepared, tmp_path, monkeypatch):
    from types import SimpleNamespace

    registry_path, _, reference, _ = prepared
    from aiops_diagnostics import admin_cli

    monkeypatch.setattr(
        admin_cli,
        "_settings_with_config_file",
        lambda _: SimpleNamespace(
            environment_onboarding_file=str(reference),
            environment_management_registry_file="",
            environment_registry_file=str(registry_path),
        ),
    )
    cli = CliRunner().invoke(
        admin_app,
        [
            "environment-artifacts",
            "--registry",
            str(tmp_path / "other-registry.json"),
            "--references",
            str(tmp_path / "other.json"),
            "--file",
            str(reference),
        ],
    )
    assert cli.exit_code == 1 and json.loads(cli.output)["error"] == "ENVIRONMENT_REFERENCES_UPDATE_REFUSED"
    configured = CliRunner().invoke(
        admin_app,
        [
            "environment-artifacts",
            "--registry",
            str(registry_path),
            "--references",
            str(reference),
            "--file",
            str(reference),
        ],
    )
    assert (
        configured.exit_code == 1
        and json.loads(configured.output)["error"] == "ENVIRONMENT_REFERENCES_UPDATE_REFUSED"
    )


@pytest.mark.parametrize("include_configuration", [False, True])
def test_artifact_reference_cli_accepts_existing_and_optional_configuration(
    prepared, tmp_path, monkeypatch, include_configuration
):
    from types import SimpleNamespace

    registry_path, _, reference, files = prepared
    from aiops_diagnostics import admin_cli

    monkeypatch.setattr(
        admin_cli,
        "_settings_with_config_file",
        lambda _: SimpleNamespace(
            environment_onboarding_file=str(reference),
            environment_management_registry_file=str(registry_path),
            environment_registry_file="",
        ),
    )
    received = {}
    monkeypatch.setattr(
        plans,
        "update_artifact_references",
        lambda path, **kwargs: received.update(kwargs) or {"environment": "a"},
    )
    data = {
        "identity": ADMIN,
        "environment": "a",
        "expected_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "application": files["application"],
        "installer": files["installer"],
    }
    if include_configuration:
        data["configuration"] = files["configuration"]
    request = write_private_text(tmp_path / "configuration-update.json", json.dumps(data))
    result = CliRunner().invoke(
        admin_app,
        [
            "environment-artifacts",
            "--registry",
            str(registry_path),
            "--references",
            str(reference),
            "--file",
            str(request),
        ],
    )
    assert result.exit_code == 0 and json.loads(result.stdout) == {"environment": "a"}
    assert received["configuration"] == (files["configuration"] if include_configuration else None)
    configuration = CliRunner().invoke(
        admin_app,
        [
            "environment-artifacts",
            "--registry",
            str(registry_path),
            "--references",
            str(reference),
            "--file",
            str(reference),
        ],
    )
    assert (
        configuration.exit_code == 1
        and json.loads(configuration.output)["error"] == "ENVIRONMENT_REFERENCES_UPDATE_REFUSED"
    )


def test_reference_update_restores_old_file_when_directory_sync_fails(prepared, tmp_path, monkeypatch):
    registry_path, _, _, _ = prepared
    revision = "d" * 40
    source_dir = tmp_path / "input"
    source_dir.mkdir(mode=0o700)
    application = _reference_archive(
        source_dir,
        "app.tar.gz",
        {
            "manifest.json": json.dumps(
                dict(
                    schema=1,
                    revision=revision,
                    python="3.12",
                    platform="linux-x86_64",
                    version="1.0.0+" + revision[:12],
                )
            ).encode(),
            "wheelhouse/aiops_diagnostics-1.0.0-py3-none-any.whl": b"opaque wheel",
        },
    )
    installer = _reference_archive(
        source_dir,
        "installer.tar.gz",
        {
            "REVISION": revision.encode(),
            "install.py": b"pass",
            "runtime_check.py": b"pass",
            "probe.py": b"pass",
        },
    )
    reference_dir = tmp_path / "onboarding-assets"
    reference_dir.mkdir(mode=0o700)
    reference = reference_dir / "references.json"
    original = json.dumps(
        {
            "schema": "aiops-environment-onboarding/1",
            "targets": [
                {
                    "id": "host-a",
                    "host": "managed-host",
                    "user": "aiops",
                    "port": 18951,
                    "python": "3.12",
                    "application": {"path": "/old/app.tar.gz", "sha256": "b" * 64, "revision": "b" * 40},
                    "installer": {"path": "/old/installer.tar.gz", "sha256": "b" * 64, "revision": "b" * 40},
                    "configurations": [{"id": "config-a", "path": "/private/config.env", "sha256": "c" * 64}],
                }
            ],
        }
    ).encode()
    reference.write_bytes(original)
    reference.chmod(0o600)
    original_fsync = os.fsync
    failed = False

    references_directory = str(reference_dir)

    def fail_one_directory_sync(fd):
        nonlocal failed
        if (
            not failed
            and os.path.isdir(f"/proc/self/fd/{fd}")
            and os.path.realpath(f"/proc/self/fd/{fd}") == references_directory
        ):
            failed = True
            raise OSError("injected directory sync failure")
        original_fsync(fd)

    monkeypatch.setattr(plans.os, "fsync", fail_one_directory_sync)
    with pytest.raises(OSError, match="directory sync failure"):
        plans.update_artifact_references(
            reference,
            registry_path=registry_path,
            identity=ADMIN,
            environment="a",
            expected_sha256=hashlib.sha256(original).hexdigest(),
            application=application,
            installer=installer,
        )
    assert failed and reference.read_bytes() == original
    assert not reference.with_name(f"{reference.name}.{hashlib.sha256(original).hexdigest()}.bak").exists()
