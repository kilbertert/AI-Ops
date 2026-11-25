"""真实私有文件/SQLite配合主机命令替身，证明重复、失败与拒绝不丢状态。"""

import hashlib
import importlib.util
import json
import os
import re
import runpy
import socket
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("managed_probe", ROOT / "ops/environments/gateway/probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
sys.modules["probe"] = probe
SPEC = importlib.util.spec_from_file_location("managed_install", ROOT / "ops/environments/gateway/install.py")
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)
fixture_bundle = runpy.run_path(str(ROOT / "tests/tooling/test_gateway_candidate.py"))["bundle"]


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(
        install.pwd,
        "getpwnam",
        lambda _: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_shell="/usr/sbin/nologin"),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    inst = install.Installer("test", "aiops", port)
    inst.root, inst.state, inst.config, inst.unit = (tmp_path / n for n in ("app", "state", "config", "unit"))
    inst.backups = tmp_path / "backups"
    trusted = install.trusted
    monkeypatch.setattr(install, "trusted", lambda p, **kw: trusted(p, **{**kw, "owner": os.getuid()}))
    monkeypatch.setattr(install.os, "chown", lambda *a: None)
    monkeypatch.setattr(install.shutil, "disk_usage", lambda _: SimpleNamespace(free=2 * 1024**3))
    calls, active = [], set()

    def command(*args):
        calls.append(args)
        if args[0] == "systemctl":
            if args[1] == "show":
                if "DropInPaths" in args:
                    return ""
                return (
                    ("loaded" if args[2] in active else "not-found") if args[-1] == "--value" else "MainPID=0"
                )
            if args[1] == "is-active" and args[-1] not in active:
                raise subprocess.CalledProcessError(3, args)
            if args[1] == "start":
                active.add(args[2])
            if args[1] == "stop":
                active.discard(args[2])
        elif args[0] == "systemd-run":
            active.add(next(x.split("=", 1)[1] for x in args if x.startswith("--unit=")))
        elif "-c" in args:
            return "3.12"
        elif "venv" in args:
            (Path(args[-1]) / "bin").mkdir(parents=True)
        return ""

    def health(version, **kwargs):
        if kwargs.get("port"):
            assert inst.name + "-candidate" in active
        else:
            assert inst.name in active
            assert json.loads((inst.root / "current/manifest.json").read_text())["version"] == version

    monkeypatch.setattr(inst, "command", command)
    monkeypatch.setattr(inst, "health", health)
    monkeypatch.setattr(inst, "check_database", lambda _: None)
    config = tmp_path / "input.env"
    config.write_text("# private empty config\n")
    config.chmod(0o600)
    archive, sha, meta = fixture_bundle(tmp_path)
    return inst, archive, sha, config, calls, active


def apply(host):
    i, archive, sha, config, *_ = host
    return i.apply(archive, sha, "/usr/bin/python3", config, install.digest(config))


@pytest.mark.parametrize("action", ["install", "recover"])
@pytest.mark.parametrize("mode", ["scalar", "candidate", "runtime"])
def test_packaged_cli_checks_pinned_module_before_action(host, tmp_path, monkeypatch, action, mode):
    inst, archive, sha, config, *_ = host
    root = tmp_path / "installer"
    root.mkdir()
    for name in ("install.py", "runtime_check.py", "probe.py"):
        (root / name).write_bytes((ROOT / "ops/environments/gateway" / name).read_bytes())
    (root / "artifact_checks.py").write_bytes(
        (ROOT / "src/aiops_diagnostics/onboarding/artifacts.py").read_bytes()
    )
    (root / "REVISION").write_text("a" * 40)
    (root / "SHA256SUMS").write_text(
        "".join(hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n" for p in root.iterdir())
    )
    monkeypatch.setattr(install, "__file__", str(root / "install.py"))
    monkeypatch.setattr(install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(install, "Installer", lambda *args: inst)
    monkeypatch.setattr(install, "trusted", lambda *args, **kwargs: None)
    original_open = os.open
    monkeypatch.setattr(
        install.os,
        "open",
        lambda p, *a, **kw: original_open(
            tmp_path / "lock" if str(p).startswith("/run/lock/") else p, *a, **kw
        ),
    )
    calls = []
    monkeypatch.setattr(inst, "recover", lambda: calls.append(action) or {"result": "recovered"})
    monkeypatch.setattr(inst, "apply", lambda *a: calls.append(action) or {"result": "installed"})
    options = ["--config", str(config), "--config-sha256", install.digest(config)]
    if mode in {"candidate", "runtime"}:
        import io
        import tarfile

        from aiops_diagnostics.onboarding.projection import summary

        pair = candidate_config(host)
        files = {"production.env": pair[0].read_bytes(), "environment.json": pair[1].read_bytes()}
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
        files["manifest.json"] = json.dumps(manifest).encode()
        candidate = tmp_path / "candidate.tar.gz"
        with tarfile.open(candidate, "w:gz") as bundle:
            for name, raw in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                bundle.addfile(member, io.BytesIO(raw))
        candidate.chmod(0o600)
        options = ["--candidate", str(candidate), "--candidate-sha256", install.digest(candidate)]
        if mode == "runtime":
            runtime = runtime_bundle(host)
            options += ["--runtime", str(runtime), "--runtime-sha256", install.digest(runtime)]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(root / "install.py"),
            action,
            "--environment",
            "test",
            "--user",
            "aiops",
            "--port",
            "18951",
            "--archive",
            str(archive),
            "--sha256",
            sha,
            *options,
        ],
    )
    assert install.main() == 0 and calls == [action]
    (root / "artifact_checks.py").write_text("tampered")
    assert install.main() == 1 and calls == [action]


