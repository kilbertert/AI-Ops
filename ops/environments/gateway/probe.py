"""在自有临时根预演最小Gateway；不切换服务、不读取公司配置或激活租户。"""

import argparse
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
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular file required")
    return path


def manifest(archive, digest, python_minor):
    regular(archive)
    if archive.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("archive too large")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("archive digest mismatch")
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if len(members) > 101 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise ValueError("artifact too large")
        files = {}
        for member in members:
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or str(name) != member.name
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
                or (member.isdir() and member.name != "wheelhouse")
            ):
                raise ValueError("unsafe archive member")
            if member.isfile():
                if member.name in files or member.size > 128 * 1024 * 1024:
                    raise ValueError("duplicate or oversized member")
                if member.name not in {"manifest.json", "SHA256SUMS"} and not (
                    name.parent == PurePosixPath("wheelhouse") and name.suffix == ".whl"
                ):
                    raise ValueError("unexpected artifact member")
                files[member.name] = member
        if not {"manifest.json", "SHA256SUMS"} <= files.keys() or len(files) > 100:
            raise ValueError("artifact file set invalid")
        checks = {}
        for line in bundle.extractfile(files["SHA256SUMS"]).read().decode().splitlines():
            sha, name = line.split("  ", 1)
            if not re.fullmatch(r"[a-f0-9]{64}", sha) or name in checks:
                raise ValueError("invalid digest manifest")
            checks[name] = sha
        if set(checks) != set(files) - {"SHA256SUMS"}:
            raise ValueError("artifact file set mismatch")
        for name, sha in checks.items():
            with bundle.extractfile(files[name]) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                    raise ValueError("artifact member mismatch")
        body = json.load(bundle.extractfile(files["manifest.json"]))
        if (
            not isinstance(body, dict)
            or set(body) != {"schema", "revision", "python", "platform", "version"}
            or type(body["schema"]) is not int
            or body["schema"] != 1
            or not all(isinstance(body[k], str) for k in ("revision", "python", "platform", "version"))
            or body["python"] != python_minor
            or body["platform"] != "linux-x86_64"
            or not re.fullmatch(r"[a-f0-9]{40}", body["revision"])
            or not re.fullmatch(r"\d+\.\d+\.\d+\+" + body["revision"][:12], body["version"])
            or sum(name.startswith("wheelhouse/aiops_diagnostics-") for name in files) != 1
        ):
            raise ValueError("incompatible artifact")
        return body


def extract(archive, dest):
    # 校验后只复制普通文件；不应用tar属主、权限、链接或设备元数据。
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if member.isfile():
                target = dest / member.name
                target.parent.mkdir(exist_ok=True)
                with bundle.extractfile(member) as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out)
                target.chmod(0o644)


def command(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True, timeout=120).strip()


def http(port, path, *, post=False):
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}" if post else None,
        headers={"Content-Type": "application/json"},
    )
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, {}


def sample(unit):
    raw = command("systemctl", "show", unit, "-p", "MainPID", "-p", "MemoryCurrent", "-p", "MemoryPeak")
    values = dict(line.split("=", 1) for line in raw.splitlines())
    pid = int(values["MainPID"])
    if not pid:
        raise RuntimeError("candidate exited")
    status = (Path("/proc") / str(pid) / "status").read_text()
    rss = {
        line.split(":")[0]: int(line.split()[1])
        for line in status.splitlines()
        if line.startswith(("VmRSS:", "VmHWM:"))
    }
    return {
        "rss_kib": rss["VmRSS"],
        "hwm_kib": rss["VmHWM"],
        "cgroup_bytes": int(values["MemoryCurrent"]),
        "cgroup_peak_bytes": int(values["MemoryPeak"]) if values.get("MemoryPeak", "").isdigit() else None,
    }


