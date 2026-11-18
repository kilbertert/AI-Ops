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

    def command(self, *args):
        return probe.command(*args)

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

    def check_database(self, release):
        self.command(
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
            "existing",
        )

    def site_observation(self):
        """仅检查本安装器自有实例；网络/业务验收不是本机就绪检查。"""
        installed = self.read_record("installed.json")
        checks, observed = [], {"installed": installed}
        checks.append({"id": "local-path-identity", "state": "passed"})
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
            "checks": checks,
        }
        fingerprint = hashlib.sha256(
            json.dumps({"observed": observed, "public": public}, sort_keys=True).encode()
        ).hexdigest()
        return fingerprint, public, {"available_memory_bytes": available, "available_disk_bytes": free}

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
        if pending["phase"] not in ("preparing", "switching"):
            raise ValueError("unknown recovery phase")
        old = pending["previous"]
        if old:
            allowed = {old["config_sha256"]}
            if pending["phase"] == "switching":
                allowed.add(pending["config_sha256"])
            if digest(self.config / "production.env") not in allowed:
                raise ValueError("configuration drift prevents rollback")
            self.check_registry(old, pending if pending["phase"] == "switching" else None)
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
                if old["config_sha256"] != pending["config_sha256"]:
                    backup = self.release(pending) / "previous-config.env"
                    self.config_snapshot(backup, self.config / "production.env", old["config_sha256"])
                if old.get("registry_sha256") != pending.get("registry_sha256"):
                    target = self.config / "registry/environment.json"
                    if "registry_sha256" in old:
                        old = self.recovery_registry(old, pending)
                    else:
                        target.unlink(missing_ok=True)
                self.check_database(release)
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
    ):
        if not re.fullmatch(r"/usr/bin/python3(?:\.(?:11|12|13))?", python):
            raise ValueError("only installed system Python is supported")
        minor = self.command(
            python, "-I", "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        )
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
        if old:
            self.check_registry(old)
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
                    raise ValueError("active registry requires separate activation delivery")
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
            if expected_config_sha is not None or expected_registry_sha is not None:
                raise ValueError("cold installation has no previous configuration")
            if any(p.exists() or p.is_symlink() for p in (self.root, self.state, self.config, self.unit)):
                raise ValueError("refuse to adopt existing paths")
            if self.command("systemctl", "show", self.name, "-p", "LoadState", "--value") != "not-found":
                raise ValueError("refuse to adopt existing unit")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", self.port))
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
            return self.prepare(archive, sha, python, config, minor, meta, old, desired, registry_file)
        except BaseException:
            if (self.root / "pending.json").exists():
                self.recover()
            raise

    def prepare(self, archive, sha, python, config, minor, meta, old, desired, registry_file=None):
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
        if old:
            self.check_database(release)
        trial = self.state / ("trial-" + uuid.uuid4().hex)
        self.record("pending.json", {**desired, "previous": old, "phase": "preparing", "trial": trial.name})
        trial.mkdir(mode=0o700)
        os.chown(trial, self.account.pw_uid, self.account.pw_gid)
        trial_unit = self.name + "-candidate"
        try:
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
                f"--property=BindReadOnlyPaths={candidate_config}:{self.config}/production.env",
                *(
                    [f"--property=BindReadOnlyPaths={release}/candidate-registry:{self.config}/registry"]
                    if registry_file is not None
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
        finally:
            self.stop(trial_unit)
            shutil.rmtree(trial)
        if old and digest(self.config / "production.env") != old["config_sha256"]:
            raise ValueError("configuration changed during candidate validation")
        if old:
            self.check_registry(old)
        self.record("pending.json", {**desired, "previous": old, "phase": "switching"})
        try:
            if old:
                self.stop(self.name)
                self.check_database(release)
                if any(p.is_symlink() for p in self.state.rglob("*")):
                    raise ValueError("state symlink prevents backup")
                shutil.copytree(self.state, release / "state-backup")
                (release / "state-backup").chmod(0o700)
                if digest(self.config / "production.env") != old["config_sha256"]:
                    raise ValueError("configuration changed before replacement")
                self.check_registry(old)
            write(self.unit, self.unit_text())
            self.unit.chmod(0o644)
            self.config_snapshot(candidate_config, self.config / "production.env", desired["config_sha256"])
            if registry_file is not None:
                self.registry_snapshot(
                    release / "candidate-registry/environment.json",
                    self.config / "registry/environment.json",
                    desired["registry_sha256"],
                )
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
    parser.add_argument("action", choices=("install", "recover", "preflight"))
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
    parser.add_argument("--python", default="/usr/bin/python3")
    parser.add_argument("--receipt", help="preflight回读持久现场回执；不安装或恢复")
    args = parser.parse_args()
    os.umask(0o022)  # 代码与venv可穿越；配置/状态/账本另用0600/0700。
    signal.signal(signal.SIGTERM, probe.interrupted)
    try:
        if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("Linux x86_64 host maintenance identity required")
        inst = Installer(args.environment, args.user, args.port)
        if args.action in ("install", "recover", "preflight"):
            revision = Path(__file__).with_name("REVISION").read_text().strip()
            sums = Path(__file__).with_name("SHA256SUMS").read_text().splitlines()
            if not re.fullmatch(r"[a-f0-9]{40}", revision):
                raise ValueError("installer bundle identity required")
            expected = {"REVISION", "install.py", "runtime_check.py", "probe.py", "artifact_checks.py"}
            checks = dict(line.split("  ", 1)[::-1] for line in sums)
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
            if args.action == "preflight":
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
                    )
                ):
                    raise ValueError("preflight accepts no deployment input")
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
                    files, _ = probe.candidate_files(args.candidate, args.candidate_sha256, args.environment)
                    files["environment.json"] = inst.candidate_directory(files["environment.json"])
                    if not args.archive or not args.sha256:
                        raise ValueError("application artifact required")
                    with tempfile.TemporaryDirectory(prefix="aiops-configuration-") as temporary:
                        root = Path(temporary)
                        for name in ("production.env", "environment.json"):
                            write(root / name, files[name].decode())
                        if any("${" in files[name].decode() for name in ("production.env",)):
                            raise ValueError("candidate interpolation refused")
                        result = inst.apply(
                            args.archive,
                            args.sha256,
                            args.python,
                            root / "production.env",
                            hashlib.sha256(files["production.env"]).hexdigest(),
                            args.expected_config_sha256,
                            root / "environment.json",
                            args.expected_registry_sha256,
                        )
                elif (
                    not all((args.archive, args.sha256, args.config, args.config_sha256))
                    or args.candidate_sha256
                    or args.expected_registry_sha256
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
