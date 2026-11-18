"""候选包完整性与正式路径；目录的私有父目录必须能通过现役读取规则。"""

import hashlib
import io
import json
import tarfile

import pytest

from aiops_diagnostics.common.private_files import write_private_text
from aiops_diagnostics.gateway.environment_artifacts import candidate_files
from aiops_diagnostics.gateway.environment_projection import render, summary, validate_candidate
from aiops_diagnostics.gateway.environment_registry import load


def candidate(tmp_path):
    template = write_private_text(tmp_path / "input.env", "AIOPS_MYSQL_PASSWORD=synthetic\n")
    draft = dict(
        id="a",
        name="候选",
        enabled=True,
        gateway_url="https://a.test",
        cpms_base_url="https://a.test/cpms",
        company_gateway_url="https://a.test",
        deployment_target="host",
        credential_ref="config",
        timezone="Asia/Shanghai",
        tenants=[dict(id="same", name="租户", enabled=True)],
    )
    files = render(
        draft, template, hashlib.sha256(template.read_bytes()).hexdigest(), "2026-11-18T00:00:00+00:00"
    )
    manifest = dict(
        schema="aiops-environment-candidate/1",
        scope="bootstrap-configuration-only",
        timezone="Asia/Shanghai",
        plan_id="plan_" + "a" * 32,
        plan_digest="b" * 64,
        draft_revision=1,
        registry_revision=1,
        executable=False,
        **summary(files),
    )
    return {**files, "manifest.json": json.dumps(manifest).encode()}


def pack(tmp_path, files, extra=None):
    target = tmp_path / "candidate.tar.gz"
    with tarfile.open(target, "w:gz") as bundle:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            bundle.addfile(member, io.BytesIO(raw))
        if extra is not None:
            bundle.addfile(extra)
    target.chmod(0o600)
    return target, hashlib.sha256(target.read_bytes()).hexdigest()


def test_candidate_bundle_matches_files_and_runtime_contract(tmp_path):
    files = candidate(tmp_path)
    path, digest = pack(tmp_path, files)
    verified, manifest = candidate_files(path, digest, "a")
    assert verified == files and manifest["executable"] is False
    config = write_private_text(tmp_path / "production.env", files["production.env"].decode())
    directory = write_private_text(tmp_path / "registry/environment.json", files["environment.json"].decode())
    validate_candidate(config, directory, "a")
    assert load(directory)["environments"][0]["enabled"] is False
    assert "/registry/environment.json" in config.read_text()
    with pytest.raises(ValueError):
        candidate_files(path, digest, "b")
    with pytest.raises(ValueError):
        candidate_files(path, "0" * 64, "a")


@pytest.mark.parametrize("fault", ["traversal", "link", "duplicate", "changed-env", "enabled"])
def test_invalid_candidate_refused_before_install(tmp_path, fault):
    files = candidate(tmp_path)
    extra = None
    if fault in {"traversal", "link", "duplicate"}:
        extra = tarfile.TarInfo("../escape" if fault == "traversal" else "production.env")
        if fault == "link":
            extra.type, extra.linkname = tarfile.SYMTYPE, "/etc/shadow"
    elif fault == "changed-env":
        files["production.env"] += b"AIOPS_HOME=/unapproved\n"
    else:
        directory = json.loads(files["environment.json"])
        directory["environments"][0]["enabled"] = True
        files["environment.json"] = json.dumps(directory).encode()
        manifest = json.loads(files["manifest.json"])
        manifest.update(summary({k: v for k, v in files.items() if k != "manifest.json"}))
        files["manifest.json"] = json.dumps(manifest).encode()
    path, digest = pack(tmp_path, files, extra)
    with pytest.raises(ValueError):
        candidate_files(path, digest, "a")
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize(
    "addition",
    [
        "AIOPS_GATEWAY_CHAT_NODE_TARGETS_FILE=/active",
        "AIOPS_MYSQL_PASSWORD=${SOURCE}",
        "AIOPS_MYSQL_PASSWORD=x\nAIOPS_MYSQL_PASSWORD=y",
    ],
)
def test_matching_archive_does_not_bypass_candidate_semantic_validation(tmp_path, addition):
    files = candidate(tmp_path)
    files["production.env"] += addition.encode() + b"\n"
    manifest = json.loads(files["manifest.json"])
    manifest.update(summary({k: v for k, v in files.items() if k != "manifest.json"}))
    files["manifest.json"] = json.dumps(manifest).encode()
    path, digest = pack(tmp_path, files)
    verified, _ = candidate_files(path, digest, "a")
    config = write_private_text(tmp_path / "production.env", verified["production.env"].decode())
    directory = write_private_text(
        tmp_path / "registry/environment.json", verified["environment.json"].decode()
    )
    with pytest.raises(ValueError):
        validate_candidate(config, directory, "a")