def next_bundle(host):
    import hashlib
    import io
    import tarfile

    inst, archive, _, config, calls, active = host
    with tarfile.open(archive) as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}
    meta = json.loads(files["manifest.json"])
    meta.update(revision="b" * 40, version="1.0.1+" + "b" * 12)
    files["manifest.json"] = json.dumps(meta).encode()
    files["SHA256SUMS"] = "".join(
        hashlib.sha256(v).hexdigest() + "  " + k + "\n" for k, v in files.items() if k != "SHA256SUMS"
    ).encode()
    target = archive.with_name("new.tar.gz")
    with tarfile.open(target, "w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return inst, target, install.digest(target), config, calls, active


def test_cold_repeat_and_upgrade_keep_owned_state(host):
    assert apply(host)["result"] == "installed"
    inst, _, _, _, calls, _ = host
    before = len(calls)
    assert apply(host)["result"] == "unchanged"
    assert not any(c[:2] == ("systemctl", "stop") for c in calls[before:])
    (inst.state / "evidence").write_text("retain")
    assert apply(next_bundle(host))["version"] == "1.0.1+" + "b" * 12
    assert (inst.state / "evidence").read_text() == "retain"
    assert (inst.root / "current/state-backup/evidence").read_text() == "retain"
    assert not (inst.root / "pending.json").exists()


@pytest.mark.parametrize("failure", ["candidate", "installed", "schema"])
def test_failed_upgrade_preserves_old_version_and_state(host, monkeypatch, failure):
    apply(host)
    inst, *_, active = host
    old = (inst.root / "current").readlink()
    (inst.state / "evidence").write_text("retain")
    health = inst.health

    def fail(version, **kw):
        if version.startswith("1.0.1") and bool(kw.get("port")) == (failure == "candidate"):
            raise RuntimeError("injected candidate failure")
        return health(version, **kw)

    monkeypatch.setattr(inst, "health", fail)
    if failure == "schema":
        monkeypatch.setattr(inst, "check_database", lambda _: (_ for _ in ()).throw(ValueError("schema")))
    with pytest.raises((RuntimeError, ValueError)):
        apply(next_bundle(host))
    assert (inst.root / "current").readlink() == old
    assert inst.name in active
    assert (inst.state / "evidence").read_text() == "retain"
    assert not (inst.root / "pending.json").exists()


@pytest.mark.parametrize("failure", ["config", "symlink", "unknown", "unit"])
def test_preflight_refuses_without_service_mutation(host, failure):
    inst, archive, sha, config, calls, _ = host
    if failure == "config":
        config.chmod(0o644)
    elif failure == "symlink":
        inst.root.symlink_to(config.parent)
    elif failure == "unknown":
        inst.state.mkdir()
    else:
        inst.unit.write_text("company-owned unit")
    with pytest.raises(ValueError):
        apply(host)
    assert not any(c[:2] in (("systemctl", "stop"), ("systemctl", "start")) for c in calls)


def test_pending_recovery_checks_original_health_and_keeps_data(host):
    apply(host)
    inst, *_, active = host
    old = inst.read_record("installed.json")
    inst.record("pending.json", {**old, "previous": old, "phase": "switching"})
    with pytest.raises(ValueError, match="recover"):
        apply(host)
    assert inst.recover()["result"] == "restored"
    assert inst.name in active
    assert not (inst.root / "pending.json").exists()


def test_runtime_rejects_schema_and_inflight_without_mutating_database(tmp_path, monkeypatch):
    import sqlite3

    from aiops_diagnostics.gateway import gateway_schema

    check = runpy.run_path(str(ROOT / "ops/environments/gateway/runtime_check.py"))["check"]
    cfg = tmp_path / "input.env"
    cfg.write_text("# empty\n")
    db = tmp_path / "gateway/gateway.db"
    gateway_schema.startup(db)
    assert check(cfg, tmp_path, "test", existing=True)["version"] == gateway_schema.VERSION
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO assistant_questions "
            "(qa_id,scope_fingerprint,question,status,created_at,updated_at,deadline_at) "
            "VALUES ('q','s','q','running','n','n','n')"
        )
    before = db.read_bytes()
    with pytest.raises(ValueError, match="inflight"):
        check(cfg, tmp_path, "test", existing=True)
    assert db.read_bytes() == before
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(gateway_schema.SchemaError):
        check(cfg, tmp_path, "test", existing=True)


def test_failed_cold_install_retains_evidence_and_can_retry(host, monkeypatch):
    inst, *_, active = host
    health = inst.health
    monkeypatch.setattr(inst, "health", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("failed")))
    with pytest.raises(RuntimeError):
        apply(host)
    assert not inst.root.exists() and not inst.unit.exists() and not active
    assert len(list(inst.backups.glob("failed-*/release-root/pending.json"))) == 1
    monkeypatch.setattr(inst, "health", health)
    assert apply(host)["result"] == "installed"


def test_recovery_preserves_unowned_trial_named_evidence(host, monkeypatch):
    apply(host)
    inst = host[0]
    unrelated = inst.state / "trial-business-evidence"
    unrelated.mkdir()
    (unrelated / "receipt").write_text("keep")
    health = inst.health

    def fail(version, **kw):
        if kw.get("port"):
            raise RuntimeError("candidate failed")
        return health(version, **kw)

    monkeypatch.setattr(inst, "health", fail)
    with pytest.raises(RuntimeError):
        apply(next_bundle(host))
    assert (unrelated / "receipt").read_text() == "keep"


def test_cold_root_is_published_only_with_recovery_record(host, monkeypatch):
    inst = host[0]
    original = install.write

    def fail(path, body):
        if path.name == "pending.json":
            raise OSError("interrupted write")
        original(path, body)

    monkeypatch.setattr(install, "write", fail)
    with pytest.raises(OSError):
        apply(host)
    assert not inst.root.exists()
    monkeypatch.setattr(install, "write", original)
    assert apply(host)["result"] == "installed"


def test_trial_accepts_formal_managed_route_and_does_not_touch_it(tmp_path, monkeypatch):
    check = runpy.run_path(str(ROOT / "ops/environments/gateway/runtime_check.py"))["check"]
    root = tmp_path / "state"
    root.mkdir()
    config = tmp_path / "production.env"
    config.write_text(f"AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE={root}/gateway/gateway.db\n")
    config.chmod(0o600)
    monkeypatch.setattr(install.os, "environ", {})
    assert (
        check(config, root / "trial", "test", existing=False, managed_state=root)["configuration"] == "valid"
    )
    assert not (root / "gateway/gateway.db").exists()


def test_mismatched_installer_revision_is_refused(host):
    host[0].revision = "f" * 40
    with pytest.raises(ValueError, match="revisions differ"):
        apply(host)
    assert not host[0].root.exists()


def test_wrong_config_digest_refused_before_writes(host):
    inst, archive, sha, config, *_ = host
    with pytest.raises(ValueError, match="configuration digest"):
        inst.apply(archive, sha, "/usr/bin/python3", config, "0" * 64)
    assert not inst.root.exists()


