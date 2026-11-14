"""隔离真实文件树/SQLite，模拟主机服务和pip，验证升级失败不丢数据或恢复点。"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiops_diagnostics.gateway import gateway_schema

ROOT = Path(__file__).resolve().parents[2]
OLD, NEW = "b" * 40, "a" * 40
STUB = r"""#!/usr/bin/python3
import json, os, subprocess, sys
from pathlib import Path
name=Path(sys.argv[0]).name; args=sys.argv[1:]; root=Path(os.environ['CASE_ROOT'])
with (root/'commands').open('a') as log: log.write(json.dumps([name,*args])+'\n')
if name=='id': print(0)
elif name=='python3' and args[:2]==['-m','venv']:
    target=Path(args[2])/'bin'; target.mkdir(parents=True)
    import shlex
    (target/'python').write_text('#!/bin/sh\nexec '+shlex.quote(os.environ['CASE_PYTHON'])+' "$@"\n')
    (target/'python').chmod(0o700)
    (target/'pip').write_text('#!/bin/sh\nexit 0\n'); (target/'pip').chmod(0o700)
elif name=='python3':
    sys.exit(subprocess.run([os.environ['CASE_PYTHON'],*args]).returncode)
elif name=='systemctl':
    if args[0]=='stop': (root/'active').unlink(missing_ok=True)
    if args[0]=='start': (root/'active').touch()
elif name=='sleep': pass
elif name=='curl':
    revision=(root/'opt/aiops-95/current/REVISION').read_text().strip()
    if os.environ.get('FAIL_HEALTH') and revision=='a'*40: sys.exit(22)
    print(json.dumps({'ok':True,'business_mutations':'disabled','version':'test+'+revision[:12]}))
else: raise RuntimeError(name)
"""


def fixture_host(tmp_path):
    root = tmp_path
    bindir = root / "bin"
    bindir.mkdir()
    (bindir / "stub").write_text(STUB)
    (bindir / "stub").chmod(0o700)
    for name in ("id", "python3", "systemctl", "curl", "sleep"):
        (bindir / name).symlink_to("stub")
    for name in ("etc/aiops-95", "etc/systemd/system", "var/lib/aiops-95/gateway", "run/lock"):
        (root / name).mkdir(parents=True)
    old = root / f"opt/aiops-95/releases/{OLD}"
    old.mkdir(parents=True)
    (old / "REVISION").write_text(OLD)
    (root / "opt/aiops-95/current").symlink_to(old)
    state = root / "var/lib/aiops-95"
    (state / "installed-revision").write_text(OLD)
    (state / "installed-backup").write_text("original-recovery-point")
    (state / "business-evidence").write_text("must survive")
    database = state / "gateway/gateway.db"
    gateway_schema.startup(database)
    (root / "etc/aiops-95/gateway.env").write_text(
        "AIOPS_GATEWAY_ENVIRONMENT=95\n"
        f"AIOPS_GATEWAY_DATABASE_FILE={database}\n"
        f"AIOPS_GATEWAY_SERVER_CONFIG_FILE={root}/etc/aiops-95/server.env\n"
    )
    (root / "etc/aiops-95/server.env").write_text("# fixture\n")
    (root / "etc/aiops-95/server.env").chmod(0o600)
    bundle = root / "bundle"
    scripts = bundle / "ops/environments/env95"
    scripts.mkdir(parents=True)
    (bundle / "wheelhouse").mkdir()
    (bundle / "wheelhouse/aiops_diagnostics-test.whl").write_text("fake wheel: pip is simulated")
    (bundle / "REVISION").write_text(NEW)
    for name in ("install.sh", "upgrade.sh", "aiops-gateway-95.service"):
        text = (ROOT / "ops/environments/env95" / name).read_text()
        for prefix in ("/etc/", "/opt/", "/var/", "/home/aiops/", "/run/lock/"):
            text = text.replace(prefix, str(root) + prefix)
        (scripts / name).write_text(text)
    (root / "etc/systemd/system/aiops-gateway-95.service").write_bytes(
        (scripts / "aiops-gateway-95.service").read_bytes()
    )
    (bundle / "SHA256SUMS").write_text(
        "".join(
            hashlib.sha256(p.read_bytes()).hexdigest() + "  " + str(p.relative_to(bundle)) + "\n"
            for p in sorted(bundle.rglob("*"))
            if p.is_file()
        )
    )
    (root / "active").touch()
    return bundle, database


@pytest.mark.parametrize(
    "failure", ["", "health", "schema", "symlink", "artifact", "inflight", "trial", "health-job"]
)
def test_upgrade_preserves_state_and_rolls_back_failed_candidate(tmp_path, failure):
    import sqlite3

    bundle, database = fixture_host(tmp_path)
    if failure in {"trial", "health-job"}:
        from aiops_diagnostics.gateway.gateway_store import GatewayStore

        store = GatewayStore(database)
        if failure == "trial":
            row = store.create_trial_job(
                "scope", order_no="synthetic", draft_fingerprint="fp", initiator="qa", workflow_trial=True
            )
            store.update_standard_diagnosis(row["diagnosis_id"], status="running")
        else:
            store.create_or_reuse_health_job("scope", "synthetic", "v1")
    if failure == "schema":
        with sqlite3.connect(database) as db:
            db.execute("PRAGMA user_version=999")
    if failure == "inflight":
        with sqlite3.connect(database) as db:
            db.execute(
                "INSERT INTO assistant_questions "
                "(qa_id,scope_fingerprint,question,status,created_at,updated_at,deadline_at) "
                "VALUES ('q','scope','question','running','now','now','later')"
            )
    if failure == "symlink":
        (tmp_path / "var/backups/aiops-95").mkdir(parents=True)
        (tmp_path / "unrelated").mkdir()
        (tmp_path / "var/backups/aiops-95/upgrades").symlink_to(tmp_path / "unrelated")
    if failure == "artifact":
        (bundle / "unlisted").write_text("must not publish")
    before = database.read_bytes()
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIOPS_")}
    env.update(
        PATH=str(tmp_path / "bin") + ":/usr/bin:/bin",
        CASE_ROOT=str(tmp_path),
        CASE_PYTHON=sys.executable,
        PYTHONPATH=str(ROOT / "src"),
        FAIL_HEALTH="1" if failure == "health" else "",
    )
    result = subprocess.run(
        ["bash", str(bundle / "ops/environments/env95/upgrade.sh"), str(bundle)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode == 0) == (not failure), result.stderr
    current = tmp_path / "opt/aiops-95/current"
    assert (current / "REVISION").read_text() == (OLD if failure else NEW)
    assert database.read_bytes() == before
    state = tmp_path / "var/lib/aiops-95"
    assert (state / "installed-revision").read_text() == OLD
    assert (state / "installed-backup").read_text() == "original-recovery-point"
    assert (state / "business-evidence").read_text() == "must survive"
    assert (tmp_path / "active").exists()
    if failure == "symlink":
        assert not list((tmp_path / "unrelated").iterdir())
    commands = (tmp_path / "commands").read_text()
    if failure in {"schema", "symlink", "artifact", "inflight", "trial", "health-job"}:
        assert '"systemctl", "stop"' not in commands
    if failure == "health":
        assert '"systemctl", "start"' in commands
