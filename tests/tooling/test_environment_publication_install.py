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


def _two_tenant_setup(inst, doc_a):
    """在目标停用目录补充第二个租户并返回(doc_a, doc_b)两份同环境发行文档。"""
    reg_path = inst.config / "registry/environment.json"
    body = json.loads(reg_path.read_text())
    body["environments"][0]["tenants"].append({"id": "peer", "enabled": False})
    raw_reg = json.dumps(body, sort_keys=True) + "\n"
    inst.registry_snapshot_bytes(raw_reg.encode(), reg_path)
    old = inst.read_record("installed.json")
    old["registry_sha256"] = publication.install.digest(reg_path)
    inst.record("installed.json", old)
    doc_a = {**doc_a, "installed": {**old, "source_revision": old["revision"]}}
    payload_b = ["test", "peer", "workspace-b", "consumer-app-b", "kb-b", "now"]
    kid_b = "kdx_" + base64.urlsafe_b64encode(json.dumps(payload_b).encode()).decode().rstrip("=")
    doc_b = {
        "schema": "aiops-environment-release/1",
        "environment": "test",
        "tenant_id": "peer",
        "id": "task_" + "e" * 32,
        "expires_at": time.time() + 900,
        "installed": None,  # 激活 A 后由调用方按最新 installed.json 填充
        "apps": {
            "operator": {"app_id": "operator-b"},
            "consumer": {"app_id": "consumer-app-b", "key": "synthetic-app-key-b"},
        },
        "knowledge": {
            "schema": "aiops-knowledge-delegation/1",
            "base_url": "https://knowledge.example",
            "capabilities": [{"knowledge_id": kid_b, "token": kid_b + "." + "b" * 64}],
        },
    }
    doc_a["knowledge"] = {
        "schema": "aiops-knowledge-delegation/1",
        "base_url": "https://knowledge.example",
        "capabilities": [
            {
                "knowledge_id": doc_a["knowledge"]["capabilities"][0]["knowledge_id"],
                "token": doc_a["knowledge"]["capabilities"][0]["knowledge_id"] + "." + "a" * 64,
            }
        ],
    }
    return doc_a, doc_b


def test_multi_tenant_non_lifo_restore_preserves_peer_without_restart_and_never_resurrects(
    staged_host, monkeypatch
):
    inst, doc_a, _ = staged_host
    doc_a, doc_b = _two_tenant_setup(inst, doc_a)
    original_prod = (inst.config / "production.env").read_bytes()
    stops = []
    real_stop = inst.stop
    monkeypatch.setattr(inst, "stop", lambda name: stops.append(name) or real_stop(name))

    # 1. 先后激活租户 A 与租户 B
    publication.stage(inst, doc_a)
    publication.activate(inst, doc_a, verification(inst, doc_a))
    after_a = inst.read_record("installed.json")
    doc_b["installed"] = {**after_a, "source_revision": after_a["revision"]}
    publication.stage(inst, doc_b)
    publication.activate(inst, doc_b, verification(inst, doc_b))
    reg_both = json.loads((inst.config / "registry/environment.json").read_text())
    assert reg_both["environments"][0]["enabled"] is True
    assert {t["id"]: t["enabled"] for t in reg_both["environments"][0]["tenants"]} == {
        "same": True,
        "peer": True,
    }
    stops.clear()

    # 2. 非 LIFO：先恢复较早发布的租户 A，租户 B 必须保持启用、零重启且知识委派与密钥完整
    prod_before_restore_a = (inst.config / "production.env").read_bytes()
    restored_a = publication.recover(inst, doc_a["id"])
    assert restored_a["phase"] == "restored" and not restored_a["activated"]
    assert stops == []
    assert (inst.config / "production.env").read_bytes() == prod_before_restore_a
    reg_after_a = json.loads((inst.config / "registry/environment.json").read_text())
    assert reg_after_a["revision"] == reg_both["revision"] + 1
    assert reg_after_a["environments"][0]["enabled"] is True
    assert {t["id"]: t["enabled"] for t in reg_after_a["environments"][0]["tenants"]} == {
        "same": False,
        "peer": True,
    }
    kb_after_a = json.loads((inst.config / "knowledge/knowledge-delegation.json").read_text())
    assert kb_after_a["capabilities"] == doc_b["knowledge"]["capabilities"]
    assert not (inst.state / "keys/consumer-app.key").exists()
    assert (inst.state / "keys/consumer-app-b.key").read_text().strip() == "synthetic-app-key-b"
    assert publication.observe(inst, doc_b)["activated"] is True
    assert publication.recover(inst, doc_a["id"]) == restored_a

    # 3. 随后恢复最后剩余的租户 B：环境收敛停用、production.env 退回无悬空委派的初始基线 P0（S1）、
    # 清理知识委派文件且绝不复活 A 的能力，两任务均可幂等重入（H3）
    restored_b = publication.recover(inst, doc_b["id"])
    assert restored_b["phase"] == "restored" and not restored_b["activated"]
    assert len(stops) == 1
    assert (inst.config / "production.env").read_bytes() == original_prod
    reg_after_b = json.loads((inst.config / "registry/environment.json").read_text())
    assert reg_after_b["revision"] == reg_after_a["revision"] + 1
    assert reg_after_b["environments"][0]["enabled"] is False
    assert {t["id"]: t["enabled"] for t in reg_after_b["environments"][0]["tenants"]} == {
        "same": False,
        "peer": False,
    }
    assert not (inst.config / "knowledge/knowledge-delegation.json").exists()
    assert not (inst.state / "keys/consumer-app-b.key").exists()
    assert "knowledge_sha256" not in inst.read_record("installed.json")
    assert not (inst.root / "pending.json").exists()
    assert publication.recover(inst, doc_a["id"])["phase"] == "restored"
    assert publication.recover(inst, doc_b["id"])["phase"] == "restored"


