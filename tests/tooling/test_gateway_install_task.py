"""D37：原安装器临时实例上的意图持久化、丢回执与只读对账。"""

import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import tarfile
import time
from pathlib import Path

import pytest

from tooling.test_gateway_install import apply_runtime, candidate_config, host, runtime_bundle  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fixed_task", ROOT / "ops/environments/gateway/task.py")
task = importlib.util.module_from_spec(spec)
sys.modules["install"] = __import__("tooling.test_gateway_install", fromlist=["install"]).install
spec.loader.exec_module(task)


@pytest.fixture
def task_state(host, monkeypatch):  # noqa: F811
    inst, application, sha, *_ = host
    application.chmod(0o600)
    runtime = runtime_bundle(host)
    apply_runtime(host, runtime)
    pair = candidate_config(host)
    files = {"production.env": pair[0].read_bytes(), "environment.json": pair[1].read_bytes()}
    files["manifest.json"] = json.dumps(
        {
            "schema": "aiops-environment-candidate/1",
            "scope": "bootstrap-configuration-only",
            "timezone": "Asia/Shanghai",
            "plan_id": "plan_" + "a" * 32,
            "plan_digest": "b" * 64,
            "draft_revision": 1,
            "registry_revision": 1,
            "executable": False,
            "projection_sha256": hashlib.sha256(
                json.dumps(
                    {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}, sort_keys=True
                ).encode()
            ).hexdigest(),
            "projected_config_sha256": hashlib.sha256(files["production.env"]).hexdigest(),
            "projected_registry_sha256": hashlib.sha256(files["environment.json"]).hexdigest(),
        }
    ).encode()
    candidate = inst.root.parent / "candidate.tar"
    with tarfile.open(candidate, "w") as bundle:
        for name, body in files.items():
            m = tarfile.TarInfo(name)
            m.size = len(body)
            m.mode = 0o600
            bundle.addfile(m, io.BytesIO(body))
    candidate.chmod(0o600)
    previous = inst.read_record("installed.json")
    inst.revision = previous["revision"]
    request = {
        "id": "task_" + "a" * 32,
        "environment": "test",
        "expires_at": time.time() + 900,
        "expected": {
            k: previous[k] for k in ("artifact_sha256", "config_sha256", "registry_sha256", "runtime_sha256")
        },
        "archives": {
            k: {"path": str(v), "sha256": task.install.digest(v)}
            for k, v in {"application": application, "candidate": candidate, "runtime": runtime}.items()
        },
    }
    monkeypatch.setattr(
        inst,
        "site_observation",
        lambda: ("synthetic", {"checks": [{"id": "configuration", "state": "passed"}]}, {}),
    )
    # candidate_files oracle仍用原成员/摘要合同。
    return inst, request, host


def test_lost_response_observes_original_intent_without_second_apply(task_state, monkeypatch):
    inst, request, _ = task_state
    original = inst.apply
    called = []

    def lost(*args, **kwargs):
        called.append(True)
        original(*args, **kwargs)
        raise OSError("reply lost")

    monkeypatch.setattr(inst, "apply", lost)
    with pytest.raises(OSError, match="reply lost"):
        task.execute(inst, request)
    after = inst.read_record("installed.json")
    result = task.execute(inst, request, observe_only=True)
    assert result["state"] == "observed" and len(called) == 1
    assert result["business"] == "unverified" and result["activation"] == "not_executed"
    assert task.execute(inst, request)["state"] == "observed" and len(called) == 1
    assert inst.read_record("installed.json") == after


@pytest.mark.parametrize("condition", ["expired", "observe", "baseline", "pending"])
def test_no_unapproved_reentry_or_baseline_write(task_state, monkeypatch, condition):
    inst, request, _ = task_state
    calls = []
    monkeypatch.setattr(inst, "apply", lambda *a, **kw: calls.append(a))
    if condition == "expired":
        request["expires_at"] = 0
    if condition == "baseline":
        request["expected"]["config_sha256"] = "f" * 64
    if condition == "pending":
        inst.record("pending.json", {"phase": "switching"})
    if condition == "baseline":
        with pytest.raises(ValueError, match="baseline"):
            task.execute(inst, request)
    else:
        result = task.execute(inst, request, observe_only=condition == "observe")
        assert result["state"] in ("blocked", "reconcile")
    assert calls == []


def test_execute_passes_managed_interpreter_and_apply_revalidates(task_state, monkeypatch):
    inst, request, fixture = task_state
    minor = inst.read_record("installed.json")["python"]
    selected = task.probe.managed_python(minor, run=inst.command)
    resolved = os.path.realpath(selected)
    if task.probe.re.fullmatch(r"python3(?:\.(?:11|12|13))?", os.path.basename(resolved)):
        selected = resolved
    monkeypatch.setattr(task.probe, "managed_python", lambda *a, **kw: selected)
    count = len(fixture[4])
    assert task.execute(inst, request)["state"] == "observed"
    minor_probe = next(c for c in reversed(fixture[4][count:]) if "-c" in c)
    # 小版本探测只发向apply内system_python核验后的解析目标。
    assert os.path.realpath(selected) in minor_probe
    forged = Path(inst.root.parent / "forged-python3.12")
    forged.write_text("#!/bin/sh\n")
    forged.chmod(0o755)
    monkeypatch.setattr(task.probe, "managed_python", lambda *a, **kw: str(forged))
    with pytest.raises(ValueError, match="system Python"):
        task.execute(inst, {**request, "id": "task_" + "b" * 32})


