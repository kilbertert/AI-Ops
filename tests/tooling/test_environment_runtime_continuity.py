"""D42：原安装事务累积租户，旧准入/资源和失败恢复保持。"""

# ruff: noqa: F811 - pytest跨模块复用原安装夹具。

import copy
import hashlib
import io
import json
import tarfile

import pytest

from aiops_diagnostics.gateway import environment_artifacts as artifacts
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway.environment_projection import validate_candidate
from tooling.test_gateway_install import host, install  # noqa: F401
from tooling.test_gateway_install_task import task, task_state  # noqa: F401


def archive(path, files):
    with tarfile.open(path, "w") as output:
        for name, value in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.mode, member.size = 0o600, len(value)
            output.addfile(member, io.BytesIO(value))
    path.chmod(0o600)
    return {"path": str(path), "sha256": install.digest(path)}


def second_tenant(state):
    inst, spec, _ = state
    spec = copy.deepcopy(spec)
    spec["id"] = "task_" + "b" * 32
    original = inst.read_record("installed.json")
    directory = inst.config / "registry/environment.json"
    rows = registry.load(directory)["environments"]
    rows[0]["enabled"] = True
    rows[0]["tenants"][0]["enabled"] = True
    registry.apply(directory, rows, expected_revision=1, actor="fixture-approved-activation", managers=[])
    production = inst.config / "production.env"
    production.write_text(
        production.read_text().replace(
            'AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED="false"',
            'AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED="true"',
        )
    )
    original.update(registry_sha256=install.digest(directory), config_sha256=install.digest(production))
    inst.record("installed.json", original)
    spec["expected"] = {k: original[k] for k in spec["expected"]}
    files, manifest = artifacts.runtime_files(
        inst.runtime_path(original) / "runtime.tar", original["runtime_sha256"], "test"
    )
    registration = json.loads(files["registrations.json"])
    registration.update(
        tenant_id="new",
        workspace_id="new-workspace",
        operator_app_id="new-operator",
        consumer_app_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
    )
    files["registrations.json"] = json.dumps(registration).encode()
    targets = json.loads(files["consumer-targets.json"])
    targets["targets"][0].update(tenant_id="new", app_id=registration["consumer_app_id"])
    files["consumer-targets.json"] = json.dumps(targets).encode()
    manifest.update(
        tenant_id="new",
        files={n: hashlib.sha256(raw).hexdigest() for n, raw in files.items() if n != "manifest.json"},
    )
    files["manifest.json"] = json.dumps(manifest).encode()
    spec["archives"]["runtime"] = archive(inst.root.parent / "new-runtime.tar", files)
    candidate, cm = artifacts.candidate_files(
        __import__("pathlib").Path(spec["archives"]["candidate"]["path"]),
        spec["archives"]["candidate"]["sha256"],
        "test",
    )
    body = json.loads(candidate["environment.json"])
    body["environments"][0]["tenants"].append({"id": "new", "name": "新租户", "enabled": False})
    candidate["environment.json"] = json.dumps(body).encode()
    from aiops_diagnostics.gateway.environment_projection import summary

    cm.update(summary({k: v for k, v in candidate.items() if k != "manifest.json"}))
    candidate["manifest.json"] = json.dumps(cm).encode()
    spec["archives"]["candidate"] = archive(inst.root.parent / "new-candidate.tar", candidate)
    return spec, original