def test_multi_tenant_lifo_restore_reverts_production_env_when_last_tenant_restores(staged_host, monkeypatch):
    inst, doc_a, _ = staged_host
    doc_a, doc_b = _two_tenant_setup(inst, doc_a)
    original_prod = (inst.config / "production.env").read_bytes()
    stops = []
    real_stop = inst.stop
    monkeypatch.setattr(inst, "stop", lambda name: stops.append(name) or real_stop(name))

    publication.stage(inst, doc_a)
    publication.activate(inst, doc_a, verification(inst, doc_a))
    after_a = inst.read_record("installed.json")
    doc_b["installed"] = {**after_a, "source_revision": after_a["revision"]}
    publication.stage(inst, doc_b)
    publication.activate(inst, doc_b, verification(inst, doc_b))
    stops.clear()

    # LIFO：先恢复 B（A 保持启用、零重启）
    assert publication.recover(inst, doc_b["id"])["phase"] == "restored"
    assert stops == []
    reg_mid = json.loads((inst.config / "registry/environment.json").read_text())
    assert reg_mid["environments"][0]["enabled"] is True
    assert {t["id"]: t["enabled"] for t in reg_mid["environments"][0]["tenants"]} == {
        "same": True,
        "peer": False,
    }
    assert (
        json.loads((inst.config / "knowledge/knowledge-delegation.json").read_text())["capabilities"]
        == doc_a["knowledge"]["capabilities"]
    )

    # 再恢复最后剩余的 A：回退 production.env 至初始字节并清理知识委派，且已恢复 B 可幂等重入（H3）
    assert publication.recover(inst, doc_a["id"])["phase"] == "restored"
    assert len(stops) == 1
    assert (inst.config / "production.env").read_bytes() == original_prod
    assert not (inst.config / "knowledge/knowledge-delegation.json").exists()
    reg_end = json.loads((inst.config / "registry/environment.json").read_text())
    assert reg_end["environments"][0]["enabled"] is False
    assert publication.recover(inst, doc_b["id"])["phase"] == "restored"
    assert publication.observe(inst, doc_b)["phase"] == "restored"


