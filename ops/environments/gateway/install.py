"""自有Gateway独立安装、同配置升级与中断恢复；只管理aiops-access-<环境>。"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import pwd
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

import probe


def digest(path):
    with probe.regular(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def trusted(path, *, owner=0, private=False):
    if (
        path.is_symlink()
        or path.stat().st_uid != owner
        or path.stat().st_mode & (0o077 if private else 0o022)
    ):
        raise ValueError("untrusted path ownership or mode")


def write(path, body):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(body.encode())
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Installer:
    def __init__(self, environment, user, port):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}[a-z0-9]|[a-z0-9]", environment):
            raise ValueError("invalid environment")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or not 1024 <= port <= 65535:
            raise ValueError("invalid service identity or port")
        account = pwd.getpwnam(user)
        if account.pw_uid == 0 or account.pw_shell not in (
            "/usr/sbin/nologin",
            "/sbin/nologin",
            "/bin/false",
        ):
            raise ValueError("existing non-login identity required")
        self.environment, self.user, self.port, self.account = environment, user, port, account
        self.name = "aiops-access-" + environment
        self.root = Path("/opt") / self.name
        self.state = Path("/var/lib") / self.name
        self.config = Path("/etc") / self.name
        self.unit = Path("/etc/systemd/system") / (self.name + ".service")
        self.backups = Path("/var/backups") / self.name

    def command(self, *args, **kwargs):
        return probe.command(*args, **kwargs)

    def environment_args(self, state, config=None):
        return [
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            "PYTHONNOUSERSITE=1",
            f"AIOPS_HOME={state}",
            f"AIOPS_DATA_HOME={state}",
            f"AIOPS_CONFIG_HOME={state}/config",
            f"AIOPS_CODEX_KEY_DIR={state}/keys",
            f"AIOPS_AGENT_RUN_ROOT={state}/runs",
            f"AIOPS_GATEWAY_DATABASE_FILE={state}/gateway/gateway.db",
            f"AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE={state}/gateway/gateway.db",
            f"AIOPS_GATEWAY_CHAT_DIFY_KEY_DIR={state}/keys",
            f"AIOPS_GATEWAY_SERVER_CONFIG_FILE={config or self.config / 'production.env'}",
            f"AIOPS_GATEWAY_ENVIRONMENT={self.environment}",
        ]

    def unit_text(self):
        command = " ".join(
            [
                *self.environment_args(self.state),
                str(self.root / "current/venv/bin/aiops-gateway"),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ]
        )
        return f"""[Unit]