@pytest.fixture
def cold_state(host, tmp_path):  # noqa: F811
    inst, application, sha, _config, _calls, _active = host
    application.chmod(0o600)
    pair = candidate_config(host)
    files = {"production.env": pair[0].read_bytes(), "environment.json": pair[1].read_bytes()}
    files["manifest.json"] = json.dumps(
        {
            "schema": "aiops-environment-candidate/1",
            "scope": "bootstrap-configuration-only",
            "timezone": "Asia/Shanghai",
            "plan_id": "plan_" + "a" * 32,
            "plan_digest": "b" * 64,
            "draft_revision": 1,
            "registry_revision": 1,
            "executable": False,
            "projection_sha256": hashlib.sha256(
                json.dumps(
                    {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}, sort_keys=True
                ).encode()
            ).hexdigest(),
            "projected_config_sha256": hashlib.sha256(files["production.env"]).hexdigest(),
            "projected_registry_sha256": hashlib.sha256(files["environment.json"]).hexdigest(),
        }
    ).encode()
    candidate = inst.root.parent / "cold-candidate.tar"
    with tarfile.open(candidate, "w") as bundle:
        for name, body in files.items():
            m = tarfile.TarInfo(name)
            m.size = len(body)
            m.mode = 0o600
            bundle.addfile(m, io.BytesIO(body))
    candidate.chmod(0o600)
    inst.revision = "a" * 40
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    spec = {
        "id": "task_" + "c" * 32,
        "environment": "test",
        "expires_at": time.time() + 900,
        "python": "3.12",
        "archives": {
            "application": {"path": str(application), "sha256": task.install.digest(application)},
            "candidate": {"path": str(candidate), "sha256": task.install.digest(candidate)},
        },
    }
    return inst, spec, staging, host


def test_cold_install_records_intent_then_installs_via_original_apply(cold_state):
    inst, spec, staging, _ = cold_state
    result = task.execute_cold(inst, spec, staging=staging)
    assert result["state"] == "observed" and result["installation"] == "installed"
    assert result["scope"] == "site-cold-installation-only" and result["executable"] is False
    record = json.loads((staging / "cold-intent.json").read_text())
    assert record["spec"] == spec and record["outcome"] == "installed"
    installed = inst.read_record("installed.json")
    assert installed["config_sha256"] == result["config_sha256"]
    assert installed["registry_sha256"] == result["registry_sha256"]
    # 冷装只装停用目录与公司连接：账本没有运行工件摘要，env也没有运行指针。
    assert "runtime_sha256" not in installed
    assert "OPERATOR_PROFILES_FILE" not in (inst.config / "production.env").read_text()


def test_cold_install_never_repeats_apply_on_same_task(cold_state, monkeypatch):
    inst, spec, staging, _ = cold_state
    assert task.execute_cold(inst, spec, staging=staging)["installation"] == "installed"
    calls = []
    monkeypatch.setattr(inst, "apply", lambda *a, **kw: calls.append(a))
    again = task.execute_cold(inst, spec, staging=staging)
    assert again["state"] == "observed" and again["installation"] == "installed"
    assert calls == []


def test_cold_install_without_intent_skips_existing_ledger(cold_state, monkeypatch):
    inst, spec, staging, _ = cold_state
    task.execute_cold(inst, spec, staging=staging)
    (staging / "cold-intent.json").unlink()
    calls = []
    monkeypatch.setattr(inst, "apply", lambda *a, **kw: calls.append(a))
    result = task.execute_cold(inst, spec, staging=staging)
    assert result["state"] == "observed" and result["installation"] == "already_installed"
    assert calls == []


@pytest.mark.parametrize(
    "obstacle",
    ["state", "config", "backups", "backups-dir", "unit", "unit-loaded", "residue", "root-without-ledger"],
)
def test_cold_install_reconciles_unknown_assets(cold_state, obstacle, monkeypatch):
    inst, spec, staging, fixture = cold_state
    if obstacle == "unit-loaded":
        fixture[5].add(inst.name)
    elif obstacle == "residue":
        (inst.root.parent / (inst.name + "-preparing-zz")).mkdir()
    elif obstacle == "root-without-ledger":
        inst.root.mkdir()
    elif obstacle == "backups-dir":
        # 声明backups根里已有内容=旧业务资产，不当空机也不收养。
        inst.backups.mkdir(mode=0o700)
        (inst.backups / "foreign.dump").write_text("foreign")
    else:
        getattr(inst, obstacle).parent.mkdir(parents=True, exist_ok=True)
        getattr(inst, obstacle).write_text("foreign")
    calls = []
    monkeypatch.setattr(inst, "apply", lambda *a, **kw: calls.append(a))
    result = task.execute_cold(inst, spec, staging=staging)
    assert result["state"] == "reconcile"
    assert result["error_code"] == "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED"
    assert calls == [] and not (staging / "cold-intent.json").exists()