def test_multi_tenant_restore_rejects_forged_capability_and_recovers_after_crash(staged_host, monkeypatch):
    inst, doc_a, _ = staged_host
    doc_a, doc_b = _two_tenant_setup(inst, doc_a)
    publication.stage(inst, doc_a)
    publication.activate(inst, doc_a, verification(inst, doc_a))
    after_a = inst.read_record("installed.json")
    doc_b["installed"] = {**after_a, "source_revision": after_a["revision"]}
    publication.stage(inst, doc_b)
    publication.activate(inst, doc_b, verification(inst, doc_b))

    kb_path = inst.config / "knowledge/knowledge-delegation.json"
    legit_kb = kb_path.read_bytes()
    forged_payload = ["test", "rogue", "workspace-x", "rogue-app", "kb-x", "now"]
    rogue_kid = "kdx_" + base64.urlsafe_b64encode(json.dumps(forged_payload).encode()).decode().rstrip("=")
    tampered = json.loads(legit_kb.decode())
    tampered["capabilities"].append({"knowledge_id": rogue_kid, "token": rogue_kid + "." + "f" * 64})
    inst.config_snapshot_bytes((json.dumps(tampered, sort_keys=True) + "\n").encode(), kb_path)
    with pytest.raises(ValueError, match="file drift"):
        publication.recover(inst, doc_a["id"])
    assert not (inst.root / "pending.json").exists()

    # 恢复合法文件并在 recover(doc_a) 写知识文件时注入崩溃，验证重试幂等收敛且 B 保持启用
    inst.config_snapshot_bytes(legit_kb, kb_path)
    real_snapshot = inst.config_snapshot_bytes
    monkeypatch.setattr(
        inst, "config_snapshot_bytes", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError, match="crash"):
        publication.recover(inst, doc_a["id"])
    assert (inst.root / "pending.json").exists()
    assert publication.stage(inst, doc_a, observe_only=True)["state"] == "reconcile"

    monkeypatch.setattr(inst, "config_snapshot_bytes", real_snapshot)
    recovered = publication.recover(inst, doc_a["id"])
    assert recovered["phase"] == "restored" and not recovered["activated"]
    assert not (inst.root / "pending.json").exists()
    assert publication.observe(inst, doc_b)["activated"] is True