def test_original_task_preserves_active_tenant_and_accumulates_runtime(task_state):
    inst, _, _ = task_state
    spec, old = second_tenant(task_state)
    before, _ = artifacts.runtime_files(inst.runtime_path(old) / "runtime.tar", old["runtime_sha256"], "test")
    config = (inst.config / "production.env").read_bytes()
    result = task.execute(inst, spec)
    assert result["state"] == "observed", result
    current = inst.read_record("installed.json")
    files, manifest = artifacts.runtime_files(
        inst.runtime_path(current) / "runtime.tar", current["runtime_sha256"], "test"
    )
    assert (
        manifest["source_runtime_sha256"]
        == result["source_runtime_sha256"]
        == spec["archives"]["runtime"]["sha256"]
    )
    assert manifest["previous_runtime_sha256"] == result["previous_runtime_sha256"] == old["runtime_sha256"]
    assert result["runtime_sha256"] != result["source_runtime_sha256"]
    for name in set(before) - {"manifest.json", "consumer-targets.json", "registrations.json"}:
        assert files[name] == before[name]
    targets = json.loads(files["consumer-targets.json"])["targets"]
    assert targets[0] == json.loads(before["consumer-targets.json"])["targets"][0]
    assert [t["tenant_id"] for t in targets] == ["same", "new"]
    row = registry.load(inst.config / "registry/environment.json")["environments"][0]
    assert row["enabled"] and row["tenants"][0]["enabled"] and not row["tenants"][1]["enabled"]
    assert (inst.config / "production.env").read_bytes() == config
    validate_candidate(
        inst.config / "production.env",
        inst.config / "registry/environment.json",
        "test",
        runtime=inst.runtime_path(current),
        candidate_tenant="new",
    )
    with pytest.raises(ValueError):
        validate_candidate(
            inst.config / "production.env",
            inst.config / "registry/environment.json",
            "test",
            runtime=inst.runtime_path(current),
            candidate_tenant="same",
        )
    assert task.execute(inst, spec)["state"] == "observed"
    assert inst.read_record("installed.json") == current
    new_spec = copy.deepcopy(spec)
    new_spec["id"] = "task_" + "c" * 32
    new_spec["expected"] = {k: current[k] for k in new_spec["expected"]}
    repeated = task.execute(inst, new_spec)
    assert repeated["state"] == "observed" and repeated["installation"] == "unchanged"
    assert repeated["runtime_sha256"] == current["runtime_sha256"]


def test_shared_model_conflict_refuses_before_installation(task_state):
    inst, _, _ = task_state
    spec, _ = second_tenant(task_state)
    path = __import__("pathlib").Path(spec["archives"]["runtime"]["path"])
    files, manifest = artifacts.runtime_files(path, spec["archives"]["runtime"]["sha256"], "test")
    files["consumer-model.json"] = b'{"api_key":"changed"}'
    manifest["files"]["consumer-model.json"] = hashlib.sha256(files["consumer-model.json"]).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    spec["archives"]["runtime"] = archive(path, files)
    before = (inst.root / "installed.json").read_bytes()
    with pytest.raises(ValueError, match="shared runtime change"):
        task.execute(inst, spec)
    assert (inst.root / "installed.json").read_bytes() == before
    assert not (inst.root / "pending.json").exists()


@pytest.mark.parametrize("cut", ["directory", "runtime"])
def test_failed_addition_recovers_old_runtime_and_monotonic_admission(task_state, monkeypatch, cut):
    inst, _, _ = task_state
    spec, old = second_tenant(task_state)
    previous = registry.load(inst.config / "registry/environment.json")
    (inst.state / "business-evidence").write_text("keep original run")
    real_snapshot, real_switch, recover = inst.config_snapshot, inst.switch_runtime, inst.recover

    def snapshot(source, target, expected):
        real_snapshot(source, target, expected)
        if cut == "directory" and target == inst.config / "registry/environment.json":
            raise RuntimeError("after-directory")

    def switch(record):
        real_switch(record)
        if cut == "runtime":
            raise RuntimeError("after-runtime")

    with monkeypatch.context() as patch:
        patch.setattr(inst, "config_snapshot", snapshot)
        patch.setattr(inst, "switch_runtime", switch)
        patch.setattr(inst, "recover", lambda: None)
        with pytest.raises(RuntimeError):
            task.execute(inst, spec)
    assert recover()["result"] == "restored"
    restored = registry.load(inst.config / "registry/environment.json")
    assert restored["revision"] > previous["revision"]
    assert restored["environments"] == previous["environments"]
    assert inst.read_record("installed.json")["runtime_sha256"] == old["runtime_sha256"]
    assert (inst.state / "business-evidence").read_text() == "keep original run"
    assert task.execute(inst, spec, observe_only=True)["state"] == "blocked"


