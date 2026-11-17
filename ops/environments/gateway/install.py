"""自有Gateway独立安装、同配置升级与中断恢复；只管理aiops-access-<环境>。"""

import argparse
import fcntl
import hashlib
import json
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

    def environment_args(self, state):
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
            f"AIOPS_GATEWAY_SERVER_CONFIG_FILE={self.config}/production.env",
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
                owned = {p.readlink().name for p in Path(f"/proc/{pid}/fd").iterdir()}
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

    def recover(self):
        pending = self.read_record("pending.json")
        if pending["phase"] not in ("preparing", "switching"):
            raise ValueError("unknown recovery phase")
        old = pending["previous"]
        if old and digest(self.config / "production.env") != old["config_sha256"]:
            raise ValueError("configuration drift prevents rollback")
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
                self.check_database(release)
                self.switch(release)
                self.command("systemctl", "daemon-reload")
                self.command("systemctl", "start", self.name)
            self.health(old["version"])
            self.record("installed.json", old)
        self.record("last-recovery.json", {**pending, "result": "restored"})
        (self.root / "pending.json").unlink()
        return {"result": "restored", "business_acceptance": False}

    def apply(self, archive, sha, python, config, config_sha):
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
        if old:
            if (
                self.unit.read_text() != self.unit_text()
                or digest(self.config / "production.env") != config_sha
            ):
                raise ValueError("unit or configuration drift")
            if old["config_sha256"] != config_sha or old["python"] != minor:
                raise ValueError("configuration or Python change needs separate delivery")
            previous = self.release(old)
            if (self.root / "current").resolve() != previous:
                raise ValueError("current release drift")
            self.command("systemctl", "is-active", "--quiet", self.name)
            self.health(old["version"], wait=0)
            if all(old.get(k) == v for k, v in desired.items()):
                return {"result": "unchanged", "version": old["version"], "business_acceptance": False}
            self.check_database(previous)
        else:
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
            if len({p.parent.stat().st_dev for p in (self.root, self.state, self.config, self.backups)}) != 1:
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
            return self.prepare(archive, sha, python, config, minor, meta, old, desired)
        except BaseException:
            if (self.root / "pending.json").exists():
                self.recover()
            raise

    def prepare(self, archive, sha, python, config, minor, meta, old, desired):
        if not old:
            self.config.mkdir(mode=0o755)
            self.state.mkdir(mode=0o700)
            os.chown(self.state, self.account.pw_uid, self.account.pw_gid)
            shutil.copyfile(config, self.config / "production.env")
            (self.config / "production.env").chmod(0o600)
            os.chown(self.config / "production.env", self.account.pw_uid, self.account.pw_gid)
            if digest(self.config / "production.env") != desired["config_sha256"]:
                raise ValueError("staged configuration mismatch")
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
                *self.environment_args(trial),
                str(release / "venv/bin/python"),
                "-I",
                str(release / "runtime_check.py"),
                str(self.config / "production.env"),
                str(trial),
                self.environment,
                "trial",
                str(self.state),
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
        self.record("pending.json", {**desired, "previous": old, "phase": "switching"})
        try:
            if old:
                self.stop(self.name)
                self.check_database(release)
                if any(p.is_symlink() for p in self.state.rglob("*")):
                    raise ValueError("state symlink prevents backup")
                shutil.copytree(self.state, release / "state-backup")
                (release / "state-backup").chmod(0o700)
            write(self.unit, self.unit_text())
            self.unit.chmod(0o644)
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
    parser.add_argument("action", choices=("install", "recover"))
    for name in ("environment", "user", "port"):
        parser.add_argument("--" + name, required=True, type=int if name == "port" else str)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--config-sha256")
    parser.add_argument("--python", default="/usr/bin/python3")
    args = parser.parse_args()
    os.umask(0o022)  # 代码与venv可穿越；配置/状态/账本另用0600/0700。
    signal.signal(signal.SIGTERM, probe.interrupted)
    try:
        if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("Linux x86_64 host maintenance identity required")
        inst = Installer(args.environment, args.user, args.port)
        if args.action in ("install", "recover"):
            revision = Path(__file__).with_name("REVISION").read_text().strip()
            sums = Path(__file__).with_name("SHA256SUMS").read_text().splitlines()
            if not re.fullmatch(r"[a-f0-9]{40}", revision):
                raise ValueError("installer bundle identity required")
            expected = {"REVISION", "install.py", "runtime_check.py", "probe.py"}
            checks = dict(line.split("  ", 1)[::-1] for line in sums)
            if set(checks) != expected or any(
                digest(Path(__file__).with_name(n)) != s for n, s in checks.items()
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
            if args.action == "recover":
                result = inst.recover()
            else:
                if not all((args.archive, args.sha256, args.config, args.config_sha256)):
                    raise ValueError("artifact and configuration identities required")
                result = inst.apply(args.archive, args.sha256, args.python, args.config, args.config_sha256)
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