Description=AI-Ops managed access {self.environment}
After=network-online.target
[Service]
User={self.user}
Group={self.account.pw_gid}
WorkingDirectory={self.root}/current
ExecStart={command}
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={self.state}
MemoryMax=384M
CPUQuota=50%
TasksMax=64
[Install]
WantedBy=multi-user.target
"""

    def health(self, version, *, port=None, wait=30):
        deadline = time.monotonic() + wait
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            try:
                name = self.name + "-candidate" if port else self.name
                self.command("systemctl", "is-active", "--quiet", name)
                pid = int(self.command("systemctl", "show", name, "-p", "MainPID", "--value"))
                if port:
                    if os.readlink(f"/proc/{pid}/ns/net") == os.readlink("/proc/self/ns/net"):
                        raise RuntimeError("candidate network isolation missing")
                    code, body = probe.isolated_http(pid, port, "/health")
                    if code != 200:
                        raise ValueError("candidate health refused")
                else:
                    with opener.open(f"http://127.0.0.1:{self.port}/health", timeout=2) as response:
                        body = json.load(response)
                owned = set()
                for descriptor in Path(f"/proc/{pid}/fd").iterdir():
                    try:
                        owned.add(descriptor.readlink().name)
                    except FileNotFoundError:
                        # 枚举后普通连接可能关闭；仍须下面的监听socket归属匹配。
                        continue
                listeners = {
                    "socket:[" + row.split()[9] + "]"
                    for row in Path(f"/proc/{pid}/net/tcp").read_text().splitlines()[1:]
                    if row.split()[1] == f"0100007F:{port or self.port:04X}" and row.split()[3] == "0A"
                }
                if (
                    body.get("version") == version
                    and body.get("business_mutations") == "disabled"
                    and pid
                    and listeners & owned
                ):
                    return
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("health identity not confirmed")
            time.sleep(0.5)

    def stop(self, name):
        if self.command("systemctl", "show", name, "-p", "LoadState", "--value") == "not-found":
            return
        self.command("systemctl", "stop", name)
        status = self.command("systemctl", "show", name, "-p", "MainPID", "-p", "LoadState")
        if "MainPID=0" not in status and "LoadState=not-found" not in status:
            raise RuntimeError("service stop unconfirmed")

    def switch(self, release):
        current = self.root / "current"
        temp = self.root / "current.next"
        if temp.exists() or temp.is_symlink():
            temp.unlink()
        temp.symlink_to(release)
        temp.replace(current)
        descriptor = os.open(self.root, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def check_current(self, old, pending):
        records = [old, pending] if pending["phase"] == "switching" else [old]
        current = self.root / "current"
        if not current.is_symlink() or Path(os.readlink(current)) not in {
            self.release(record) for record in records
        }:
            raise ValueError("current release drift prevents rollback")

    def read_record(self, name):
        self.check_paths()
        path = self.root / name
        trusted(path, private=True)
        value = json.loads(path.read_text())
        for key, expected in (("environment", self.environment), ("user", self.user), ("port", self.port)):
            if value[key] != expected:
                raise ValueError("installation identity mismatch")
        return value

    def check_paths(self):
        for path in (self.root, self.root / "releases", self.config):
            if path.exists() or path.is_symlink():
                trusted(path)
        if self.state.exists() or self.state.is_symlink():
            trusted(self.state, owner=self.account.pw_uid, private=True)
        if self.unit.exists() or self.unit.is_symlink():
            trusted(self.unit)
            if self.unit.read_text() != self.unit_text():
                raise ValueError("unit ownership drift")
            if self.command("systemctl", "show", self.name, "-p", "DropInPaths", "--value"):
                raise ValueError("unit overrides require separate delivery")
        path = self.config / "production.env"
        if path.exists() or path.is_symlink():
            trusted(path, owner=self.account.pw_uid, private=True)
        directory = self.config / "registry"
        if directory.exists() or directory.is_symlink():
            trusted(directory, owner=self.account.pw_uid, private=True)
            for member in directory.iterdir():
                if member.name not in {"environment.json", "environment.json.lock"}:
                    raise ValueError("unknown registry file")
                trusted(member, owner=self.account.pw_uid, private=True)
                if not member.is_file() or member.stat().st_nlink != 1:
                    raise ValueError("registry file must be a regular single-link file")

    def registry_digest(self):
        self.check_paths()
        path = self.config / "registry/environment.json"
        return digest(path) if path.exists() else "absent"

    def check_registry(self, old, pending=None):
        allowed = {old.get("registry_sha256", "absent")}
        if pending is not None:
            allowed.add(pending.get("registry_sha256", "absent"))
            if "recovery_registry_sha256" in pending:
                allowed.add(pending["recovery_registry_sha256"])
        if self.registry_digest() not in allowed:
            raise ValueError("registry configuration drift")

    def runtime_path(self, record):
        release = record["runtime_release"]
        if not re.fullmatch(r"[a-f0-9]{12}-[a-z0-9_]+", release):
            raise ValueError("invalid runtime release")
        return self.root / "releases" / release / "runtime"

    def check_runtime(self, old, pending=None):
        pointer = self.config / "runtime"
        allowed = [r for r in (old, pending) if r is not None]
        paths = {self.runtime_path(r) for r in allowed if r.get("runtime_sha256")}
        if pointer.is_symlink():
            if Path(os.readlink(pointer)) not in paths:
                raise ValueError("runtime pointer drift")
        elif pointer.exists() or all(r.get("runtime_sha256") for r in allowed):
            raise ValueError("runtime pointer missing or unowned")
        for record in allowed:
            if not record.get("runtime_sha256"):
                continue
            root = self.runtime_path(record)
            trusted(root.parent)
            trusted(root, owner=self.account.pw_uid, private=True)
            trusted(root / "runtime.tar", owner=self.account.pw_uid, private=True)
            files, _ = probe.runtime_files(root / "runtime.tar", record["runtime_sha256"], self.environment)
            if {p.name for p in root.iterdir()} != set(files) | {"runtime.tar"}:
                raise ValueError("runtime snapshot file set drift")
            for name, raw in files.items():
                trusted(root / name, owner=self.account.pw_uid, private=True)
                if (root / name).stat().st_nlink != 1 or (root / name).read_bytes() != raw:
                    raise ValueError("runtime snapshot drift")

    def switch_runtime(self, record):
        pointer = self.config / "runtime"
        if record.get("runtime_sha256"):
            temporary = self.config / "runtime.next"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(self.runtime_path(record))
            temporary.replace(pointer)
        else:
            pointer.unlink(missing_ok=True)
        descriptor = os.open(self.config, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def registry_snapshot(self, source, target, expected):
        if not target.parent.exists():
            target.parent.mkdir(mode=0o700)
            os.chown(target.parent, self.account.pw_uid, self.account.pw_gid)
        trusted(target.parent, owner=self.account.pw_uid, private=True)
        self.config_snapshot(source, target, expected)

    def recovery_registry(self, old, pending):
        release = self.release(pending)
        target = release / "recovery-registry/environment.json"
        if "recovery_registry_sha256" in pending:
            trusted(target, owner=self.account.pw_uid, private=True)
            if digest(target) != pending["recovery_registry_sha256"]:
                raise ValueError("recovery registry drift")
        else:
            original = release / "previous-registry/environment.json"
            if digest(original) != old["registry_sha256"]:
                raise ValueError("previous registry drift")
            if digest(release / "candidate-registry/environment.json") != pending["registry_sha256"]:
                raise ValueError("candidate registry drift prevents recovery")
            body = json.loads(original.read_text())
            observed = json.loads((release / "candidate-registry/environment.json").read_text())
            body["audit"] = observed["audit"] + [
                {
                    "revision": observed["revision"] + 1,
                    "actor": "installation-recovery",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                }
            ]
            body["revision"] = observed["revision"] + 1
            raw = json.dumps(body, sort_keys=True).encode() + b"\n"
            self.registry_snapshot_bytes(raw, target)
            pending["recovery_registry_sha256"] = digest(target)
            self.record("pending.json", pending)
        self.registry_snapshot(
            target, self.config / "registry/environment.json", pending["recovery_registry_sha256"]
        )
        return {**old, "registry_sha256": pending["recovery_registry_sha256"]}

    def registry_snapshot_bytes(self, raw, target):
        if not target.parent.exists():
            target.parent.mkdir(mode=0o700)
            os.chown(target.parent, self.account.pw_uid, self.account.pw_gid)
        self.config_snapshot_bytes(raw, target)

    def candidate_directory(self, incoming):
        """草稿投影不是现役版本；安装本地版本递增，相同内容复用原字节。"""
        body = json.loads(incoming)
        if (self.root / "installed.json").exists():
            old = self.read_record("installed.json")
            self.check_registry(old)
            if "registry_sha256" in old:
                raw = (self.config / "registry/environment.json").read_bytes()
                current = json.loads(raw)
                active = current["environments"][0]
                proposed = body["environments"][0]
                if active["enabled"] or any(t["enabled"] for t in active["tenants"]):
                    for key in ("id", "gateway_url", "cpms_base_url", "company_gateway_url", "config_ref"):
                        if active[key] != proposed[key]:
                            raise ValueError("active environment connection cannot change during onboarding")
                retained = {t["id"]: t for t in active["tenants"]}
                for tenant in proposed["tenants"]:
                    if tenant["id"] not in retained:
                        if tenant["enabled"]:
                            raise ValueError("new candidate cannot grant admission")
                        retained[tenant["id"]] = tenant
                proposed["tenants"] = list(retained.values())
                proposed["enabled"] = active["enabled"]
                body["managers"] = current["managers"]
                if (
                    body["environments"] == current["environments"]
                    and body["managers"] == current["managers"]
                ):
                    return raw
                body["revision"] = current["revision"] + 1
                body["audit"] = current["audit"] + [
                    {
                        "revision": body["revision"],
                        "actor": "candidate-installation",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                    }
                ]
        return json.dumps(body, sort_keys=True).encode() + b"\n"

    def candidate_configuration(self, incoming):
        """在用环境保留现行开关；任何其他配置变化须独立维护窗口。"""
        if not (self.root / "installed.json").exists():
            return incoming
        old = self.read_record("installed.json")
        self.check_registry(old)
        if not old.get("registry_sha256"):
            return incoming
        current = json.loads((self.config / "registry/environment.json").read_text())["environments"][0]
        if not current["enabled"] and not any(t["enabled"] for t in current["tenants"]):
            return incoming
        actual = (self.config / "production.env").read_bytes()
        if hashlib.sha256(actual).hexdigest() != old["config_sha256"]:
            raise ValueError("active configuration drift")

        # 投影是固定排序的env；只忽略由当前准入保留的两个运行开关。
        def without_switches(raw):
            return b"\n".join(
                line
                for line in raw.splitlines()
                if not line.startswith(
                    (
                        b"AIOPS_GATEWAY_CONSUMER_CHATFLOW_ENABLED=",
                        b"AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED=",
                    )
                )
            )

        if sorted(without_switches(incoming).splitlines()) != sorted(without_switches(actual).splitlines()):
            raise ValueError("active configuration change requires separate window")
        return actual

    def candidate_runtime(self, incoming, incoming_sha, destination):
        old = self.read_record("installed.json") if (self.root / "installed.json").exists() else None
        if not old or not old.get("runtime_sha256"):
            return incoming, incoming_sha
        self.check_runtime(old)
        _, before = probe.runtime_files(
            self.runtime_path(old) / "runtime.tar", old["runtime_sha256"], self.environment
        )
        _, proposed = probe.runtime_files(incoming, incoming_sha, self.environment)
        if incoming_sha == old["runtime_sha256"]:
            return incoming, incoming_sha
        # 原单租户停用安装仍可更新自身配置；只给新增租户/已组合快照增加保留约束。
        if before["schema"] == "aiops-environment-runtime/1" and before["tenant_id"] == proposed["tenant_id"]:
            return incoming, incoming_sha
        # 已开通租户的运行行不得被候选顶替；未开通行是已消费的候选身份，允许重铸替换。
        # 目录不可核验时置空集合，退回全量拒绝。
        self.check_registry(old)
        replaceable = set()
        shared_update = False
        if old.get("registry_sha256"):
            current = json.loads((self.config / "registry/environment.json").read_text())["environments"][0]
            replaceable = {row["id"] for row in current["tenants"] if row.get("enabled") is False}
            # 与candidate_configuration同一停用判据：环境与全部既有租户停用时，
            # 本次安装即共享接线的配置维护窗口；键缺失按在用处理（fail-closed）。
            shared_update = current.get("enabled") is False and all(
                row.get("enabled") is False for row in current["tenants"]
            )
        raw, sha = probe.compose_runtime(
            self.runtime_path(old) / "runtime.tar",
            old["runtime_sha256"],
            incoming,
            incoming_sha,
            self.environment,
            replaceable_tenants=replaceable,
            shared_update=shared_update,
        )
        with destination.open("xb") as stream:
            stream.write(raw)
        destination.chmod(0o600)
        return destination, sha

    def release(self, record):
        if not re.fullmatch(r"[a-f0-9]{12}-[a-z0-9_]+", record["release"]):
            raise ValueError("invalid recorded release")
        path = self.root / "releases" / record["release"]
        trusted(path)
        if (
            probe.manifest(path / "artifact.tar.gz", record["artifact_sha256"], record["python"])["version"]
            != record["version"]
        ):
            raise ValueError("recorded artifact drift")
        return path

    def record(self, name, value):
        write(self.root / name, json.dumps(value) + "\n")

    def config_snapshot(self, source, target, expected):
        body = probe.regular(source).read_bytes()
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError("configuration snapshot mismatch")
        self.config_snapshot_bytes(body, target)

    def config_snapshot_bytes(self, body, target):
        staging = self.root / "configuration-staging"
        staging.mkdir(mode=0o700, exist_ok=True)
        trusted(staging, private=True)
        if staging.stat().st_dev != target.parent.stat().st_dev:
            raise ValueError("configuration snapshot requires one filesystem")
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as out:
            temporary = Path(out.name)
            out.write(body)
            out.flush()
            os.fchown(out.fileno(), self.account.pw_uid, self.account.pw_gid)
            os.fsync(out.fileno())
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        descriptor = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _run_runtime_check(self, release, mode, *extra):
        raw = self.command(
            "runuser",
            "-u",
            self.user,
            "--",
            *self.environment_args(self.state),
            str(release / "venv/bin/python"),
            "-I",
            str(release / "runtime_check.py"),
            str(self.config / "production.env"),
            str(self.state),
            self.environment,
            mode,
            *extra,
        )
        text = (raw or "").strip()
        if not text:
            return None
        return json.loads(text)

    def check_database(self, release):
        mode = getattr(self, "_database_check_mode", "existing")
        return self._run_runtime_check(release, mode)

    def _check_database_for_upgrade(self, release):
        previous = getattr(self, "_database_check_mode", "existing")
        self._database_check_mode = "upgrade-check"
        try:
            return self.check_database(release)
        finally:
            self._database_check_mode = previous

    def rehearse_schema_upgrade(self, release, trial):
        backup = trial / "pre-schema-rehearsal.db"
        result = self._run_runtime_check(release, "rehearse", str(backup))
        if not isinstance(result, dict) or not result.get("rehearsed"):
            raise ValueError("schema upgrade rehearsal failed")
        return result

    def backup_schema_database(self, release):
        staging = release / "schema-staging"
        if staging.exists() or staging.is_symlink():
            raise ValueError("schema staging already exists")
        staging.mkdir(mode=0o700)
        os.chown(staging, self.account.pw_uid, self.account.pw_gid)
        staged = staging / "pre-schema-gateway.db"
        try:
            result = self._run_runtime_check(release, "backup", str(staged))
            if not isinstance(result, dict) or not isinstance(result.get("backup_sha256"), str):
                raise ValueError("schema backup failed")
            expected_sha = result["backup_sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                raise ValueError("invalid schema backup digest")
            trusted(staged, owner=self.account.pw_uid, private=True)
            if digest(staged) != expected_sha:
                raise ValueError("schema backup digest mismatch")
            target = release / "pre-schema-gateway.db"
            if target.exists() or target.is_symlink():
                raise ValueError("schema backup target already exists")
            with tempfile.NamedTemporaryFile(dir=release, delete=False) as out:
                temporary = Path(out.name)
                with staged.open("rb") as src:
                    shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            temporary.chmod(0o600)
            temporary.replace(target)
            descriptor = os.open(release, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            trusted(target, private=True)
            if digest(target) != expected_sha:
                raise ValueError("retained schema backup digest mismatch")
            return expected_sha
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def migrate_schema_database(self, release, expected_to_version):
        result = self._run_runtime_check(release, "migrate")
        if not isinstance(result, dict) or result.get("to_version") != expected_to_version:
            raise ValueError("schema migration did not reach expected version")
        return result

    def restore_schema_database(self, pending):
        info = pending.get("schema_upgrade")
        if info is None:
            return
        if (
            not isinstance(info, dict)
            or set(info) != {"from_version", "to_version", "backup_sha256"}
            or type(info["from_version"]) is not int
            or type(info["to_version"]) is not int
            or not (0 <= info["from_version"] < info["to_version"])
            or not isinstance(info["backup_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", info["backup_sha256"])
        ):
            raise ValueError("invalid pending schema upgrade record")
        pending_release = self.release(pending)
        observed = self._run_runtime_check(pending_release, "version")
        if not isinstance(observed, dict) or type(observed.get("version")) is not int:
            raise ValueError("unable to observe database schema version for rollback")
        actual_version = observed["version"]
        if actual_version == info["from_version"]:
            return
        if actual_version != info["to_version"]:
            raise ValueError("schema drift prevents rollback")
        backup = pending_release / "pre-schema-gateway.db"
        trusted(backup, private=True)
        if digest(backup) != info["backup_sha256"]:
            raise ValueError("schema rollback backup digest mismatch")
        db_dir = self.state / "gateway"
        database = db_dir / "gateway.db"
        if db_dir.is_symlink() or not db_dir.is_dir():
            raise ValueError("invalid gateway database directory")
        probe.regular(database)
        failed_dir = pending_release / f"failed-schema-{uuid.uuid4().hex[:8]}"
        failed_dir.mkdir(mode=0o700)
        for suffix in ("", "-wal", "-shm"):
            sidecar = db_dir / ("gateway.db" + suffix)
            if sidecar.is_symlink():
                raise ValueError("database sidecar symlink refused")
            if sidecar.exists():
                shutil.copy2(sidecar, failed_dir / sidecar.name)
                (failed_dir / sidecar.name).chmod(0o600)
        with tempfile.NamedTemporaryFile(dir=db_dir, delete=False) as out:
            temporary = Path(out.name)
            with backup.open("rb") as src:
                shutil.copyfileobj(src, out)
            out.flush()
            os.fchown(out.fileno(), self.account.pw_uid, self.account.pw_gid)
            os.fchmod(out.fileno(), 0o600)
            os.fsync(out.fileno())
        try:
            temporary.replace(database)
        finally:
            temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            sidecar = db_dir / ("gateway.db" + suffix)
            if sidecar.exists():
                sidecar.unlink()
        descriptor = os.open(db_dir, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def site_observation(self):
        """仅检查本安装器自有实例；网络/业务验收不是本机就绪检查。"""
        installed = self.read_record("installed.json")
        checks, observed = [], {"installed": installed}
        unit_present = self.unit.is_file()
        observed["unit"] = digest(self.unit) if unit_present else "absent"
        checks.append({"id": "local-path-identity", "state": "passed" if unit_present else "blocked"})
        for key, operation in (
            ("installed-artifact", lambda: self.release(installed)),
            ("configuration", lambda: digest(self.config / "production.env")),
            ("registry", self.registry_digest),
        ):
            try:
                value = operation()
                if key == "installed-artifact":
                    matched = (self.root / "current").resolve() == value
                    observed[key] = str(value)
                else:
                    if key == "configuration":
                        self.check_runtime(installed)
                        observed["runtime_sha256"] = installed.get("runtime_sha256", "absent")
                    expected = (
                        installed["config_sha256"]
                        if key == "configuration"
                        else installed.get("registry_sha256", "absent")
                    )
                    matched = value == expected
                    observed[key] = value
                checks.append({"id": key, "state": "passed" if matched else "blocked"})
            except (OSError, ValueError, RuntimeError, tarfile.TarError, EOFError, zlib.error):
                observed[key] = "unavailable"
                checks.append({"id": key, "state": "blocked"})
        pending = self.root / "pending.json"
        observed["recovery"] = digest(pending) if pending.exists() else "none"
        checks.append({"id": "recovery", "state": "blocked" if pending.exists() else "passed"})
        for key, operation in (
            ("service-readiness", lambda: self.health(installed["version"], wait=0)),
            ("schema-inflight", lambda: self.check_database(self.release(installed))),
        ):
            try:
                operation()
                checks.append({"id": key, "state": "passed"})
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.SubprocessError,
                tarfile.TarError,
                EOFError,
                zlib.error,
            ):
                checks.append({"id": key, "state": "blocked"})
        observed["process"] = self.command(
            "systemctl", "show", self.name, "-p", "MainPID", "-p", "ActiveEnterTimestampMonotonic"
        )
        available = int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())[1]) * 1024
        free = min(
            shutil.disk_usage(p.parent).free for p in (self.root, self.state, self.config, self.backups)
        )
        checks.append(
            {"id": "resource-headroom", "state": "passed" if min(available, free) >= 1024**3 else "blocked"}
        )
        checks += [
            {"id": key, "state": "pending"} for key in ("managed-host-identity", "formal-network", "business")
        ]
        public = {
            "scope": "installed-instance-readiness-only",
            "environment": self.environment,
            "version": installed["version"],
            "source_revision": installed["revision"],
            "artifact_sha256": installed["artifact_sha256"],
            "config_sha256": installed["config_sha256"],
            "registry_sha256": installed.get("registry_sha256", "absent"),
            "runtime_sha256": installed.get("runtime_sha256", "absent"),
            "checks": checks,
        }
        fingerprint = hashlib.sha256(
            json.dumps({"observed": observed, "public": public}, sort_keys=True).encode()
        ).hexdigest()
        return fingerprint, public, {"available_memory_bytes": available, "available_disk_bytes": free}

    def register_runtime(self, *, observe_only=False):
        installed = self.read_record("installed.json")
        if (self.root / "pending.json").exists() or not installed.get("runtime_sha256"):
            raise ValueError("installed runtime required without pending change")
        if installed["revision"] != self.revision:
            raise ValueError("registration installer and application revisions differ")
        self.check_registry(installed)
        self.check_runtime(installed)
        if digest(self.config / "production.env") != installed["config_sha256"]:
            raise ValueError("configuration drift prevents registration")
        release = self.release(installed)
        if (self.root / "current").resolve() != release:
            raise ValueError("current release drift prevents registration")
        self.health(installed["version"], wait=0)
        self.check_database(release)
        with (self.root / "installed.json").open("rb") as receipt:
            result = json.loads(
                self.command(
                    "runuser",
                    "-u",
                    self.user,
                    "--",
                    *self.environment_args(self.state),
                    str(release / "venv/bin/aiops-admin"),
                    "environment-runtime-register",
                    "--sha256",
                    installed["runtime_sha256"],
                    "--receipt-fd",
                    str(receipt.fileno()),
                    *(["--observe-only"] if observe_only else []),
                    pass_fds=(receipt.fileno(),),
                )
            )
        if (
            result.get("environment") != self.environment
            or result.get("runtime_sha256") != installed["runtime_sha256"]
            or result.get("result")
            not in ({"unchanged", "absent"} if observe_only else {"registered", "unchanged"})
            or result.get("scope") != "inactive-runtime-registration-only"
            or result.get("activated") is not False
            or result.get("business_acceptance") is not False
        ):
            raise ValueError("runtime registration receipt invalid")
        return result

    def preflight(self, receipt=None):
        fingerprint, public, resources = self.site_observation()
        directory = self.root / "site-preflight"
        now = time.time()
        state = "blocked" if any(c["state"] == "blocked" for c in public["checks"]) else "observed"

        def stored(path):
            trusted(path, private=True)
            if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 65536:
                raise ValueError("invalid site receipt file")
            row = json.loads(path.read_text())
            integrity = row.pop("integrity_sha256", "")
            if hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest() != integrity:
                raise ValueError("site receipt integrity mismatch")
            result = row["result"]
            if (
                result["environment"] != self.environment
                or result["id"] + ".json" != path.name
                or result["executable"] is not False
                or result["scope"] != "installed-instance-readiness-only"
                or result["state"] not in {"observed", "blocked"}
                or not all(
                    type(result[key]) in (int, float) and math.isfinite(result[key])
                    for key in ("created_at", "expires_at")
                )
                or result["expires_at"] != row["expires_at"]
                or result["expires_at"] - result["created_at"] != 900
            ):
                raise ValueError("site receipt identity mismatch")
            if row["fingerprint"] == fingerprint and any(
                result.get(key) != value for key, value in public.items()
            ):
                raise ValueError("site receipt observation mismatch")
            return row

        if receipt is not None:
            if not re.fullmatch(r"site_[0-9a-f]{32}", receipt):
                raise ValueError("invalid site receipt")
            trusted(directory, private=True)
            path = directory / (receipt + ".json")
            row = stored(path)
            current = row["fingerprint"] == fingerprint and row["expires_at"] > now
            return {**row["result"], "state": state if current else "expired", "executable": False}
        directory.mkdir(mode=0o700, exist_ok=True)
        trusted(directory, private=True)
        records = list(directory.glob("site_*.json"))
        if len(records) > 4096:
            raise ValueError("site receipt archive requires operator review")
        for path in records:
            if not re.fullmatch(r"site_[0-9a-f]{32}\.json", path.name):
                raise ValueError("invalid site record filename")
            row = stored(path)
            if row["fingerprint"] == fingerprint and row["expires_at"] > now:
                return row["result"]
        identifier = "site_" + hashlib.sha256((fingerprint + str(now)).encode()).hexdigest()[:32]
        result = {
            "id": identifier,
            **public,
            "state": state,
            "executable": False,
            "created_at": now,
            "expires_at": now + 900,
            "resources": resources,
        }
        row = {"fingerprint": fingerprint, "expires_at": now + 900, "result": result}
        row["integrity_sha256"] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        write(directory / (identifier + ".json"), json.dumps(row) + "\n")
        return result

    def recover(self):
        pending = self.read_record("pending.json")
        if pending.get("mode") == "publication":
            import publication

            return publication.recover(self, pending["operation"])
        if pending["phase"] not in ("preparing", "switching"):
            raise ValueError("unknown recovery phase")
        old = pending["previous"]
        if old:
            self.check_current(old, pending)
            allowed = {old["config_sha256"]}
            if pending["phase"] == "switching":
                allowed.add(pending["config_sha256"])
            if digest(self.config / "production.env") not in allowed:
                raise ValueError("configuration drift prevents rollback")
            self.check_registry(old, pending if pending["phase"] == "switching" else None)
            self.check_runtime(old, pending if pending["phase"] == "switching" else None)
        self.stop(self.name + "-candidate")
        if pending.get("trial"):
            if not re.fullmatch(r"trial-[a-f0-9]{32}", pending["trial"]):
                raise ValueError("invalid recorded trial")
            trial = self.state / pending["trial"]
            if trial.is_symlink():
                raise ValueError("trial symlink refused")
            if trial.exists():
                shutil.rmtree(trial)
        if pending["phase"] == "switching":
            self.stop(self.name)
        if old is None:
            if self.unit.exists():
                self.command("systemctl", "disable", self.name)
                self.unit.unlink()
                self.command("systemctl", "daemon-reload")
            backup = self.backups
            if backup.exists():
                trusted(backup, private=True)
            backup.mkdir(mode=0o700, exist_ok=True)
            if any(
                p.exists() and p.stat().st_dev != backup.stat().st_dev
                for p in (self.root, self.state, self.config)
            ):
                raise ValueError("cross-filesystem recovery requires separate plan")
            saved = Path(tempfile.mkdtemp(prefix="failed-", dir=backup))
            self.record("last-recovery.json", {**pending, "result": "cold-failure-retained"})
            for name, path in (("state", self.state), ("config", self.config), ("release-root", self.root)):
                if path.exists():
                    path.rename(saved / name)
            return {"result": "cold-failure-retained", "business_acceptance": False}
        else:
            release = self.release(old)
            if pending["phase"] == "switching":
                self.check_current(old, pending)
                if old["config_sha256"] != pending["config_sha256"]:
                    backup = self.release(pending) / "previous-config.env"
                    self.config_snapshot(backup, self.config / "production.env", old["config_sha256"])
                if old.get("registry_sha256") != pending.get("registry_sha256"):
                    target = self.config / "registry/environment.json"
                    if "registry_sha256" in old:
                        old = self.recovery_registry(old, pending)
                    else:
                        target.unlink(missing_ok=True)
                self.switch_runtime(old)
                self.restore_schema_database(pending)
                self.check_database(release)
                self.check_current(old, pending)
                self.switch(release)
                self.command("systemctl", "daemon-reload")
                self.command("systemctl", "start", self.name)
            self.health(old["version"])
            self.record("installed.json", old)
            if "registry_sha256" not in old and (self.config / "registry").exists():
                (self.config / "registry/environment.json.lock").unlink(missing_ok=True)
                (self.config / "registry").rmdir()
        self.record("last-recovery.json", {**pending, "result": "restored"})
        (self.root / "pending.json").unlink()
        return {"result": "restored", "business_acceptance": False}

    def apply(
        self,
        archive,
        sha,
        python,
        config,
        config_sha,
        expected_config_sha=None,
        registry_file=None,
        expected_registry_sha=None,
        runtime_archive=None,
        runtime_sha=None,
        expected_runtime_sha=None,
    ):
        python, minor = probe.system_python(python, run=self.command)
        meta = probe.manifest(archive, sha, minor)
        if getattr(self, "revision", meta["revision"]) != meta["revision"]:
            raise ValueError("installer and application revisions differ")
        trusted(config, private=True)
        if digest(config) != config_sha:
            raise ValueError("configuration digest mismatch")
        registry_sha = None
        if registry_file is not None:
            trusted(registry_file, private=True)
            registry_sha = digest(registry_file)
        if runtime_archive is not None:
            if registry_file is None or not runtime_sha:
                raise ValueError("runtime requires bundled configuration and digest")
            trusted(runtime_archive, private=True)
            probe.runtime_files(runtime_archive, runtime_sha, self.environment)
        elif runtime_sha or expected_runtime_sha:
            raise ValueError("runtime artifact required")
        if (self.root / "pending.json").exists():
            raise ValueError("unfinished change requires recover")
        if (
            self.command("systemctl", "show", self.name + "-candidate", "-p", "LoadState", "--value")
            != "not-found"
        ):
            raise ValueError("candidate unit already exists")
        old = self.read_record("installed.json") if (self.root / "installed.json").exists() else None
        desired = {
            **meta,
            "environment": self.environment,
            "user": self.user,
            "port": self.port,
            "artifact_sha256": sha,
            "config_sha256": config_sha,
        }
        if registry_sha is not None:
            desired["registry_sha256"] = registry_sha
        if runtime_archive is not None:
            desired["runtime_sha256"] = runtime_sha
        if old:
            self.check_registry(old)
            self.check_runtime(old)
            if old.get("knowledge_sha256"):
                if digest(self.config / "knowledge/knowledge-delegation.json") != old["knowledge_sha256"]:
                    raise ValueError("delegated knowledge configuration drift")
                desired["knowledge_sha256"] = old["knowledge_sha256"]
            if old.get("runtime_sha256") and runtime_archive is None:
                raise ValueError("runtime configuration requires runtime artifact")
            if expected_runtime_sha is not None and expected_runtime_sha != old.get(
                "runtime_sha256", "absent"
            ):
                raise ValueError("stale expected runtime")
            if runtime_sha != old.get("runtime_sha256") and expected_runtime_sha != old.get(
                "runtime_sha256", "absent"
            ):
                raise ValueError("runtime change requires expected current digest")
            if "registry_sha256" not in old and (self.config / "registry").exists():
                raise ValueError("refuse to adopt unrecorded registry directory")
            if "registry_sha256" in old and registry_file is None:
                raise ValueError("bundled configuration requires a registry")
            if expected_registry_sha is not None and expected_registry_sha != old.get(
                "registry_sha256", "absent"
            ):
                raise ValueError("stale expected registry")
            if registry_sha != old.get("registry_sha256") and expected_registry_sha != old.get(
                "registry_sha256", "absent"
            ):
                raise ValueError("registry change requires expected current digest")
            if "registry_sha256" in old:
                previous_registry = json.loads((self.config / "registry/environment.json").read_text())
                if previous_registry["environments"][0]["enabled"] or any(
                    t["enabled"] for t in previous_registry["environments"][0]["tenants"]
                ):
                    if runtime_archive is None or config_sha != old["config_sha256"]:
                        raise ValueError("active registry requires unchanged shared configuration")
                    files, composed = probe.runtime_files(runtime_archive, runtime_sha, self.environment)
                    if runtime_sha != old.get("runtime_sha256"):
                        if (
                            composed.get("schema") != "aiops-environment-runtime/2"
                            or composed.get("previous_runtime_sha256") != old["runtime_sha256"]
                        ):
                            raise ValueError("active runtime requires recorded predecessor")
                        prior, _ = probe.runtime_files(
                            self.runtime_path(old) / "runtime.tar", old["runtime_sha256"], self.environment
                        )
                        shared = set(files) - {"manifest.json", "consumer-targets.json", "registrations.json"}
                        if any(files[k] != prior[k] for k in shared):
                            raise ValueError("active shared runtime changed")
                        previous_targets = json.loads(prior["consumer-targets.json"])["targets"]
                        next_targets = json.loads(files["consumer-targets.json"])["targets"]
                        inactive = {
                            t["id"]
                            for t in previous_registry["environments"][0]["tenants"]
                            if t.get("enabled") is False
                        }
                        if any(
                            t not in next_targets
                            for t in previous_targets
                            if t.get("tenant_id") not in inactive
                        ):
                            raise ValueError("active tenant runtime removed or replaced")
                    proposed = json.loads(registry_file.read_text())["environments"][0]
                    old_row = previous_registry["environments"][0]
                    if (
                        any(proposed[k] != old_row[k] for k in old_row if k != "tenants")
                        or any(t not in proposed["tenants"] for t in old_row["tenants"])
                        or any(
                            t["enabled"]
                            for t in proposed["tenants"]
                            if t["id"] not in {r["id"] for r in old_row["tenants"]}
                        )
                    ):
                        raise ValueError("active admission changed during candidate installation")
                    if runtime_sha != old.get("runtime_sha256") and any(
                        t["id"] == composed["tenant_id"] and t["enabled"] for t in proposed["tenants"]
                    ):
                        raise ValueError("candidate cannot replace active tenant")
                incoming = json.loads(registry_file.read_text())
                if (
                    registry_sha != old["registry_sha256"]
                    and incoming["revision"] <= previous_registry["revision"]
                ):
                    raise ValueError("registry revision must increase")
            if (
                self.unit.read_text() != self.unit_text()
                or digest(self.config / "production.env") != old["config_sha256"]
            ):
                raise ValueError("unit or configuration drift")
            if expected_config_sha is not None and expected_config_sha != old["config_sha256"]:
                raise ValueError("stale expected configuration")
            if old["config_sha256"] != config_sha and expected_config_sha != old["config_sha256"]:
                raise ValueError("configuration change requires expected current digest")
            if old["python"] != minor:
                raise ValueError("Python change needs separate delivery")
            previous = self.release(old)
            if (self.root / "current").resolve() != previous:
                raise ValueError("current release drift")
            self.command("systemctl", "is-active", "--quiet", self.name)
            self.health(old["version"], wait=0)
            if all(old.get(k) == v for k, v in desired.items()):
                return {"result": "unchanged", "version": old["version"], "business_acceptance": False}
            self.check_database(previous)
        else:
            if (
                expected_config_sha is not None
                or expected_registry_sha is not None
                or expected_runtime_sha is not None
            ):
                raise ValueError("cold installation has no previous configuration")
            if any(p.exists() or p.is_symlink() for p in (self.root, self.state, self.config, self.unit)):
                raise ValueError("refuse to adopt existing paths")
            if self.command("systemctl", "show", self.name, "-p", "LoadState", "--value") != "not-found":
                raise ValueError("refuse to adopt existing unit")
            with socket.socket() as sock:
                # uvicorn同样允许TIME_WAIT复用；listen仍拒绝已有活动listener。
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", self.port))
                sock.listen(1)
        if shutil.disk_usage("/opt").free < 1024**3:
            raise ValueError("insufficient disk headroom")
        available = int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())[1])
        if available < 1024 * 1024:
            raise ValueError("insufficient memory headroom")
        if not old:
            if self.backups.exists() or self.backups.is_symlink():
                trusted(self.backups, private=True)
            devices = {p.parent.stat().st_dev for p in (self.root, self.state, self.config, self.backups)}
            if self.backups.exists():
                devices.add(self.backups.stat().st_dev)
            if len(devices) != 1:
                raise ValueError("cold recovery requires one filesystem")
            staged_root = Path(tempfile.mkdtemp(prefix=self.name + "-preparing-", dir=self.root.parent))
            try:
                write(
                    staged_root / "pending.json",
                    json.dumps({**desired, "previous": None, "phase": "preparing"}) + "\n",
                )
                staged_root.chmod(0o755)
                staged_root.rename(self.root)
                descriptor = os.open(self.root.parent, os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if staged_root.exists():
                    shutil.rmtree(staged_root)
        else:
            self.record("pending.json", {**desired, "previous": old, "phase": "preparing"})
        try:
            return self.prepare(
                archive, sha, python, config, minor, meta, old, desired, registry_file, runtime_archive
            )
        except BaseException:
            if (self.root / "pending.json").exists():
                self.recover()
            raise

    def prepare(
        self,
        archive,
        sha,
        python,
        config,
        minor,
        meta,
        old,
        desired,
        registry_file=None,
        runtime_archive=None,
    ):
        if not old:
            self.config.mkdir(mode=0o755)
            self.state.mkdir(mode=0o700)
            os.chown(self.state, self.account.pw_uid, self.account.pw_gid)
            shutil.copyfile(config, self.config / "production.env")
            (self.config / "production.env").chmod(0o600)
            os.chown(self.config / "production.env", self.account.pw_uid, self.account.pw_gid)
            if digest(self.config / "production.env") != desired["config_sha256"]:
                raise ValueError("staged configuration mismatch")
            if registry_file is not None:
                self.registry_snapshot(
                    registry_file, self.config / "registry/environment.json", desired["registry_sha256"]
                )
        releases = self.root / "releases"
        releases.mkdir(mode=0o755, exist_ok=True)
        release = Path(tempfile.mkdtemp(prefix=sha[:12] + "-", dir=releases))
        release.chmod(0o755)
        desired["release"] = release.name
        desired["installer_sha256"] = digest(Path(__file__))
        if runtime_archive is not None:
            runtime = release / "runtime"
            runtime.mkdir(mode=0o700)
            os.chown(runtime, self.account.pw_uid, self.account.pw_gid)
            self.config_snapshot(runtime_archive, runtime / "runtime.tar", desired["runtime_sha256"])
            files, _ = probe.runtime_files(
                runtime / "runtime.tar", desired["runtime_sha256"], self.environment
            )
            for name, raw in files.items():
                self.config_snapshot_bytes(raw, runtime / name)
            desired["runtime_release"] = release.name
        staged = release / "artifact.tar.gz"
        shutil.copyfile(archive, staged)
        probe.manifest(staged, sha, minor)
        probe.extract(staged, release)
        candidate_config = release / "candidate-config.env"
        self.config_snapshot(config, candidate_config, desired["config_sha256"])
        if registry_file is not None:
            self.registry_snapshot(
                registry_file, release / "candidate-registry/environment.json", desired["registry_sha256"]
            )
            if not (self.config / "registry").exists():
                (self.config / "registry").mkdir(mode=0o700)
                os.chown(self.config / "registry", self.account.pw_uid, self.account.pw_gid)
        if old and "registry_sha256" in old:
            self.registry_snapshot(
                self.config / "registry/environment.json",
                release / "previous-registry/environment.json",
                old["registry_sha256"],
            )
        if old:
            self.config_snapshot(
                self.config / "production.env", release / "previous-config.env", old["config_sha256"]
            )
        if runtime_archive is not None:
            candidate_root = release / "candidate-root"
            candidate_root.mkdir(mode=0o700)
            os.chown(candidate_root, self.account.pw_uid, self.account.pw_gid)
            self.config_snapshot(
                candidate_config, candidate_root / "production.env", desired["config_sha256"]
            )
            self.registry_snapshot(
                release / "candidate-registry/environment.json",
                candidate_root / "registry/environment.json",
                desired["registry_sha256"],
            )
            (candidate_root / "runtime").symlink_to(runtime)
            if old and old.get("knowledge_sha256"):
                # 候选mount覆盖/etc根，必须携带同一受限知识文件，不复制中央管理key。
                (candidate_root / "knowledge").mkdir(mode=0o700)
                os.chown(candidate_root / "knowledge", self.account.pw_uid, self.account.pw_gid)
                self.config_snapshot(
                    self.config / "knowledge/knowledge-delegation.json",
                    candidate_root / "knowledge/knowledge-delegation.json",
                    old["knowledge_sha256"],
                )
        shutil.copyfile(Path(__file__).with_name("runtime_check.py"), release / "runtime_check.py")
        (release / "runtime_check.py").chmod(0o644)
        self.command(
            "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", python, "-I", "-m", "venv", str(release / "venv")
        )
        wheel = next((release / "wheelhouse").glob("aiops_diagnostics-*.whl"))
        self.command(
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "PIP_CONFIG_FILE=/dev/null",
            str(release / "venv/bin/pip"),
            "install",
            "--no-index",
            "--find-links",
            str(release / "wheelhouse"),
            str(wheel),
        )
        self.command(
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "PIP_CONFIG_FILE=/dev/null",
            str(release / "venv/bin/pip"),
            "check",
        )
        db_report = None
        if old:
            db_report = self._check_database_for_upgrade(release)
        trial = self.state / ("trial-" + uuid.uuid4().hex)
        self.record("pending.json", {**desired, "previous": old, "phase": "preparing", "trial": trial.name})
        trial.mkdir(mode=0o700)
        os.chown(trial, self.account.pw_uid, self.account.pw_gid)
        trial_unit = self.name + "-candidate"
        previous_directory = None
        if old and old.get("registry_sha256") and registry_file is not None and runtime_archive is not None:
            row = json.loads((self.config / "registry/environment.json").read_text())["environments"][0]
            if row["enabled"] or any(t["enabled"] for t in row["tenants"]):
                previous_directory = str(self.config / "registry/environment.json")
        try:
            if old and isinstance(db_report, dict) and db_report["version"] < db_report["current_version"]:
                self.rehearse_schema_upgrade(release, trial)
            self.command(
                "runuser",
                "-u",
                self.user,
                "--",
                *self.environment_args(trial, candidate_config),
                str(release / "venv/bin/python"),
                "-I",
                str(release / "runtime_check.py"),
                str(candidate_config),
                str(trial),
                self.environment,
                "trial",
                str(self.state),
                *(
                    [str(release / "candidate-registry/environment.json")]
                    if registry_file is not None
                    else []
                ),
                *([str(runtime)] if runtime_archive is not None else []),
                *([previous_directory] if previous_directory else []),
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            self.command(
                "systemd-run",
                "--quiet",
                "--collect",
                "--unit=" + trial_unit,
                "--property=User=" + self.user,
                "--property=MemoryMax=384M",
                "--property=CPUQuota=50%",
                "--property=TasksMax=64",
                "--property=RuntimeMaxSec=90",
                "--property=NoNewPrivileges=yes",
                "--property=ProtectSystem=strict",
                "--property=ProtectHome=yes",
                "--property=PrivateTmp=yes",
                "--property=PrivateNetwork=yes",
                "--property=ReadWritePaths=" + str(trial),
                *(
                    [f"--property=BindReadOnlyPaths={candidate_root}:{self.config}"]
                    if runtime_archive is not None
                    else [f"--property=BindReadOnlyPaths={candidate_config}:{self.config}/production.env"]
                ),
                *(
                    [f"--property=BindReadOnlyPaths={release}/candidate-registry:{self.config}/registry"]
                    if registry_file is not None and runtime_archive is None
                    else []
                ),
                *self.environment_args(trial),
                str(release / "venv/bin/aiops-gateway"),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            )
            self.health(meta["version"], port=port)
            if old:
                # 记录真实旧服务与候选共存的cgroup成本，供正式开通容量门消费。
                try:
                    samples = []
                    for unit in (self.name, trial_unit):
                        raw = self.command("systemctl", "show", unit, "-p", "MemoryCurrent", "-p", "MainPID")
                        values = dict(line.split("=", 1) for line in raw.splitlines())
                        samples.append(
                            {
                                "service": unit,
                                "memory_bytes": int(values["MemoryCurrent"]),
                                "pid": int(values["MainPID"]),
                            }
                        )
                    if any(row["memory_bytes"] <= 0 or row["pid"] <= 0 for row in samples):
                        raise ValueError("capacity sample unavailable")
                    write(
                        release / "capacity.json",
                        json.dumps(
                            {
                                "environment": self.environment,
                                "revision": desired["revision"],
                                "previous_revision": old["revision"],
                                "sampled_at": time.time(),
                                "services": samples,
                                "available_memory_bytes": int(
                                    re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())[1]
                                )
                                * 1024,
                            }
                        ),
                    )
                except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError):
                    # 安装合同不依赖容量采样；没有事实时后续发布必须明确拒绝。
                    pass
        finally:
            self.stop(trial_unit)
            shutil.rmtree(trial)
        if old and digest(self.config / "production.env") != old["config_sha256"]:
            raise ValueError("configuration changed during candidate validation")
        if old:
            self.check_registry(old)
            self.check_runtime(old)
        if runtime_archive is not None:
            self.check_runtime(old or {}, desired)
        self.record("pending.json", {**desired, "previous": old, "phase": "switching"})
        try:
            if old:
                self.stop(self.name)
                upgrade_report = self._check_database_for_upgrade(release)
                if any(p.is_symlink() for p in self.state.rglob("*")):
                    raise ValueError("state symlink prevents backup")
                shutil.copytree(self.state, release / "state-backup")
                (release / "state-backup").chmod(0o700)
                if digest(self.config / "production.env") != old["config_sha256"]:
                    raise ValueError("configuration changed before replacement")
                self.check_registry(old)
                self.check_runtime(old)
                if (
                    isinstance(upgrade_report, dict)
                    and upgrade_report["version"] < upgrade_report["current_version"]
                ):
                    backup_sha = self.backup_schema_database(release)
                    schema_upgrade = {
                        "from_version": upgrade_report["version"],
                        "to_version": upgrade_report["current_version"],
                        "backup_sha256": backup_sha,
                    }
                    self.record(
                        "pending.json",
                        {
                            **desired,
                            "previous": old,
                            "phase": "switching",
                            "schema_upgrade": schema_upgrade,
                        },
                    )
                    self.migrate_schema_database(release, upgrade_report["current_version"])
            write(self.unit, self.unit_text())
            self.unit.chmod(0o644)
            self.config_snapshot(candidate_config, self.config / "production.env", desired["config_sha256"])
            if registry_file is not None:
                self.registry_snapshot(
                    release / "candidate-registry/environment.json",
                    self.config / "registry/environment.json",
                    desired["registry_sha256"],
                )
            if runtime_archive is not None:
                self.switch_runtime(desired)
            self.switch(release)
            self.command("systemctl", "daemon-reload")
            self.command("systemctl", "start", self.name)
            self.health(meta["version"])
            self.command("systemctl", "enable", self.name)
            self.record("installed.json", desired)
            (self.root / "pending.json").unlink()
        except BaseException:
            self.recover()
            raise
        return {"result": "installed", "version": meta["version"], "business_acceptance": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "recover", "preflight", "register-runtime"))
    for name in ("environment", "user", "port"):
        parser.add_argument("--" + name, required=True, type=int if name == "port" else str)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--config-sha256")
    parser.add_argument("--expected-config-sha256", help="更换配置时必须匹配现役配置摘要")
    parser.add_argument("--candidate", type=Path, help="已冻结的三文件停用候选包，与--config互斥")
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--expected-registry-sha256", help="目录变化必须匹配当前摘要，首次引入为absent")
    parser.add_argument("--runtime", type=Path, help="同计划的私有运行工件，仅与--candidate成套使用")
    parser.add_argument("--runtime-sha256")
    parser.add_argument("--expected-runtime-sha256", help="运行工件变化匹配当前摘要，首次引入为absent")
    parser.add_argument("--python", default="/usr/bin/python3")
    parser.add_argument("--receipt", help="preflight回读持久现场回执；不安装或恢复")
    args = parser.parse_args()
    os.umask(0o022)  # 代码与venv可穿越；配置/状态/账本另用0600/0700。
    signal.signal(signal.SIGTERM, probe.interrupted)
    try:
        if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("Linux x86_64 host maintenance identity required")
        inst = Installer(args.environment, args.user, args.port)
        if args.action in ("install", "recover", "preflight", "register-runtime"):
            revision = Path(__file__).with_name("REVISION").read_text().strip()
            sums = Path(__file__).with_name("SHA256SUMS").read_text().splitlines()
            if not re.fullmatch(r"[a-f0-9]{40}", revision):
                raise ValueError("installer bundle identity required")
            expected = {"REVISION", "install.py", "runtime_check.py", "probe.py", "artifact_checks.py"}
            checks = dict(line.split("  ", 1)[::-1] for line in sums)
            if Path(__file__).with_name("task.py").is_file():
                expected.add("task.py")
            if Path(__file__).with_name("publication.py").is_file():
                expected.add("publication.py")
            if (
                len(checks) != len(sums)
                or set(checks) != expected
                or any(digest(Path(__file__).with_name(n)) != s for n, s in checks.items())
            ):
                raise ValueError("installer bundle digest mismatch")
            inst.revision = revision
        for p in (
            inst.root.parent,
            inst.state.parent,
            inst.config.parent,
            inst.unit.parent,
            Path("/var/backups"),
        ):
            for parent in (p, *p.parents):
                trusted(parent)
        lockpath = Path("/run/lock", inst.name + ".lock")
        descriptor = os.open(lockpath, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as lock:
            trusted(lockpath, private=True)
            if os.fstat(lock.fileno()).st_nlink != 1:
                raise ValueError("lock hardlink refused")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action in ("preflight", "register-runtime"):
                if any(
                    (
                        args.archive,
                        args.sha256,
                        args.config,
                        args.config_sha256,
                        args.candidate,
                        args.candidate_sha256,
                        args.expected_config_sha256,
                        args.expected_registry_sha256,
                        args.runtime,
                        args.runtime_sha256,
                        args.expected_runtime_sha256,
                    )
                ):
                    raise ValueError("observation and registration accept no deployment input")
                if args.action == "register-runtime":
                    if args.receipt is not None:
                        raise ValueError("registration accepts no preflight receipt")
                    result = inst.register_runtime()
                else:
                    result = inst.preflight(args.receipt)
            elif args.receipt is not None:
                raise ValueError("receipt is only accepted by preflight")
            elif args.action == "recover":
                result = inst.recover()
            else:
                if args.candidate is not None:
                    if not args.candidate_sha256 or args.config is not None or args.config_sha256:
                        raise ValueError("candidate and scalar configuration are mutually exclusive")
                    trusted(args.candidate, private=True)
                    files, candidate = probe.candidate_files(
                        args.candidate, args.candidate_sha256, args.environment
                    )
                    if args.runtime is not None:
                        trusted(args.runtime, private=True)
                        _, runtime = probe.runtime_files(args.runtime, args.runtime_sha256, args.environment)
                        if any(runtime[key] != candidate[key] for key in ("plan_id", "plan_digest")):
                            raise ValueError("runtime and configuration plan mismatch")
                        base = f"/etc/aiops-access-{args.environment}/runtime/"
                        files["production.env"] += (
                            f'AIOPS_GATEWAY_OPERATOR_PROFILES_FILE="{base}operator-profiles.json"\n'
                            f'AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE="{base}consumer-targets.json"\n'
                        ).encode()
                    elif args.runtime_sha256 or args.expected_runtime_sha256:
                        raise ValueError("runtime artifact required")
                    files["production.env"] = inst.candidate_configuration(files["production.env"])
                    files["environment.json"] = inst.candidate_directory(files["environment.json"])
                    if not args.archive or not args.sha256:
                        raise ValueError("application artifact required")
                    with tempfile.TemporaryDirectory(prefix="aiops-configuration-") as temporary:
                        root = Path(temporary)
                        for name in ("production.env", "environment.json"):
                            write(root / name, files[name].decode())
                        if any("${" in files[name].decode() for name in ("production.env",)):
                            raise ValueError("candidate interpolation refused")
                        runtime_path, runtime_sha = (
                            inst.candidate_runtime(args.runtime, args.runtime_sha256, root / "runtime.tar")
                            if args.runtime is not None
                            else (None, None)
                        )
                        result = inst.apply(
                            args.archive,
                            args.sha256,
                            args.python,
                            root / "production.env",
                            hashlib.sha256(files["production.env"]).hexdigest(),
                            args.expected_config_sha256,
                            root / "environment.json",
                            args.expected_registry_sha256,
                            runtime_path,
                            runtime_sha,
                            args.expected_runtime_sha256,
                        )
                elif (
                    not all((args.archive, args.sha256, args.config, args.config_sha256))
                    or args.candidate_sha256
                    or args.expected_registry_sha256
                    or args.runtime
                    or args.runtime_sha256
                    or args.expected_runtime_sha256
                ):
                    raise ValueError("artifact and configuration identities required")
                else:
                    result = inst.apply(
                        args.archive,
                        args.sha256,
                        args.python,
                        args.config,
                        args.config_sha256,
                        args.expected_config_sha256,
                    )
        print(json.dumps(result))
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
        tarfile.TarError,
        KeyboardInterrupt,
    ) as exc:
        print(
            json.dumps({"result": "failed", "error_type": type(exc).__name__, "business_acceptance": False})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