def test_old_customer_http_still_answers_after_new_tenant_install_and_registration(
    task_state, tmp_path, monkeypatch
):
    from pathlib import Path

    from consumer.test_consumer_agent import ask, wire
    from consumer.test_consumer_chat_service import done
    from execution.test_app_runtime_dify_entry import dify_chat

    from aiops_diagnostics.dify.self_service_store import SelfServiceStore
    from aiops_diagnostics.gateway.environment_registry import EnvironmentRegistry

    inst, _, _ = task_state
    spec, _ = second_tenant(task_state)
    root = tmp_path / "http"
    root.mkdir()
    with dify_chat(root, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, root)
        document = json.loads(env["bindings"].read_text())
        document["bindings"][0]["environment"] = "test"
        env["bindings"].write_text(json.dumps(document))
        content = Path(document["bindings"][0]["content_file"])
        body = json.loads(content.read_text())
        body["environment"] = "test"
        content.write_text(json.dumps(body))
        environment = env["settings"]
        environment.execution_environment = "test"
        environment.environment = "test"
        environment.environment_registry_file = str(inst.config / "registry/environment.json")
        runtime.environment_registry = EnvironmentRegistry(environment.environment_registry_file, "test")
        original_resolve = env["app"].state.gateway.caller_resolver.resolve

        def resolve(*args, **kwargs):
            from dataclasses import replace

            caller = original_resolve(*args, **kwargs)
            return replace(
                caller,
                effective_tenant_id="same",
                subject=replace(caller.subject, tenant_id="same"),
                caller=replace(caller.caller, tenant_id="same"),
                scope_fingerprint="",
            )

        # Bind the existing real QA route to the existing fixture tenant; no new model engine.
        document["bindings"][0]["tenant_id"] = "same"
        env["bindings"].write_text(json.dumps(document))
        body["tenant_id"] = "same"
        content.write_text(json.dumps(body))
        monkeypatch.setattr(env["app"].state.gateway.caller_resolver, "resolve", resolve)
        before = env["bindings"].read_bytes()
        first = ask(env, "hello")
        assert first.status_code == 202, first.text
        assert done(env, first.json()["qa_id"]).json()["status"] == "completed"
        assert task.execute(inst, spec)["state"] == "observed"
        current = inst.read_record("installed.json")
        files, _ = artifacts.runtime_files(
            inst.runtime_path(current) / "runtime.tar", current["runtime_sha256"], "test"
        )
        registration = json.loads(files["registrations.json"])
        consumer = next(
            t for t in json.loads(files["consumer-targets.json"])["targets"] if t["tenant_id"] == "new"
        )
        registered = SelfServiceStore(environment.database_file).register_runtime(
            registration, consumer, verify=lambda: None
        )
        assert registered
        assert env["bindings"].read_bytes() == before
        second = ask(env, "hello again")
        assert second.status_code == 202, second.text
        assert done(env, second.json()["qa_id"]).json()["status"] == "completed"
        assert done(env, first.json()["qa_id"]).json()["status"] == "completed"
        with pytest.raises(registry.RegistryError):
            runtime.environment_registry.require("new")


def test_candidate_venv_gate_refuses_new_admission_and_active_target(task_state, tmp_path):
    inst, _, _ = task_state
    spec, _ = second_tenant(task_state)
    previous = tmp_path / "before-directory.json"
    previous.write_bytes((inst.config / "registry/environment.json").read_bytes())
    previous.chmod(0o600)
    assert task.execute(inst, spec)["state"] == "observed"
    current = inst.read_record("installed.json")
    directory = inst.config / "registry/environment.json"
    validate_candidate(
        inst.config / "production.env",
        directory,
        "test",
        runtime=inst.runtime_path(current),
        previous_directory=previous,
    )
    original = directory.read_bytes()
    changed = json.loads(original)
    changed["environments"][0]["tenants"][1]["enabled"] = True
    directory.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="admission changed"):
        validate_candidate(
            inst.config / "production.env",
            directory,
            "test",
            runtime=inst.runtime_path(current),
            previous_directory=previous,
        )
    directory.write_bytes(original)