def test_existing_backup_mount_is_rejected_before_cold_writes(host, monkeypatch):
    inst = host[0]
    inst.backups.mkdir(mode=0o700)
    original = Path.stat

    def mounted(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == inst.backups:
            fields = list(result)
            fields[2] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", mounted)
    with pytest.raises(ValueError, match="one filesystem"):
        apply(host)
    assert not inst.root.exists()
    assert not any(c[0] == "systemd-run" for c in host[4])


@pytest.mark.parametrize("failure", [None, "candidate", "installed"])
def test_configuration_replacement_and_recovery(host, monkeypatch, failure):
    apply(host)
    inst, archive, sha, config, calls, active = host
    old_config = config.read_bytes()
    old_digest = install.digest(config)
    old_release = (inst.root / "current").readlink()
    replacement = config.with_name("new.env")
    replacement.write_text("AIOPS_QUERY_TIMEOUT_SECONDS=8\n")
    replacement.chmod(0o600)
    health = inst.health
    seen_configs = []

    def inspect_health(version, **kw):
        current = (inst.config / "production.env").read_bytes()
        if kw.get("port"):
            assert current == old_config
            candidate = next(c for c in reversed(calls) if c[0] == "systemd-run")
            binding = next(
                x.split("=", 2)[2] for x in candidate if x.startswith("--property=BindReadOnlyPaths=")
            )
            source, target = binding.split(":")
            assert target == str(inst.config / "production.env")
            path = Path(source)
            assert path.read_bytes() == replacement.read_bytes()
            assert path.stat().st_mode & 0o077 == 0
            if failure == "candidate":
                raise RuntimeError("invalid candidate")
        elif current != old_config:
            seen_configs.append(current)
            if failure == "installed":
                raise RuntimeError("new configuration unhealthy")
        return health(version, **kw)

    monkeypatch.setattr(inst, "health", inspect_health)
    args = (archive, sha, "/usr/bin/python3", replacement, install.digest(replacement), old_digest)
    if failure:
        with pytest.raises(RuntimeError):
            inst.apply(*args)
        assert (inst.config / "production.env").read_bytes() == old_config
        assert (inst.root / "current").readlink() == old_release
    else:
        assert inst.apply(*args)["result"] == "installed"
        assert seen_configs == [replacement.read_bytes()]
        assert inst.apply(*args[:-1])["result"] == "unchanged"
    assert inst.name in active and not (inst.root / "pending.json").exists()


@pytest.mark.parametrize("expected", [None, "0" * 64])
def test_configuration_change_requires_current_digest(host, expected):
    apply(host)
    inst, archive, sha, config, calls, _ = host
    config.write_text("# changed\n")
    before = len(calls)
    with pytest.raises(ValueError):
        inst.apply(archive, sha, "/usr/bin/python3", config, install.digest(config), expected)
    assert not any(c[:2] == ("systemctl", "stop") for c in calls[before:])


def test_interrupted_configuration_switch_restores_matching_pair(host, monkeypatch):
    apply(host)
    inst, archive, sha, config, *_ = host
    old_digest, old_bytes = install.digest(config), config.read_bytes()
    previous = (inst.root / "current").readlink()
    replacement = config.with_name("replacement.env")
    replacement.write_text("AIOPS_QUERY_TIMEOUT_SECONDS=9\n")
    replacement.chmod(0o600)
    health, recover = inst.health, inst.recover

    def crash(version, **kw):
        if not kw.get("port") and (inst.config / "production.env").read_bytes() != old_bytes:
            raise RuntimeError("interrupted after configuration switch")
        return health(version, **kw)

    # 保存真实磁盘中间态；独立Hacker另用进程硬退出确认finally被跳过的路径。
    monkeypatch.setattr(inst, "health", crash)
    monkeypatch.setattr(inst, "recover", lambda: None)
    with pytest.raises(RuntimeError):
        inst.apply(archive, sha, "/usr/bin/python3", replacement, install.digest(replacement), old_digest)
    assert inst.read_record("pending.json")["phase"] == "switching"
    assert (inst.config / "production.env").read_bytes() == replacement.read_bytes()
    monkeypatch.setattr(inst, "health", health)
    monkeypatch.setattr(inst, "recover", recover)
    assert inst.recover()["result"] == "restored"
    assert (inst.config / "production.env").read_bytes() == old_bytes
    assert (inst.root / "current").readlink() == previous


@pytest.mark.parametrize("moment", ["candidate", "stopped"])
def test_intervening_configuration_change_is_not_overwritten(host, monkeypatch, moment):
    apply(host)
    inst, archive, sha, config, calls, _ = host
    before = len(calls)
    expected = install.digest(config)
    config.write_text("# desired\n")
    external = b"# external edit\n"
    health, check = inst.health, inst.check_database

    def health_with_edit(version, **kw):
        result = health(version, **kw)
        if moment == "candidate" and kw.get("port"):
            (inst.config / "production.env").write_bytes(external)
        return result

    def check_with_edit(release):
        if moment == "stopped" and any(c == ("systemctl", "stop", inst.name) for c in calls[before:]):
            (inst.config / "production.env").write_bytes(external)
        return check(release)

    monkeypatch.setattr(inst, "health", health_with_edit)
    monkeypatch.setattr(inst, "check_database", check_with_edit)
    with pytest.raises(ValueError, match="configuration drift"):
        inst.apply(archive, sha, "/usr/bin/python3", config, install.digest(config), expected)
    assert (inst.config / "production.env").read_bytes() == external
    assert (inst.root / "pending.json").exists()
    if moment == "candidate":
        assert ("systemctl", "stop", inst.name) not in calls[before:]


def test_registry_requires_logical_config_path_preserved_for_candidate(tmp_path, monkeypatch):
    from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
    from aiops_diagnostics.onboarding.registry import apply as registry_apply

    registry = tmp_path / "registry/registry.json"
    config = tmp_path / "production.env"
    snapshot = tmp_path / "candidate-config.env"
    row = {
        "id": "test",
        "enabled": False,
        "gateway_url": "https://gateway.example",
        "cpms_base_url": "https://cpms.example",
        "config_ref": str(config),
        "tenants": [],
    }
    registry_apply(registry, [row], expected_revision=0, actor="test")
    config.write_text(f"AIOPS_GATEWAY_ENVIRONMENT_REGISTRY_FILE={registry}\n")
    config.chmod(0o600)
    snapshot.write_bytes(config.read_bytes())
    snapshot.chmod(0o600)
    monkeypatch.setattr(
        install.os,
        "environ",
        {
            "AIOPS_GATEWAY_ENVIRONMENT": "test",
            "AIOPS_DATA_HOME": str(tmp_path / "state"),
            "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(snapshot),
        },
    )
    with pytest.raises(ValueError, match="configuration does not match"):
        GatewayServerSettings.from_env().validate()
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(config))
    GatewayServerSettings.from_env().validate()


def candidate_config(host, *, revision=1, name="候选", config_text=""):
    from aiops_diagnostics.common.private_files import write_private_text
    from aiops_diagnostics.onboarding.projection import render

    inst = host[0]
    draft = dict(
        id="test",
        name=name,
        enabled=True,
        gateway_url="https://target.example",
        cpms_base_url="https://target.example/cpms",
        company_gateway_url="https://target.example",
        deployment_target="host",
        credential_ref="config",
        timezone="Asia/Shanghai",
        tenants=[dict(id="same", name="合成租户", enabled=True)],
    )
    template = write_private_text(inst.root.parent / ("template-" + str(revision) + ".env"), config_text)
    result = render(draft, template, install.digest(template), "2026-11-18T00:00:00+00:00")
    config = write_private_text(
        inst.root.parent / ("candidate-" + str(revision) + ".env"), result["production.env"].decode()
    )
    body = json.loads(result["environment.json"])
    if revision > 1:
        body["revision"] = revision
        body["audit"] += [
            dict(revision=i, actor="candidate-projection", at="2026-11-18T00:00:00+00:00")
            for i in range(2, revision + 1)
        ]
    directory = write_private_text(
        inst.root.parent / ("directory-" + str(revision) + ".json"), json.dumps(body)
    )
    return config, directory