def test_s2_h2_h4_m1_fail_closed_and_staged_peer_guards(staged_host):
    """覆盖 S2（空能力/历史回滚/stage基线）、H2（B staged 时恢复 A 再激活 B）、H4 与 M1。"""
    inst, doc_a, _ = staged_host
    doc_a, doc_b = _two_tenant_setup(inst, doc_a)
    original_prod = (inst.config / "production.env").read_bytes()

    publication.stage(inst, doc_a)
    publication.activate(inst, doc_a, verification(inst, doc_a))
    reg_after_a = (inst.config / "registry/environment.json").read_bytes()
    kb_path = inst.config / "knowledge/knowledge-delegation.json"
    kb_after_a = kb_path.read_bytes()

    # S2-C：第三方篡改 knowledge-delegation.json 后，stage(B) 在 setup 阶段即拒绝
    empty_kb = json.dumps(
        {
            "schema": "aiops-knowledge-delegation/1",
            "base_url": "https://knowledge.example",
            "capabilities": [],
        },
        sort_keys=True,
    )
    inst.config_snapshot_bytes((empty_kb + "\n").encode(), kb_path)
    after_a = inst.read_record("installed.json")
    doc_b["installed"] = {**after_a, "source_revision": after_a["revision"]}
    with pytest.raises(ValueError, match="knowledge baseline changed"):
        publication.stage(inst, doc_b)
    # S2-A：空 capabilities 也必须被 recover(A) 拒绝
    with pytest.raises(ValueError, match="file drift"):
        publication.recover(inst, doc_a["id"])
    inst.config_snapshot_bytes(kb_after_a, kb_path)

    # H2：A activated + B staged 期间恢复 A，随后激活 B 必须写出实时 knowledge_sha256 并通过 observe(B)
    publication.stage(inst, doc_b)
    assert publication.recover(inst, doc_a["id"])["phase"] == "restored"
    assert publication.activate(inst, doc_b, verification(inst, doc_b))["activated"] is True
    assert publication.observe(inst, doc_b)["activated"] is True

    # S2-B：第三方把 environment.json 回滚到 A 激活时的旧版本，recover(B) 必须拒绝
    reg_live = (inst.config / "registry/environment.json").read_bytes()
    inst.registry_snapshot_bytes(reg_after_a, inst.config / "registry/environment.json")
    with pytest.raises(ValueError, match="registry"):
        publication.recover(inst, doc_b["id"])
    inst.registry_snapshot_bytes(reg_live, inst.config / "registry/environment.json")

    # S2-B：第三方把 production.env 回滚到 P0，recover(B) 必须拒绝
    prod_live = (inst.config / "production.env").read_bytes()
    inst.config_snapshot_bytes(original_prod, inst.config / "production.env")
    with pytest.raises(ValueError, match="file drift"):
        publication.recover(inst, doc_b["id"])
    inst.config_snapshot_bytes(prod_live, inst.config / "production.env")

    # M1：publication-tasks 下出现未知文件或悬空符号链接必须 fail-closed
    stray = inst.root / "publication-tasks" / "rogue.txt"
    stray.write_text("x")
    with pytest.raises(ValueError, match="untrusted publication task entry"):
        publication.observe(inst, doc_b)
    stray.unlink()

    # H4：同租户 A 存在残留高字典序 staged 任务（task_f...）与更新的低字典序 activated 任务（task_1...）：
    # 对端 B 的 observe 必须确定性采用 activated 能力，且恢复旧 task_A1 必须被拒绝
    payload_a2 = ["test", "same", "workspace", "consumer-app", "kb-v2", "now"]
    kid_a2 = "kdx_" + base64.urlsafe_b64encode(json.dumps(payload_a2).encode()).decode().rstrip("=")
    live_inst = inst.read_record("installed.json")
    doc_a1_stale = {
        **doc_a,
        "id": "task_" + "f" * 32,
        "installed": {**live_inst, "source_revision": live_inst["revision"]},
    }
    publication.stage(inst, doc_a1_stale)
    live_after_a1 = inst.read_record("installed.json")
    doc_a2_new = {
        **doc_a,
        "id": "task_" + "1" * 32,
        "installed": {**live_after_a1, "source_revision": live_after_a1["revision"]},
        "knowledge": {
            "schema": "aiops-knowledge-delegation/1",
            "base_url": "https://knowledge.example",
            "capabilities": [{"knowledge_id": kid_a2, "token": kid_a2 + "." + "9" * 64}],
        },
    }
    publication.stage(inst, doc_a2_new)
    publication.activate(inst, doc_a2_new, verification(inst, doc_a2_new))
    assert publication.observe(inst, doc_b)["activated"] is True
    with pytest.raises(ValueError, match="superseded by active publication"):
        publication.recover(inst, doc_a1_stale["id"])


def test_h1_recover_succeeds_after_intervening_install_upgrade(staged_host, task_state):
    """H1：A 激活后经 install.py 升级/追加新租户候选安装推进 installed.json，A 仍可独立恢复。"""
    from tooling.test_gateway_install import next_bundle

    inst, doc_a, _ = staged_host
    doc_a, _ = _two_tenant_setup(inst, doc_a)
    original_prod = (inst.config / "production.env").read_bytes()
    publication.stage(inst, doc_a)
    publication.activate(inst, doc_a, verification(inst, doc_a))
    current = inst.read_record("installed.json")
    runtime = inst.runtime_path(current) / "runtime.tar"
    _, application, sha, *_ = next_bundle(task_state[2])
    inst.revision = "b" * 40
    assert (
        inst.apply(
            application,
            sha,
            "/usr/bin/python3",
            inst.config / "production.env",
            current["config_sha256"],
            registry_file=inst.config / "registry/environment.json",
            expected_registry_sha=current["registry_sha256"],
            runtime_archive=runtime,
            runtime_sha=current["runtime_sha256"],
            expected_runtime_sha=current["runtime_sha256"],
        )["result"]
        == "installed"
    )
    upgraded = inst.read_record("installed.json")
    assert upgraded["artifact_sha256"] == sha
    restored = publication.recover(inst, doc_a["id"])
    assert restored["phase"] == "restored" and not restored["activated"]
    assert (inst.config / "production.env").read_bytes() == original_prod
    final_receipt = inst.read_record("installed.json")
    assert final_receipt["artifact_sha256"] == sha
    assert "knowledge_sha256" not in final_receipt