def test_existing_configuration_change_cannot_hide_in_new_tenant_task(task_state):
    inst, _, _ = task_state
    second_tenant(task_state)
    original = (inst.config / "production.env").read_bytes()
    with pytest.raises(ValueError, match="separate window"):
        inst.candidate_configuration(original + b'AIOPS_MYSQL_PASSWORD="changed"\n')
    assert (inst.config / "production.env").read_bytes() == original


def test_original_single_inactive_tenant_can_still_change_own_runtime(task_state, tmp_path):
    from pathlib import Path

    inst, spec, _ = task_state
    value = spec["archives"]["runtime"]
    files, manifest = artifacts.runtime_files(Path(value["path"]), value["sha256"], "test")
    files["consumer-model.json"] = b'{"api_key":"new-approved-own-key"}'
    manifest["files"]["consumer-model.json"] = hashlib.sha256(files["consumer-model.json"]).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    new = archive(tmp_path / "own-update.tar", files)
    path, sha = inst.candidate_runtime(Path(new["path"]), new["sha256"], tmp_path / "merged.tar")
    assert path == Path(new["path"]) and sha == new["sha256"]
    assert not (tmp_path / "merged.tar").exists()


def retarget_runtime(source, sha, dest, tenant, app_id):
    """把既有候选运行档重铸为同租户新身份；登记面与共享文件保持不变。"""
    files, manifest = artifacts.runtime_files(source, sha, "test")
    registration = json.loads(files["registrations.json"])
    registration.update(tenant_id=tenant, consumer_app_id=app_id)
    files["registrations.json"] = json.dumps(registration).encode()
    targets = json.loads(files["consumer-targets.json"])
    targets["targets"][0].update(tenant_id=tenant, app_id=app_id)
    files["consumer-targets.json"] = json.dumps(targets).encode()
    manifest.update(
        tenant_id=tenant,
        files={n: hashlib.sha256(raw).hexdigest() for n, raw in files.items() if n != "manifest.json"},
    )
    files["manifest.json"] = json.dumps(manifest).encode()
    return archive(dest, files)


def test_inactive_tenant_runtime_row_replaced_by_fresh_candidate(task_state, tmp_path):
    """被取消候选留下的未开通租户行可由重铸候选原位替换；已开通租户行不变。"""
    from pathlib import Path

    inst, _, _ = task_state
    spec, old = second_tenant(task_state)
    assert task.execute(inst, spec)["state"] == "observed"
    current = inst.read_record("installed.json")
    before, _ = artifacts.runtime_files(
        inst.runtime_path(current) / "runtime.tar", current["runtime_sha256"], "test"
    )
    recandidate = retarget_runtime(
        Path(spec["archives"]["runtime"]["path"]),
        spec["archives"]["runtime"]["sha256"],
        inst.root.parent / "recandidate-runtime.tar",
        "new",
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    )
    again = copy.deepcopy(spec)
    again["id"] = "task_" + "d" * 32
    again["expected"] = {k: current[k] for k in again["expected"]}
    again["archives"]["runtime"] = recandidate
    result = task.execute(inst, again)
    assert result["state"] == "observed", result
    now = inst.read_record("installed.json")
    files, manifest = artifacts.runtime_files(
        inst.runtime_path(now) / "runtime.tar", now["runtime_sha256"], "test"
    )
    targets = json.loads(files["consumer-targets.json"])["targets"]
    assert [t["tenant_id"] for t in targets] == ["same", "new"]
    assert targets[0] == json.loads(before["consumer-targets.json"])["targets"][0]
    assert targets[1]["app_id"] == "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    assert manifest["source_runtime_sha256"] == recandidate["sha256"]
    assert manifest["previous_runtime_sha256"] == current["runtime_sha256"]
    assert old["runtime_sha256"] != current["runtime_sha256"]
    # 替换结果可幂等重放：同一候选再次组合收敛为无操作。
    assert task.execute(inst, again)["state"] == "observed"
    assert inst.read_record("installed.json") == now