def apply_candidate(host, pair, *, expected=None):
    inst, archive, sha, *_ = host
    old = inst.read_record("installed.json") if (inst.root / "installed.json").exists() else None
    return inst.apply(
        archive,
        sha,
        "/usr/bin/python3",
        pair[0],
        install.digest(pair[0]),
        old["config_sha256"] if old else None,
        pair[1],
        expected if expected is not None else old.get("registry_sha256", "absent") if old else None,
    )


def test_configuration_pair_cold_repeat_and_single_file_transition(host):
    apply(host)
    pair = candidate_config(host)
    assert apply_candidate(host, pair)["result"] == "installed"
    inst, *_, calls, active = host
    assert install.digest(inst.config / "registry/environment.json") == install.digest(pair[1])
    assert (inst.config / "registry").stat().st_mode & 0o077 == 0
    bindings = [
        a for c in calls if c[0] == "systemd-run" for a in c if a.startswith("--property=BindReadOnlyPaths=")
    ]
    assert any(a.endswith(":" + str(inst.config / "registry")) for a in bindings)
    count = len(calls)
    assert apply_candidate(host, pair)["result"] == "unchanged"
    assert not any(c[:2] == ("systemctl", "stop") for c in calls[count:])
    with pytest.raises(ValueError, match="requires a registry"):
        apply(host)
    assert inst.name in active


@pytest.mark.parametrize("failure", ["candidate", "config-written", "directory-written"])
def test_pair_interruption_restores_files_and_preserves_monotonic_revision(host, monkeypatch, failure):
    first = candidate_config(host)
    assert apply_candidate(host, first)["result"] == "installed"
    inst = host[0]
    previous = (inst.root / "current").readlink()
    (inst.state / "evidence").write_text("keep")
    second = candidate_config(host, revision=2, name="changed", config_text="AIOPS_MYSQL_PASSWORD=new\n")
    original_snapshot, original_health, original_recover = inst.config_snapshot, inst.health, inst.recover

    def snapshot(source, target, expected):
        original_snapshot(source, target, expected)
        if (failure == "config-written" and target == inst.config / "production.env") or (
            failure == "directory-written" and target == inst.config / "registry/environment.json"
        ):
            raise RuntimeError("interrupted switch")

    def health(*a, **kw):
        if failure == "candidate" and kw.get("port"):
            raise RuntimeError("candidate rejected")
        return original_health(*a, **kw)

    monkeypatch.setattr(inst, "config_snapshot", snapshot)
    monkeypatch.setattr(inst, "health", health)
    monkeypatch.setattr(inst, "recover", lambda: None)
    with pytest.raises(RuntimeError):
        apply_candidate(host, second)
    assert (inst.root / "pending.json").exists()
    monkeypatch.setattr(inst, "config_snapshot", original_snapshot)
    monkeypatch.setattr(inst, "health", original_health)
    monkeypatch.setattr(inst, "recover", original_recover)
    assert inst.recover()["result"] == "restored"
    assert (inst.config / "production.env").read_bytes() == first[0].read_bytes()
    restored = json.loads((inst.config / "registry/environment.json").read_text())
    assert restored["environments"] == json.loads(first[1].read_text())["environments"]
    assert restored["revision"] == (1 if failure == "candidate" else 3)
    assert (
        install.digest(inst.config / "registry/environment.json")
        == inst.read_record("installed.json")["registry_sha256"]
    )
    assert (inst.root / "current").readlink() == previous
    assert (inst.state / "evidence").read_text() == "keep"


def test_pair_drift_and_stale_digest_do_not_mutate_files(host):
    first = candidate_config(host)
    apply_candidate(host, first)
    inst = host[0]
    second = candidate_config(host, revision=2)
    with pytest.raises(ValueError, match="stale expected registry"):
        apply_candidate(host, second, expected="0" * 64)
    old_config = (inst.config / "production.env").read_bytes()
    (inst.config / "registry/environment.json").write_text("external")
    with pytest.raises(ValueError, match="registry configuration drift"):
        apply_candidate(host, second)
    assert (inst.config / "registry/environment.json").read_text() == "external"
    assert (inst.config / "production.env").read_bytes() == old_config


def test_candidate_local_revision_preserves_repeat_after_recovery(host, monkeypatch):
    first = candidate_config(host)
    apply_candidate(host, first)
    inst = host[0]
    second = candidate_config(host, revision=2, name="new")
    old = inst.health

    def fail(version, **kw):
        if (
            not kw.get("port")
            and json.loads((inst.config / "registry/environment.json").read_text())["environments"][0]["name"]
            == "new"
        ):
            raise RuntimeError("new registry rejected")
        return old(version, **kw)

    monkeypatch.setattr(inst, "health", fail)
    with pytest.raises(RuntimeError):
        apply_candidate(host, second)
    raw = inst.candidate_directory(first[1].read_bytes())
    assert raw == (inst.config / "registry/environment.json").read_bytes()
    incoming = json.loads(inst.candidate_directory(second[1].read_bytes()))
    assert incoming["revision"] == 4 and len(incoming["audit"]) == 4


def test_snapshot_interruption_does_not_pollute_private_registry(host, monkeypatch):
    first = candidate_config(host)
    apply_candidate(host, first)
    inst = host[0]
    old = inst.read_record("installed.json")
    inst.record("pending.json", {**old, "previous": old, "phase": "switching"})
    target = inst.config / "registry/environment.json"
    original_replace = Path.replace

    def interrupted(path, destination):
        if destination == target:
            raise OSError("interrupted before replacement")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", interrupted)
    with pytest.raises(OSError):
        inst.config_snapshot(first[1], target, install.digest(first[1]))
    assert {p.name for p in target.parent.iterdir()} == {"environment.json"}
    monkeypatch.setattr(Path, "replace", original_replace)
    assert inst.recover()["result"] == "restored"


def test_recovery_snapshot_ownership_is_set_before_publication(host, monkeypatch):
    apply(host)
    inst = host[0]
    private = inst.root / "recovery"
    private.mkdir(mode=0o700)
    sentinel = inst.root.parent / "system-file"
    sentinel.write_text("keep")
    original_replace = Path.replace
    target = private / "snapshot.json"
    ownership = []

    def replaced(path, destination):
        result = original_replace(path, destination)
        if destination == target:
            target.unlink()
            target.symlink_to(sentinel)
        return result

    monkeypatch.setattr(Path, "replace", replaced)
    monkeypatch.setattr(install.os, "chown", lambda *a, **k: ownership.append(a))
    inst.registry_snapshot_bytes(b"{}", target)
    assert sentinel.read_text() == "keep" and ownership == []


