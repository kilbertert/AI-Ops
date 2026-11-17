"""真实私有文件/SQLite配合主机命令替身，证明重复、失败与拒绝不丢状态。"""

import importlib.util
import json
import os
import runpy
import socket
import subprocess
import sys
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