def test_enabled_tenant_runtime_row_still_refuses_candidate_replacement(task_state, tmp_path):
    from pathlib import Path

    inst, _, _ = task_state
    spec, old = second_tenant(task_state)
    assert task.execute(inst, spec)["state"] == "observed"
    recandidate = retarget_runtime(
        inst.runtime_path(old) / "runtime.tar",
        old["runtime_sha256"],
        tmp_path / "recandidate-same.tar",
        "same",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
    )
    with pytest.raises(ValueError, match="cannot be replaced by candidate"):
        inst.candidate_runtime(Path(recandidate["path"]), recandidate["sha256"], tmp_path / "merged.tar")
    assert not (tmp_path / "merged.tar").exists()


def test_tenant_row_without_enabled_key_still_refuses_replacement(task_state, tmp_path):
    """目录行缺 enabled 键按保护处理（fail-closed），不得当成未开通放行。"""
    from pathlib import Path

    inst, _, _ = task_state
    spec, _ = second_tenant(task_state)
    assert task.execute(inst, spec)["state"] == "observed"
    directory = inst.config / "registry/environment.json"
    body = json.loads(directory.read_text())
    del body["environments"][0]["tenants"][1]["enabled"]
    directory.write_text(json.dumps(body))
    record = inst.read_record("installed.json")
    record["registry_sha256"] = install.digest(directory)
    inst.record("installed.json", record)
    recandidate = retarget_runtime(
        Path(spec["archives"]["runtime"]["path"]),
        spec["archives"]["runtime"]["sha256"],
        tmp_path / "recandidate.tar",
        "new",
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    )
    with pytest.raises(ValueError, match="cannot be replaced by candidate"):
        inst.candidate_runtime(Path(recandidate["path"]), recandidate["sha256"], tmp_path / "merged.tar")
    assert not (tmp_path / "merged.tar").exists()


def test_unverifiable_registry_still_refuses_replacement(task_state, tmp_path):
    """安装记录缺 registry_sha256 时判据集合为空——check_registry 先行拒绝。"""
    from pathlib import Path

    inst, _, _ = task_state
    spec, _ = second_tenant(task_state)
    assert task.execute(inst, spec)["state"] == "observed"
    record = inst.read_record("installed.json")
    record.pop("registry_sha256")
    inst.record("installed.json", record)
    recandidate = retarget_runtime(
        Path(spec["archives"]["runtime"]["path"]),
        spec["archives"]["runtime"]["sha256"],
        tmp_path / "recandidate.tar",
        "new",
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    )
    with pytest.raises(ValueError, match="registry configuration drift"):
        inst.candidate_runtime(Path(recandidate["path"]), recandidate["sha256"], tmp_path / "merged.tar")
    assert not (tmp_path / "merged.tar").exists()


def test_consumer_candidate_can_open_while_other_tenant_is_enabled(task_state, tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from aiops_diagnostics import __version__
    from aiops_diagnostics.consumer import candidate_trial

    inst, _, _ = task_state
    spec, _ = second_tenant(task_state)
    assert task.execute(inst, spec)["state"] == "observed"
    # Build/version is the fixture artifact; set the matching installed build in this isolated copy.
    build = inst.root / "current/manifest.json"
    body = json.loads(build.read_text())
    body["version"] = __version__
    build.write_text(json.dumps(body))

    class LocalPath(type(Path())):
        def __new__(cls, *args):
            value = str(args[0])
            for source, dest in (
                ("/etc/aiops-access-test", inst.config),
                ("/opt/aiops-access-test", inst.root),
            ):
                if value == source or value.startswith(source + "/"):
                    return Path(str(dest) + value[len(source) :])
            return Path(*args)

    monkeypatch.setattr(candidate_trial, "Path", LocalPath)
    context = SimpleNamespace(
        settings=SimpleNamespace(
            environment="test",
            execution_environment="test",
            server_config_file=str(inst.config / "production.env"),
            environment_registry_file=str(inst.config / "registry/environment.json"),
        )
    )
    endpoint, _ = candidate_trial.installed(context, {"environment": "test", "tenant_id": "new"})
    assert endpoint == "https://target.example/cpms"
    with pytest.raises(ValueError, match="changed"):
        candidate_trial.installed(context, {"environment": "test", "tenant_id": "same"})