def test_registry_writer_lock_does_not_block_lifecycle(host):
    first = candidate_config(host)
    apply_candidate(host, first)
    inst = host[0]
    lock = inst.config / "registry/environment.json.lock"
    lock.touch(mode=0o600)
    assert apply_candidate(host, first)["result"] == "unchanged"
    lock.chmod(0o644)
    with pytest.raises(ValueError, match="mode"):
        apply_candidate(host, first)


def test_site_preflight_is_persistent_and_never_restarts(host, monkeypatch):
    apply(host)
    inst, *_, calls, _ = host
    before = len(calls)
    snapshot = (inst.root / "installed.json").read_bytes()
    first = inst.preflight()
    assert inst.preflight()["id"] == first["id"]
    assert inst.preflight(first["id"])["state"] == "observed"
    assert first["scope"] == "installed-instance-readiness-only" and first["executable"] is False
    assert {c["id"] for c in first["checks"] if c["state"] == "pending"} == {
        "managed-host-identity",
        "formal-network",
        "business",
    }
    assert str(inst.root) not in json.dumps(first) and "private" not in json.dumps(first)
    assert (inst.root / "installed.json").read_bytes() == snapshot
    assert not any(c[:2] in (("systemctl", "stop"), ("systemctl", "start")) for c in calls[before:])
    monkeypatch.setattr(install.time, "time", lambda: first["expires_at"] + 1)
    assert inst.preflight(first["id"])["state"] == "expired"


def test_site_preflight_refuses_stale_readiness_and_reports_recovery(host, monkeypatch):
    apply(host)
    inst = host[0]
    first = inst.preflight()
    (inst.config / "production.env").write_text("changed")
    assert inst.preflight(first["id"])["state"] == "expired"
    changed = inst.preflight()
    assert changed["id"] != first["id"] and changed["state"] == "blocked"
    assert {"id": "configuration", "state": "blocked"} in changed["checks"]
    old = inst.read_record("installed.json")
    inst.record("pending.json", {**old, "previous": old, "phase": "switching"})
    pending = inst.preflight()
    assert pending["state"] == "blocked" and {"id": "recovery", "state": "blocked"} in pending["checks"]
    assert (inst.root / "pending.json").exists()
    with pytest.raises(ValueError):
        inst.preflight("../installed")


def test_site_receipt_cannot_cross_local_installation_identity(host):
    apply(host)
    inst = host[0]
    receipt = inst.preflight()
    path = inst.root / "site-preflight" / (receipt["id"] + ".json")
    row = json.loads(path.read_text())
    row["result"]["environment"] = "other"
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="integrity"):
        inst.preflight(receipt["id"])


def test_site_preflight_rechecks_service_instance_and_recorded_results(host, monkeypatch):
    apply(host)
    inst = host[0]
    receipt = inst.preflight()
    record_path = inst.root / "site-preflight" / (receipt["id"] + ".json")
    row = json.loads(record_path.read_text())
    row["result"]["checks"][0]["state"] = "blocked"
    record_path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="integrity"):
        inst.preflight(receipt["id"])
    row["result"]["checks"][0]["state"] = "passed"
    record_path.write_text(json.dumps(row))
    command = inst.command
    monkeypatch.setattr(
        inst, "command", lambda *a: "MainPID=changed" if "ActiveEnterTimestampMonotonic" in a else command(*a)
    )
    assert inst.preflight(receipt["id"])["state"] == "expired"


@pytest.mark.parametrize("field", ["state", "expires_at", "resources"])
def test_site_receipt_rejects_modified_metadata(host, field):
    apply(host)
    inst = host[0]
    receipt = inst.preflight()
    path = inst.root / "site-preflight" / (receipt["id"] + ".json")
    row = json.loads(path.read_text())
    row["result"][field] = {
        "state": "completed",
        "expires_at": receipt["expires_at"] + 100000,
        "resources": {"available_memory_bytes": 999999999999, "available_disk_bytes": 999999999999},
    }[field]
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="integrity"):
        inst.preflight()
    with pytest.raises(ValueError, match="integrity"):
        inst.preflight(receipt["id"])


def test_site_preflight_requires_installed_unit_even_if_process_is_healthy(host):
    apply(host)
    inst = host[0]
    receipt = inst.preflight()
    inst.unit.unlink()
    assert inst.preflight(receipt["id"])["state"] == "expired"
    now = inst.preflight()
    assert now["state"] == "blocked" and {"id": "local-path-identity", "state": "blocked"} in now["checks"]


