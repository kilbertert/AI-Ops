"""D43原安装器准入最后与恢复：真实私有文件/账本，主机命令用既有替身。"""

# ruff: noqa: F811 - 复用原受管安装器fixture。
import base64
import importlib.util
import json
import time

import pytest

from tooling.test_gateway_install import ROOT, host  # noqa: F401
from tooling.test_gateway_install_task import task_state  # noqa: F401

SPEC = importlib.util.spec_from_file_location(
    "publication_install", ROOT / "ops/environments/gateway/publication.py"
)
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


@pytest.fixture
def staged_host(task_state, monkeypatch):
    inst, _, _ = task_state
    old = inst.read_record("installed.json")
    payload = ["test", "same", "workspace", "consumer-app", "kb", "now"]
    kid = "kdx_" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    document = {
        "schema": "aiops-environment-release/1",
        "environment": "test",
        "tenant_id": "same",
        "id": "task_" + "d" * 32,
        "expires_at": time.time() + 900,
        "installed": {**old, "source_revision": old["revision"]},
        "apps": {
            "operator": {"app_id": "operator"},
            "consumer": {"app_id": "consumer-app", "key": "synthetic-app-key"},
        },
        "knowledge": {"base_url": "https://knowledge.example", "capabilities": [{"knowledge_id": kid}]},
    }
    calls = []
    local = inst.state / "gateway/environment-publications" / (document["id"] + ".json")

    def invoke(inst, doc, action):
        calls.append(action)
        if action == "prepare":
            local.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
            local.write_text("{}")
            return {"versions": {"operator": 1, "consumer": 1}}
        if action in {"publish", "observe"}:
            return {"phase": "published", "routes": {"operator": 1, "consumer": 1}}
        return {"phase": "restored"}

    monkeypatch.setattr(publication, "invoke", invoke)
    monkeypatch.setattr(publication, "refresh_verification", lambda *a: a[-1])
    return inst, document, calls


def verification(inst, doc):
    result = {
        "state": "passed",
        "operation": doc["id"],
        "operator_runs": ["order", "offline"],
        "observed_at": time.time(),
    }
    file = inst.state / "gateway/environment-publications" / (doc["id"] + ".verification.json")
    file.write_text(json.dumps(result))
    file.chmod(0o600)
    return result


def test_stage_activation_restore_keeps_monotonic_directory_and_old_config(staged_host):
    inst, doc, calls = staged_host
    old = (inst.config / "production.env").read_bytes()
    staged = publication.stage(inst, doc)
    assert staged["phase"] == "staged" and not staged["activated"]
    body = json.loads((inst.config / "registry/environment.json").read_text())
    assert not body["environments"][0]["tenants"][0]["enabled"]
    assert publication.stage(inst, doc) == staged and calls == ["prepare"]
    proof = verification(inst, doc)
    active = publication.activate(inst, doc, proof)
    assert active["activated"]
    assert json.loads((inst.config / "registry/environment.json").read_text())["revision"] == 2
    assert publication.activate(inst, doc, proof)["activated"]
    restored = publication.recover(inst, doc["id"])
    assert restored["phase"] == "restored" and not restored["activated"]
    assert json.loads((inst.config / "registry/environment.json").read_text())["revision"] == 3
    assert (inst.config / "production.env").read_bytes() == old
    assert not (inst.config / "knowledge/knowledge-delegation.json").exists()
    assert not (inst.state / "keys/consumer-app.key").exists()
    assert not (inst.root / "pending.json").exists()
    assert publication.recover(inst, doc["id"]) == restored


@pytest.mark.parametrize("failure", ["config-write", "version-import", "routes", "registry", "installed"])
def test_partial_side_effects_recover_without_overwriting_unknown_changes(staged_host, monkeypatch, failure):
    inst, doc, calls = staged_host
    snapshot, invoke, registry, record = (
        inst.config_snapshot_bytes,
        publication.invoke,
        inst.registry_snapshot_bytes,
        inst.record,
    )

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic crash")

    if failure == "config-write":
        monkeypatch.setattr(inst, "config_snapshot_bytes", fail)
    elif failure == "version-import":
        monkeypatch.setattr(publication, "invoke", fail)
    if failure in {"config-write", "version-import"}:
        with pytest.raises(RuntimeError):
            publication.stage(inst, doc)
    else:
        publication.stage(inst, doc)
        proof = verification(inst, doc)
        if failure == "routes":
            monkeypatch.setattr(publication, "invoke", fail)
        elif failure == "registry":
            monkeypatch.setattr(inst, "registry_snapshot_bytes", fail)
        else:

            def fail_installed(name, value):
                if name == "installed.json":
                    fail()
                record(name, value)

            monkeypatch.setattr(inst, "record", fail_installed)
        with pytest.raises(RuntimeError):
            publication.activate(inst, doc, proof)
    monkeypatch.setattr(inst, "config_snapshot_bytes", snapshot)
    monkeypatch.setattr(publication, "invoke", invoke)
    monkeypatch.setattr(inst, "registry_snapshot_bytes", registry)
    monkeypatch.setattr(inst, "record", record)
    assert (inst.root / "pending.json").exists()
    assert publication.stage(inst, doc, observe_only=True)["state"] == "reconcile"
    assert publication.recover(inst, doc["id"])["phase"] == "restored"
    assert not (inst.root / "pending.json").exists()