def test_cold_install_tolerates_declared_empty_backups_root(cold_state):
    inst, spec, staging, _ = cold_state
    # 受管staging可预建声明backups根；可信私有空目录不改变真空事实。
    inst.backups.mkdir(mode=0o700)
    result = task.execute_cold(inst, spec, staging=staging)
    assert result["state"] == "observed" and result["installation"] == "installed"
    assert inst.backups.is_dir() and not any(inst.backups.iterdir())


def test_cold_install_refuses_occupied_port(cold_state, monkeypatch):
    inst, spec, staging, _ = cold_state
    calls = []
    monkeypatch.setattr(inst, "apply", lambda *a, **kw: calls.append(a))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", inst.port))
        sock.listen(1)
        result = task.execute_cold(inst, spec, staging=staging)
    assert result["state"] == "blocked"
    assert result["error_code"] == "ENVIRONMENT_COLD_INSTALLATION_REFUSED"
    assert calls == []


@pytest.mark.parametrize("mode", ["expired", "observe"])
def test_cold_install_unattempted_empty_host_is_not_installed(cold_state, mode, monkeypatch):
    inst, spec, staging, _ = cold_state
    calls = []
    monkeypatch.setattr(inst, "apply", lambda *a, **kw: calls.append(a))
    if mode == "expired":
        spec["expires_at"] = 0
    result = task.execute_cold(inst, spec, staging=staging, observe_only=mode == "observe")
    assert result["state"] == "blocked" and result["installation"] == "not_installed"
    assert result["error_code"] == "ENVIRONMENT_INSTALLATION_OBSERVATION_REQUIRED"
    assert calls == []


def test_cold_lost_reply_reenters_as_observation_only(cold_state, monkeypatch):
    inst, spec, staging, _ = cold_state
    original = inst.apply

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("reply lost")

    monkeypatch.setattr(inst, "apply", lost)
    with pytest.raises(OSError, match="reply lost"):
        task.execute_cold(inst, spec, staging=staging)
    result = task.execute_cold(inst, spec, staging=staging, observe_only=True)
    assert result["state"] == "observed" and result["installation"] == "installed"


def test_cold_intent_without_takeover_reconciles(cold_state):
    inst, spec, staging, _ = cold_state
    intent = staging / "cold-intent.json"
    intent.write_text(json.dumps({"spec": spec, "desired": {"config_sha256": "x" * 64}}))
    intent.chmod(0o600)
    result = task.execute_cold(inst, spec, staging=staging)
    assert result["state"] == "reconcile"
    assert result["error_code"] == "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED"


def test_cold_foreign_intent_is_unconfirmed(cold_state):
    inst, spec, staging, _ = cold_state
    intent = staging / "cold-intent.json"
    intent.write_text(json.dumps({"spec": {**spec, "id": "task_" + "d" * 32}}))
    intent.chmod(0o600)
    result = task.execute_cold(inst, spec, staging=staging)
    assert result["state"] == "blocked" and result["installation"] == "unconfirmed"


def test_cold_install_requires_declared_minor(cold_state):
    inst, spec, staging, _ = cold_state
    with pytest.raises(ValueError):
        task.execute_cold(inst, {**spec, "python": "3.9"}, staging=staging)
    # 声明3.13而目标只有3.12：包manifest先拒绝，意图不落盘。
    with pytest.raises(ValueError):
        task.execute_cold(inst, {**spec, "python": "3.13"}, staging=staging)
    assert not (staging / "cold-intent.json").exists()


def test_registration_lost_receipt_reenters_as_observation_only(task_state, monkeypatch):
    inst, previous, _ = task_state
    installed = inst.read_record("installed.json")
    registration = json.loads((inst.runtime_path(installed) / "registrations.json").read_text())
    spec = {k: previous[k] for k in ("id", "environment", "expires_at")}
    spec.update(
        expected={"revision": installed["revision"], **previous["expected"]},
        tenant_id=registration["tenant_id"],
    )
    calls = []

    def register(*, observe_only=False):
        calls.append(observe_only)
        if not observe_only:
            raise OSError("lost reply after commit")
        return dict(
            environment=inst.environment,
            tenant_id=registration["tenant_id"],
            runtime_sha256=installed["runtime_sha256"],
            result="unchanged",
            scope="inactive-runtime-registration-only",
            activated=False,
            business_acceptance=False,
        )

    monkeypatch.setattr(inst, "register_runtime", register)
    with pytest.raises(OSError):
        task.register(inst, spec)
    assert task.register(inst, spec)["state"] == "observed"
    assert calls == [False, True]
    assert not (inst.root / "pending.json").exists()
    with pytest.raises(ValueError, match="baseline changed"):
        task.register(inst, {**spec, "expected": {**spec["expected"], "runtime_sha256": "f" * 64}})
    assert calls == [False, True]
