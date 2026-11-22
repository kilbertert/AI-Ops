"""在自有临时根预演最小Gateway；不切换服务、不读取公司配置或激活租户。"""

import argparse
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
from pathlib import Path

try:
    from artifact_checks import candidate_files as candidate_files
    from artifact_checks import compose_runtime as compose_runtime
    from artifact_checks import manifest
    from artifact_checks import regular as regular
    from artifact_checks import runtime_files as runtime_files
except ModuleNotFoundError as exc:
    if exc.name != "artifact_checks":
        raise
    from aiops_diagnostics.gateway.environment_artifacts import candidate_files as candidate_files
    from aiops_diagnostics.gateway.environment_artifacts import compose_runtime as compose_runtime
    from aiops_diagnostics.gateway.environment_artifacts import manifest
    from aiops_diagnostics.gateway.environment_artifacts import regular as regular
    from aiops_diagnostics.gateway.environment_artifacts import runtime_files as runtime_files


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


def command(*args, pass_fds=(), input=None):
    return subprocess.check_output(
        args,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        timeout=120,
        pass_fds=pass_fds,
        input=input,
    ).strip()


SUPPORTED_PYTHON_MINORS = ("3.11", "3.12", "3.13")
MANAGED_PYTHON_ROOTS = ("/usr/bin", "/usr/local/bin")


def trusted_directory(path, owner):
    # /usr与/usr/local允许符号链接；解析后必须root拥有且组/他人不可写。
    path = Path(os.path.realpath(path))
    if not path.is_dir() or path.stat().st_uid != owner or path.stat().st_mode & 0o022:
        raise ValueError("untrusted interpreter location")


def system_python(path, *, roots=MANAGED_PYTHON_ROOTS, owner=0, run=command, check_dir=trusted_directory):
    """受管解释器：受管bin目录内python3[.11|.12|.13]；解析后目标与父目录归属/权限必须受控。"""
    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or str(candidate.parent) not in roots
        or not re.fullmatch(r"python3(?:\.(?:11|12|13))?", candidate.name)
    ):
        raise ValueError("only installed system Python is supported")
    resolved = Path(os.path.realpath(candidate))
    if not resolved.is_file():
        raise ValueError("trusted interpreter required")
    mode = resolved.stat().st_mode
    if resolved.stat().st_uid != owner or mode & 0o022 or not mode & 0o111:
        raise ValueError("untrusted interpreter ownership or mode")
    for directory in {*candidate.parents, *resolved.parents}:
        check_dir(directory, owner)
    minor = run(
        str(candidate),
        "-I",
        "-c",
        "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')",
    )
    expected = candidate.name[7:]
    if expected and minor != "3" + expected or not expected and minor not in SUPPORTED_PYTHON_MINORS:
        raise ValueError("interpreter minor mismatch")
    return str(candidate), minor


def managed_python(
    minor=None, *, roots=MANAGED_PYTHON_ROOTS, owner=0, run=command, check_dir=trusted_directory
):
    """/usr/bin先于/usr/local/bin、裸python3先于带小版本名；不匹配或不受管的候选跳过。"""
    for base in roots:
        for name in ("python3", "python3.11", "python3.12", "python3.13"):
            candidate = Path(base) / name
            if not candidate.exists() and not candidate.is_symlink():
                continue
            try:
                selected, actual = system_python(
                    candidate, roots=roots, owner=owner, run=run, check_dir=check_dir
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if minor is None or actual == minor:
                return selected
    raise ValueError("no managed interpreter for Python " + (minor or "3.11-3.13"))


def isolated_http(pid, port, path, *, post=False):
    script = """import json,urllib.request,urllib.error,sys
r=urllib.request.Request(sys.argv[1],data=b'{}' if sys.argv[2]=='post' else None,
 headers={'Content-Type':'application/json'})
try:
 with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=5) as response:
  print(json.dumps([response.status,json.load(response)]))
except urllib.error.HTTPError as exc:print(json.dumps([exc.code,{}]))
"""
    return json.loads(
        command(
            "nsenter",
            "-t",
            str(pid),
            "-n",
            "/usr/bin/python3",
            "-I",
            "-c",
            script,
            f"http://127.0.0.1:{port}{path}",
            "post" if post else "get",
        )
    )


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
        "network": "private-namespace",
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
                "--property=PrivateNetwork=yes",
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
            pid = int(command("systemctl", "show", unit, "-p", "MainPID", "--value"))
            if not pid or os.readlink(f"/proc/{pid}/ns/net") == os.readlink("/proc/self/ns/net"):
                raise RuntimeError("candidate network isolation missing")
            deadline = time.monotonic() + 45
            while True:
                try:
                    code, health = isolated_http(pid, port, "/health")
                    if code == 200:
                        break
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("candidate readiness failed")
                time.sleep(0.5)
            if health.get("version") != meta["version"] or health.get("business_mutations") != "disabled":
                raise RuntimeError("candidate identity mismatch")
            if isolated_http(pid, port, "/v1/assistant/questions", post=True)[0] not in (401, 403):
                raise RuntimeError("anonymous access was not refused")
            result["samples"][f"instance_{index}_idle"] = sample(unit)
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(
                    pool.map(lambda _, port=port, pid=pid: isolated_http(pid, port, "/health")[0], range(40))
                )
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
        args.python, minor = system_python(args.python)
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