def test_activate_consumes_refresh_result_for_stale_but_identical_evidence(staged_host, monkeypatch):
    inst, doc, calls = staged_host
    publication.stage(inst, doc)
    proof = verification(inst, doc)
    proof["observed_at"] = time.time() - 400
    seen = []

    def refresh(inst_, doc_, value):
        seen.append(value)
        return {**value, "observed_at": time.time()}

    monkeypatch.setattr(publication, "refresh_verification", refresh)
    assert publication.activate(inst, doc, proof)["activated"]
    assert seen == [proof]


def test_activate_rejects_unconfirmed_refresh_or_evidence_drift(staged_host, monkeypatch):
    inst, doc, calls = staged_host
    publication.stage(inst, doc)
    proof = verification(inst, doc)
    monkeypatch.setattr(publication, "refresh_verification", lambda *a: {**a[-1], "state": "expired"})
    with pytest.raises(ValueError, match="verification required"):
        publication.activate(inst, doc, proof)
    monkeypatch.setattr(publication, "refresh_verification", lambda *a: a[-1])
    with pytest.raises(ValueError, match="receipt differs"):
        publication.activate(inst, doc, {**proof, "run_id": "other-run"})


def test_activate_rejects_expired_document_before_refresh(staged_host, monkeypatch):
    inst, doc, calls = staged_host
    doc["expires_at"] = time.time() + 0.2
    publication.stage(inst, doc)
    proof = verification(inst, doc)
    time.sleep(0.3)
    fired = []
    monkeypatch.setattr(publication, "refresh_verification", lambda *a: fired.append(a) or a[-1])
    with pytest.raises(ValueError, match="verification required"):
        publication.activate(inst, doc, proof)
    assert not fired


def test_recovery_refuses_third_party_file_or_directory_change(staged_host):
    inst, doc, _ = staged_host
    publication.stage(inst, doc)
    config = inst.config / "production.env"
    config.write_text(config.read_text() + 'AIOPS_OWNER_SETTING="other-owner"\n')
    before = config.read_bytes()
    with pytest.raises(ValueError, match="file drift"):
        publication.recover(inst, doc["id"])
    assert config.read_bytes() == before
    assert not (inst.root / "pending.json").exists()


def test_next_install_preserves_published_knowledge_and_runtime(staged_host, task_state):
    from tooling.test_gateway_install import next_bundle

    inst, doc, _ = staged_host
    publication.stage(inst, doc)
    publication.activate(inst, doc, verification(inst, doc))
    current = inst.read_record("installed.json")
    active_config = inst.config / "production.env"
    active_directory = inst.config / "registry/environment.json"
    knowledge_before = (inst.config / "knowledge/knowledge-delegation.json").read_bytes()
    runtime = inst.runtime_path(current) / "runtime.tar"
    _, application, sha, *_ = next_bundle(task_state[2])
    inst.revision = "b" * 40
    result = inst.apply(
        application,
        sha,
        "/usr/bin/python3",
        active_config,
        current["config_sha256"],
        registry_file=active_directory,
        expected_registry_sha=current["registry_sha256"],
        runtime_archive=runtime,
        runtime_sha=current["runtime_sha256"],
        expected_runtime_sha=current["runtime_sha256"],
    )
    assert result["result"] == "installed"
    receipt = inst.read_record("installed.json")
    assert receipt["knowledge_sha256"] == current["knowledge_sha256"]
    assert (inst.config / "knowledge/knowledge-delegation.json").read_bytes() == knowledge_before
    assert (
        inst.root / "current/candidate-root/knowledge/knowledge-delegation.json"
    ).read_bytes() == knowledge_before
    assert json.loads(active_directory.read_text())["environments"][0]["tenants"][0]["enabled"]
