"""D37：原安装器临时实例上的意图持久化、丢回执与只读对账。"""

import hashlib
import importlib.util
import io
import json
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