def test_round2_activate_crash_after_peer_restore_and_missing_baseline_guards(staged_host, monkeypatch):
    """覆盖 NEW-H3（B 在 A 恢复后 activate 半途崩溃可重入恢复）、NEW-M1（缺基线 fail-closed）与 NEW-M3。"""
    inst, doc_a, _ = staged_host
    doc_a, doc_b = _two_tenant_setup(inst, doc_a)
    original_prod = (inst.config / "production.env").read_bytes()

    # NEW-H3：A 激活 -> B staged -> A 恢复 -> B activate 在写 installed.json 前崩溃 -> B 仍可 observe/recover
    publication.stage(inst, doc_a)
    publication.activate(inst, doc_a, verification(inst, doc_a))
    after_a = inst.read_record("installed.json")
    doc_b["installed"] = {**after_a, "source_revision": after_a["revision"]}
    publication.stage(inst, doc_b)
    assert publication.recover(inst, doc_a["id"])["phase"] == "restored"

    real_record = inst.record

    def fail_installed_once(name, value):
        if name == "installed.json":
            raise RuntimeError("crash-before-installed")
        return real_record(name, value)

    monkeypatch.setattr(inst, "record", fail_installed_once)
    with pytest.raises(RuntimeError, match="crash-before-installed"):
        publication.activate(inst, doc_b, verification(inst, doc_b))
    monkeypatch.setattr(inst, "record", real_record)
    assert (inst.root / "pending.json").exists()
    assert publication.stage(inst, doc_b, observe_only=True)["state"] == "reconcile"

    # NEW-M1 + BUG_0004：基线置于 publication-tasks/ 之外，即使缺失也能从受信任 sibling 反查 P0 并完整恢复
    baseline = inst.root / "publication-delegation-baseline.json"
    assert baseline.exists()
    baseline.unlink()
    recovered_b = publication.recover(inst, doc_b["id"])
    assert recovered_b["phase"] == "restored"
    assert (inst.config / "production.env").read_bytes() == original_prod
    assert not (inst.root / "pending.json").exists()

    # NEW-M3：同租户 A 存在新旧两个 staged 任务（高字典序旧任务 vs 低字典序新任务），
    # 恢复旧 staged 任务不得破坏新 staged 任务
    live_inst = inst.read_record("installed.json")
    doc_a1 = {
        **doc_a,
        "id": "task_" + "f" * 32,
        "installed": {**live_inst, "source_revision": live_inst["revision"]},
    }
    publication.stage(inst, doc_a1)
    time.sleep(0.02)
    live_after_a1 = inst.read_record("installed.json")
    payload_a2 = ["test", "same", "workspace", "consumer-app", "kb-v3", "now"]
    kid_a2 = "kdx_" + base64.urlsafe_b64encode(json.dumps(payload_a2).encode()).decode().rstrip("=")
    doc_a2 = {
        **doc_a,
        "id": "task_" + "2" * 32,
        "installed": {**live_after_a1, "source_revision": live_after_a1["revision"]},
        "knowledge": {
            "schema": "aiops-knowledge-delegation/1",
            "base_url": "https://knowledge.example",
            "capabilities": [{"knowledge_id": kid_a2, "token": kid_a2 + "." + "8" * 64}],
        },
    }
    publication.stage(inst, doc_a2)
    with pytest.raises(ValueError, match="superseded by active publication"):
        publication.recover(inst, doc_a1["id"])