def run(args, meta):
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0 or account.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false"):
        raise ValueError("existing non-login service identity required")
    parent = args.root.absolute()
    for p in (parent, *parent.parents):
        if p.is_symlink() or not p.is_dir() or p.stat().st_uid != 0 or p.stat().st_mode & 0o022:
            raise ValueError("trusted root-owned parent required")
    command("runuser", "-u", args.user, "--", "/usr/bin/test", "-x", str(parent))
    root = Path(tempfile.mkdtemp(prefix="candidate-", dir=parent))
    units = []
    result = {
        "revision": meta["revision"],
        "version": meta["version"],
        "environment": args.environment,
        "scope": "startup-and-health-only",
        "business_acceptance": False,
        "samples": {},
    }
    try:
        root.chmod(0o755)
        os.umask(0o022)  # 仅无凭据代码可读，临时配置和状态另设0700/0600。
        staged = root / "artifact.tar.gz"
        shutil.copyfile(args.archive, staged)
        staged.chmod(0o600)
        manifest(staged, args.sha256, meta["python"])
        extract(staged, root)
        command(
            "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", args.python, "-I", "-m", "venv", str(root / "venv")
        )
        wheel = next((root / "wheelhouse").glob("aiops_diagnostics-*.whl"))
        command(
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "PIP_CONFIG_FILE=/dev/null",
            str(root / "venv/bin/pip"),
            "install",
            "--no-index",
            "--find-links",
            str(root / "wheelhouse"),
            str(wheel),
        )
        command(
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "PIP_CONFIG_FILE=/dev/null",
            str(root / "venv/bin/pip"),
            "check",
        )
        for index in range(2):
            state = root / f"state-{index}"
            state.mkdir(mode=0o700)
            os.chown(state, account.pw_uid, account.pw_gid)
            config = state / "config"
            config.mkdir(mode=0o700)
            os.chown(config, account.pw_uid, account.pw_gid)
            cfg = config / "production.env"
            cfg.write_text("# isolated candidate; no business sources\n")
            cfg.chmod(0o600)
            os.chown(cfg, account.pw_uid, account.pw_gid)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            unit = "aiops-candidate-" + root.name + f"-{index}"
            units.append(unit)
            command(
                "systemd-run",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                f"--property=User={args.user}",
                f"--property=Group={account.pw_gid}",
                "--property=MemoryMax=384M",
                "--property=CPUQuota=50%",
                "--property=TasksMax=32",
                "--property=RuntimeMaxSec=180",
                "--property=NoNewPrivileges=yes",
                "--property=ProtectSystem=strict",
                "--property=ProtectHome=yes",
                "--property=PrivateTmp=yes",
                "--property=UMask=0077",
                f"--property=ReadWritePaths={state}",
                "--property=IPAddressDeny=any",
                "--property=IPAddressAllow=localhost",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "LANG=C.UTF-8",
                "PYTHONNOUSERSITE=1",
                f"AIOPS_HOME={state}",
                f"AIOPS_DATA_HOME={state / 'data'}",
                f"AIOPS_CONFIG_HOME={config}",
                f"AIOPS_GATEWAY_ENVIRONMENT={args.environment}",
                str(root / "venv/bin/aiops-gateway"),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            )
            deadline = time.monotonic() + 45
            while True:
                try:
                    code, health = http(port, "/health")
                    if code == 200:
                        break
                except (OSError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("candidate readiness failed")
                time.sleep(0.5)
            if health.get("version") != meta["version"] or health.get("business_mutations") != "disabled":
                raise RuntimeError("candidate identity mismatch")
            if http(port, "/v1/assistant/questions", post=True)[0] not in (401, 403):
                raise RuntimeError("anonymous access was not refused")
            result["samples"][f"instance_{index}_idle"] = sample(unit)
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(lambda _, port=port: http(port, "/health")[0], range(40)))
            if responses != [200] * 40:
                raise RuntimeError("health load failed")
            result["samples"][f"instance_{index}_health"] = {
                **sample(unit),
                "requests": 40,
                "concurrency": 4,
                "elapsed_seconds": round(time.monotonic() - start, 3),
            }
        result["samples"]["overlap_rss_kib"] = sum(sample(u)["rss_kib"] for u in units)
        result["result"] = "passed"
    finally:
        cleanup_ok = True
        for unit in units:
            try:
                subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=30)
                state = subprocess.run(
                    ["systemctl", "show", unit, "-p", "LoadState", "-p", "MainPID"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                cleanup_ok &= "LoadState=not-found" in state.stdout or "MainPID=0\n" in state.stdout
            except (OSError, subprocess.SubprocessError):
                cleanup_ok = False
        if not cleanup_ok:
            raise RuntimeError("candidate cleanup unconfirmed; root retained")
        shutil.rmtree(root)
    result["temporary_services_removed"] = True
    return result


def interrupted(_signum, _frame):
    raise KeyboardInterrupt()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--python", default="/usr/bin/python3")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?", args.environment):
            raise ValueError("invalid environment")
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("unsupported platform")
        minor = command(
            args.python, "-I", "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        )
        meta = manifest(args.archive, args.sha256, minor)
        if args.verify_only:
            result = {"result": "verified", **meta}
        else:
            if os.geteuid() != 0:
                raise ValueError("host maintenance identity required")
            result = run(args, meta)
        print(json.dumps(result))
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        tarfile.TarError,
        KeyError,
        TypeError,
        KeyboardInterrupt,
    ) as exc:
        print(
            json.dumps({"result": "failed", "error_type": type(exc).__name__, "business_acceptance": False}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