@pytest.mark.parametrize("fault", ["closed_connection", "closed_listener", "permission", "wrong_version"])
def test_readiness_tolerates_closed_connection_but_requires_listener(monkeypatch, fault):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"version":"fixture","business_mutations":"disabled"}')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    inst = install.Installer.__new__(install.Installer)
    inst.name, inst.port = "fixture", server.server_port
    inst.command = lambda *args: str(os.getpid()) if "MainPID" in args else ""
    original_iter, original_link = Path.iterdir, Path.readlink
    descriptors = Path(f"/proc/{os.getpid()}/fd")
    absent = descriptors / "999999"
    observed = []

    def snapshot(path):
        if path != descriptors:
            return original_iter(path)
        # 真实socket在目录已枚举后关闭；其他连接消失不改变监听socket的归属。
        transient = socket.socket()
        vanished = descriptors / str(transient.fileno())
        transient.close()
        observed.append(vanished)
        return iter([vanished, absent if fault == "closed_listener" else descriptors / str(server.fileno())])

    def readlink(path):
        if fault == "permission" and path in observed:
            raise PermissionError("proc access refused")
        return original_link(path)

    monkeypatch.setattr(Path, "iterdir", snapshot)
    monkeypatch.setattr(Path, "readlink", readlink)
    try:
        if fault == "closed_connection":
            inst.health("fixture", wait=0)
        else:
            with pytest.raises(RuntimeError, match="health identity"):
                inst.health("different" if fault == "wrong_version" else "fixture", wait=0)
        assert observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def runtime_bundle(host, *, model="model-one"):
    """按D30公开格式造合成工件，不读真实模型/节点凭据。"""
    import io

    from aiops_diagnostics.operator import operator_agent, operator_tools

    inst = host[0]
    root = "/etc/aiops-access-test/runtime"
    credential = dict(api_key="synthetic", function_calling_type="tool_call", max_tokens_to_sample=4096)
    approved = dict(
        plugin="fixture",
        provider="fixture",
        name=model,
        credential_file=root + "/operator-model.json",
        completion_params={"max_tokens": 4096},
    )
    files = {
        "agent-runs.json": dict(
            environment="test",
            snail_base_url="https://execution.example",
            snail_app_id="test-app",
            snail_app_token="synthetic",
            snail_agent_id=2,
            snail_open_id="test",
            expected_node_id="test-node",
            nodes=[dict(node_id="test-node", environment="test", token_sha256="a" * 64, active=True)],
        ),
        "operator-manifest.json": dict(
            schema=operator_agent.SCHEMA,
            name="test",
            history_policy=operator_agent.HISTORY_POLICY,
            model=dict(
                provider_key="openai",
                adapter_key="openai-compatible",
                model_key=model,
                endpoint="https://model-proxy:9101",
            ),
            model_options={},
            system_messages=[operator_agent.sop() + "\n" + operator_agent.INSTRUCTIONS],
            tools=[
                dict(name=t.name, description=t.description, input_schema=t.input_schema())
                for t in operator_tools.tools()
            ],
        ),
        "operator-profiles.json": dict(
            schema="operator-profiles/1",
            profiles=[
                dict(
                    id="operator-test",
                    model=approved,
                    consumer_models=[],
                    target=dict(
                        environment="test",
                        entry="operator",
                        config_file=root + "/agent-runs.json",
                        manifest_file=root + "/operator-manifest.json",
                        cpms_base_url="https://target.example/cpms",
                        proxy_base_url="https://gateway-egress:9443",
                    ),
                )
            ],
        ),
        "consumer-targets.json": dict(
            schema="aiops-consumer-targets/1",
            targets=[
                dict(
                    environment="test",
                    tenant_id="same",
                    app_id="c" * 8 + "-" + "c" * 4 + "-" + "c" * 4 + "-" + "c" * 4 + "-" + "c" * 12,
                    knowledge_base_ids=["test-kb"],
                    live_order_status=True,
                    live_billing_record=True,
                )
            ],
        ),
        "registrations.json": dict(
            environment="test",
            gateway_url="https://target.example",
            tenant_id="same",
            workspace_id="workspace",
            operator_app_id="operator",
            enabled=False,
        ),
        "operator-model.json": credential,
        "consumer-model.json": credential,
    }
    files["registrations.json"]["consumer_app_id"] = files["consumer-targets.json"]["targets"][0]["app_id"]
    raw = {name: json.dumps(value).encode() for name, value in files.items()}
    body = dict(
        schema="aiops-environment-runtime/1",
        scope="runtime-wiring-only",
        environment="test",
        tenant_id="same",
        plan_id="plan_" + "a" * 32,
        plan_digest="b" * 64,
        executable=False,
        installed=False,
        activated=False,
        files={name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
    )
    raw["manifest.json"] = json.dumps(body).encode()
    archive = inst.root.parent / (model + ".tar")
    with tarfile.open(archive, "w") as bundle:
        for name, value in raw.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(value), 0o600
            bundle.addfile(member, io.BytesIO(value))
    archive.chmod(0o600)
    return archive


def apply_runtime(host, archive):
    inst, application, sha, *_ = host
    pair = candidate_config(host)
    with pair[0].open("a") as stream:
        stream.write(
            'AIOPS_GATEWAY_OPERATOR_PROFILES_FILE="/etc/aiops-access-test/runtime/operator-profiles.json"\n'
            'AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE="/etc/aiops-access-test/runtime/consumer-targets.json"\n'
        )
    old = inst.read_record("installed.json") if (inst.root / "installed.json").exists() else None
    return inst.apply(
        application,
        sha,
        "/usr/bin/python3",
        pair[0],
        install.digest(pair[0]),
        old["config_sha256"] if old else None,
        pair[1],
        old.get("registry_sha256", "absent") if old else None,
        archive,
        install.digest(archive),
        old.get("runtime_sha256", "absent") if old else None,
    )


@pytest.mark.parametrize("failure", [None, "candidate", "runtime-written"])
def test_runtime_install_repeat_and_restore_keep_database_and_old_snapshot(host, monkeypatch, failure):
    inst = host[0]
    first = runtime_bundle(host)
    assert apply_runtime(host, first)["result"] == "installed"
    old = inst.read_record("installed.json")
    original = (inst.config / "runtime").resolve()
    candidate = next(a for a in reversed(host[4]) if a[0] == "systemd-run")
    assert f"--property=BindReadOnlyPaths={original.parent}/candidate-root:{inst.config}" in candidate
    from aiops_diagnostics.onboarding.projection import validate_candidate

    validate_candidate(
        inst.config / "production.env", inst.config / "registry/environment.json", "test", runtime=original
    )
    marker = inst.state / "business-evidence"
    marker.write_text("current-state")
    calls = len(host[4])
    assert apply_runtime(host, first)["result"] == "unchanged"
    assert not any(a[0] == "systemd-run" or a[:2] == ("systemctl", "stop") for a in host[4][calls:])
    second = runtime_bundle(host, model="model-two")
    switch, health = inst.switch_runtime, inst.health

    def fail_switch(record):
        switch(record)
        if failure == "runtime-written" and record.get("runtime_sha256") != old["runtime_sha256"]:
            marker.write_text("new-evidence-after-switch")
            raise KeyboardInterrupt()

    def fail_health(version, **kwargs):
        health(version, **kwargs)
        if failure == "candidate" and kwargs.get("port"):
            raise RuntimeError("candidate failure")

    monkeypatch.setattr(inst, "switch_runtime", fail_switch)
    monkeypatch.setattr(inst, "health", fail_health)
    if failure:
        with pytest.raises((RuntimeError, KeyboardInterrupt)):
            apply_runtime(host, second)
        assert (inst.config / "runtime").resolve() == original
        assert inst.read_record("installed.json")["runtime_sha256"] == old["runtime_sha256"]
    else:
        assert apply_runtime(host, second)["result"] == "installed"
        assert (inst.config / "runtime").resolve() != original
    assert marker.read_text() == (
        "new-evidence-after-switch" if failure == "runtime-written" else "current-state"
    )
    assert not (inst.root / "pending.json").exists()
    assert original.is_dir()


@pytest.mark.parametrize("drift", ["member", "pointer", "unrecorded"])
def test_runtime_drift_refuses_without_overwriting_owned_or_unknown_files(host, drift):
    inst = host[0]
    archive = runtime_bundle(host)
    apply_runtime(host, archive)
    pointer = inst.config / "runtime"
    if drift == "member":
        (pointer / "consumer-model.json").write_text("external-change")
    else:
        pointer.unlink()
        if drift == "pointer":
            pointer.symlink_to(inst.root.parent / "unknown")
        else:
            pointer.mkdir()
    with pytest.raises(ValueError):
        apply_runtime(host, archive)
    assert not (inst.root / "pending.json").exists()


@pytest.mark.parametrize("invalid", ["digest", "environment", "member", "protocol", "profile", "consumer"])
def test_runtime_archive_and_semantic_mismatch_refused(host, invalid):
    import io

    from aiops_diagnostics.onboarding.projection import validate_runtime

    inst = host[0]
    archive = runtime_bundle(host)
    sha = install.digest(archive)
    if invalid in {"digest", "environment"}:
        with pytest.raises(ValueError):
            probe.runtime_files(
                archive,
                "0" * 64 if invalid == "digest" else sha,
                "other" if invalid == "environment" else "test",
            )
        return
    files, body = probe.runtime_files(archive, sha, "test")
    name = {
        "member": "consumer-model.json",
        "protocol": "operator-manifest.json",
        "profile": "operator-profiles.json",
        "consumer": "consumer-targets.json",
    }[invalid]
    changed = json.loads(files[name])
    if invalid == "member":
        changed["api_key"] = "different"
    elif invalid == "protocol":
        changed["system_messages"] = ["not approved SOP"]
    elif invalid == "profile":
        changed["profiles"][0]["target"]["environment"] = "other"
    else:
        changed["targets"][0]["tenant_id"] = "other"
    files[name] = json.dumps(changed).encode()
    if invalid != "member":
        body["files"][name] = hashlib.sha256(files[name]).hexdigest()
    files["manifest.json"] = json.dumps(body).encode()
    with tarfile.open(archive, "w") as bundle:
        for key, value in files.items():
            member = tarfile.TarInfo(key)
            member.size = len(value)
            bundle.addfile(member, io.BytesIO(value))
    if invalid == "member":
        with pytest.raises(ValueError, match="member digest"):
            probe.runtime_files(archive, install.digest(archive), "test")
    else:
        snapshot = inst.root.parent / "runtime"
        snapshot.mkdir(mode=0o700)
        for key, value in files.items():
            (snapshot / key).write_bytes(value)
            (snapshot / key).chmod(0o600)
        (snapshot / "runtime.tar").write_bytes(archive.read_bytes())
        (snapshot / "runtime.tar").chmod(0o600)
        row = json.loads(candidate_config(host)[1].read_text())["environments"][0]
        with pytest.raises(ValueError):
            validate_runtime(snapshot, row)
    assert not inst.root.exists()


@pytest.mark.parametrize("drift", ["missing", "unowned"])
def test_pending_runtime_recovery_refuses_changed_application_pointer(host, monkeypatch, drift):
    inst = host[0]
    apply_runtime(host, runtime_bundle(host))
    second = runtime_bundle(host, model="model-two")
    switch, recover = inst.switch_runtime, inst.recover

    def crash(record):
        switch(record)
        raise KeyboardInterrupt()

    monkeypatch.setattr(inst, "switch_runtime", crash)
    monkeypatch.setattr(inst, "recover", lambda: None)
    with pytest.raises(KeyboardInterrupt):
        apply_runtime(host, second)
    monkeypatch.setattr(inst, "recover", recover)
    pointer = inst.root / "current"
    pointer.unlink()
    if drift == "unowned":
        pointer.symlink_to(inst.root.parent / "unknown-release")
    calls = len(host[4])
    with pytest.raises(ValueError, match="current release drift"):
        inst.recover()
    assert (inst.root / "pending.json").exists()
    assert not any(a[:2] == ("systemctl", "stop") for a in host[4][calls:])
    assert not pointer.exists()
    assert pointer.is_symlink() == (drift == "unowned")


def test_runtime_recovery_rechecks_application_pointer_after_stop(host, monkeypatch):
    inst = host[0]
    apply_runtime(host, runtime_bundle(host))
    record = inst.read_record("installed.json")
    inst.record("pending.json", {**record, "previous": record, "phase": "switching"})
    stop = inst.stop

    def changed_after_stop(name):
        stop(name)
        if name == inst.name:
            (inst.root / "current").unlink()
            (inst.root / "current").write_text("external-change-after-stop")

    monkeypatch.setattr(inst, "stop", changed_after_stop)
    with pytest.raises(ValueError, match="current release drift"):
        inst.recover()
    assert (inst.root / "current").read_text() == "external-change-after-stop"
    assert (inst.root / "pending.json").exists()


@pytest.mark.parametrize("refusal", [None, "pending", "drift", "revision"])
def test_fixed_installer_registers_without_restart_or_deployment_input(host, monkeypatch, refusal):
    inst = host[0]
    apply_runtime(host, runtime_bundle(host))
    old = inst.read_record("installed.json")
    inst.revision = old["revision"]
    if refusal == "pending":
        inst.record("pending.json", {**old, "previous": old, "phase": "preparing"})
    elif refusal == "drift":
        (inst.config / "runtime/consumer-model.json").write_text("external")
    elif refusal == "revision":
        inst.revision = "b" * 40
    original = inst.command
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if "environment-runtime-register" in args:
            assert args[:4] == ("runuser", "-u", inst.user, "--")
            descriptor = int(args[-1])
            assert kwargs["pass_fds"] == (descriptor,)
            assert args[-2] == "--receipt-fd"
            return json.dumps(
                dict(
                    environment="test",
                    runtime_sha256=old["runtime_sha256"],
                    result="registered",
                    scope="inactive-runtime-registration-only",
                    activated=False,
                    business_acceptance=False,
                )
            )
        return original(*args)

    monkeypatch.setattr(inst, "command", command)
    if refusal:
        with pytest.raises(ValueError):
            inst.register_runtime()
        assert not any("environment-runtime-register" in c for c in calls)
    else:
        assert inst.register_runtime()["result"] == "registered"
    assert not any(a[:2] in (("systemctl", "stop"), ("systemctl", "start")) for a in calls)


def test_cold_probe_reuses_time_wait_but_refuses_live_listener(host):
    inst, *_ = host
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        inst.port = listener.getsockname()[1]
        with pytest.raises(OSError):
            apply(host)
        assert not (inst.root / "pending.json").exists() and not inst.root.exists()
        with socket.socket() as client:
            client.connect(("127.0.0.1", inst.port))
            connection, _ = listener.accept()
            with connection:
                connection.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
    # 服务端先FIN，原裸bind会被TIME_WAIT误拒；无需sleep或猜测端口释放时间。
    assert apply(host)["result"] == "installed"


def test_apply_flows_selected_interpreter_into_venv(host):
    inst, archive, sha, config, *_ = host
    selected = "/usr/bin/python3"
    resolved = os.path.realpath(selected)
    if re.fullmatch(r"python3(?:\.(?:11|12|13))?", os.path.basename(resolved)):
        selected = resolved
    assert inst.apply(archive, sha, selected, config, install.digest(config))["result"] == "installed"
    venv = next(c for c in reversed(host[4]) if "venv" in c)
    # venv创建只走system_python核验后的解析目标。
    assert venv[venv.index("-i") + 2] == os.path.realpath(selected)


def test_managed_python_selects_local_install_when_system_default_is_old(tmp_path):
    """95形状：/usr/bin/python3是平台旧版，/usr/local/bin的受管安装被选中。"""
    usr_bin = tmp_path / "usr/bin"
    local_bin = tmp_path / "usr/local/bin"
    usr_bin.mkdir(parents=True)
    local_bin.mkdir(parents=True)
    # 不同解析目标区分候选：旧平台链与root维护链各指自己的真实文件。
    new_minor = probe.SUPPORTED_PYTHON_MINORS[-1]
    old = tmp_path / "targets" / "platform-python3.6"
    new = tmp_path / "targets" / "python3.13"
    old.parent.mkdir()
    for target in (old, new):
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
    (usr_bin / "python3").symlink_to(old)
    (local_bin / "python3").symlink_to(new)
    # 小版本探测只发向解析后目标，不是候选路径。
    minors = {os.path.realpath(old): "3.6", os.path.realpath(new): new_minor}
    roots = (str(usr_bin), str(local_bin))

    def run(path, *args):
        return minors[path]

    def check_dir(path, owner):
        resolved = Path(os.path.realpath(path))
        if resolved == tmp_path or resolved in tmp_path.parents:
            return  # 合成容器及其祖先（含1777的/tmp）只模拟挂载点；区内目录实核。
        probe.trusted_directory(resolved, os.getuid())

    kwargs = dict(roots=roots, owner=os.getuid(), run=run, check_dir=check_dir)
    assert probe.managed_python(new_minor, **kwargs) == str(local_bin / "python3")
    other = next(m for m in probe.SUPPORTED_PYTHON_MINORS if m != new_minor)
    with pytest.raises(ValueError, match="no managed interpreter"):
        probe.managed_python(other, **kwargs)
    with pytest.raises(ValueError, match="system Python"):
        probe.system_python(str(usr_bin / "python3.6"), **kwargs)


def test_managed_python_rejects_writable_mismatched_or_foreign_paths(tmp_path):
    root = tmp_path / "usr/local/bin"
    root.mkdir(parents=True)
    fake = root / "python3.13"
    fake.write_text("#!/bin/sh\n")

    def check(_path, _owner):
        return None

    fake.chmod(0o777)
    with pytest.raises(ValueError, match="ownership or mode"):
        probe.system_python(
            str(fake), roots=(str(root),), owner=os.getuid(), run=lambda *a: "3.13", check_dir=check
        )
    fake.chmod(0o755)
    with pytest.raises(ValueError, match="minor mismatch"):
        probe.system_python(
            str(fake), roots=(str(root),), owner=os.getuid(), run=lambda *a: "3.12", check_dir=check
        )
    assert probe.system_python(
        str(fake), roots=(str(root),), owner=os.getuid(), run=lambda *a: "3.13", check_dir=check
    ) == (os.path.realpath(fake), "3.13")
    # 受管候选经符号链接→合成真实目标：返回的是解析后绝对目标而非候选路径。
    # 不探测宿主/usr/bin/python3——其小版本在CI上可能不在受支持范围。
    synthetic = tmp_path / "targets" / "python3.12"
    synthetic.parent.mkdir()
    synthetic.write_text("#!/bin/sh\n")
    synthetic.chmod(0o755)
    link = root / "python3"
    link.symlink_to(synthetic)
    path, minor = probe.system_python(
        str(link), roots=(str(root),), owner=os.getuid(), run=lambda *a: "3.12", check_dir=check
    )
    assert path == os.path.realpath(synthetic) and minor == "3.12"
    with pytest.raises(ValueError, match="system Python"):
        probe.system_python(
            str(tmp_path / "python3"),
            roots=(str(root),),
            owner=os.getuid(),
            run=lambda *a: "3.12",
            check_dir=check,
        )


def test_system_python_executes_only_the_verified_resolved_target(tmp_path):
    """核验后只执行解析后绝对目标：候选链路的可写中间目录不构成重定向面。

    评审反例（parent-hop）：`managed/python3` → `alias/python3`，`alias`本身
    是指向0777的`writable/hop`的目录级符号链接，`hop`再指向受信`final`目录。
    逐hop文本枚举看不到路径组件级链接；改为探测+返回+执行解析后目标后，
    中间跳在核验后无法重定向解释器——但解析目标自身的祖先仍须全部受控。
    """
    root = tmp_path / "managed"
    root.mkdir()
    final = tmp_path / "final"
    final.mkdir()
    target = final / "python3"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    writable = tmp_path / "writable"
    writable.mkdir()
    writable.chmod(0o777)
    # root反例形状：managed/python3 → alias/python3 → writable/hop/python3 → final/python3。
    (writable / "hop").symlink_to(final)
    alias = tmp_path / "alias"
    alias.symlink_to(writable / "hop")
    candidate = root / "python3"
    candidate.symlink_to(alias / "python3")
    resolved = os.path.realpath(candidate)
    assert resolved == os.path.realpath(target)
    probed = []

    def run(path, *args):
        probed.append(path)
        return "3.13"

    def check_dir(path, owner):
        resolved_dir = Path(os.path.realpath(path))
        if resolved_dir == tmp_path or resolved_dir in tmp_path.parents:
            return  # 合成容器祖先不属被测链；区内目录按自身归属实核。
        probe.trusted_directory(resolved_dir, os.getuid())

    # 0777中间跳不再否决：小版本探测与执行都只经过解析后目标，hop不可重定向。
    assert probe.system_python(
        str(candidate), roots=(str(root),), owner=os.getuid(), run=run, check_dir=check_dir
    ) == (resolved, "3.13")
    assert probed == [resolved]
    # 解析目标自身落在可写父目录下仍拒：resolved的全部父目录受核。
    direct = tmp_path / "w" / "bin"
    direct.mkdir(parents=True)
    direct.chmod(0o777)
    exposed = direct / "python3.13"
    exposed.write_text("x")
    exposed.chmod(0o755)
    named = root / "python3.13"
    named.symlink_to(exposed)
    with pytest.raises(ValueError, match="untrusted"):
        probe.system_python(str(named), roots=(str(root),), owner=os.getuid(), run=run, check_dir=check_dir)
    named.unlink()
    # root常规安全链：managed bin内二级符号链接→同一受信目标照常接受。
    named.symlink_to(target)
    candidate.unlink()
    candidate.symlink_to(named)
    probed.clear()
    inner_resolved = os.path.realpath(candidate)
    assert probe.system_python(
        str(candidate), roots=(str(root),), owner=os.getuid(), run=run, check_dir=check_dir
    ) == (inner_resolved, "3.13")
    assert probed == [inner_resolved]
    # 中间跳指向不受管目标仍按目标本身拒绝，不因hop在受管名下放行。
    evil = tmp_path / "evil-bin"
    evil.write_text("#!/bin/sh\n")
    evil.chmod(0o777)
    named.unlink()
    named.symlink_to(evil)
    with pytest.raises(ValueError, match="ownership or mode"):
        probe.system_python(
            str(candidate), roots=(str(root),), owner=os.getuid(), run=run, check_dir=check_dir
        )
    writable_bin = tmp_path / "w2" / "bin"
    writable_bin.mkdir(parents=True)
    writable_bin.chmod(0o777)
    owned = writable_bin / "python3"
    owned.write_text("x")
    owned.chmod(0o755)
    with pytest.raises(ValueError, match="interpreter location"):
        probe.system_python(str(owned), roots=(str(writable_bin),), owner=os.getuid(), run=lambda *a: "3.12")
