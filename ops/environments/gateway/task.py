"""固定停用候选安装任务；先记意图，重入只观察，事务复用原安装器。"""

import argparse
import fcntl
import json
import math
import os
import re
import socket
import tempfile
import time
from pathlib import Path

import install
import probe


def observe(inst, spec, record):
    result = {
        "id": spec["id"],
        "environment": inst.environment,
        "scope": "candidate-installation-only",
        "state": "blocked",
        "installation": "unconfirmed",
        "registration": "not_executed",
        "activation": "not_executed",
        "business": "unverified",
        "executable": False,
        "error_code": "ENVIRONMENT_INSTALLATION_OBSERVATION_REQUIRED",
    }
    if record is None or record.get("spec") != spec:
        return result
    if (inst.root / "pending.json").exists():
        return {**result, "state": "reconcile", "error_code": "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED"}
    _, current, _ = inst.site_observation()
    installed = inst.read_record("installed.json")
    if any(c["state"] == "blocked" for c in current["checks"]):
        return result
    desired = record.get("desired", {})
    if not desired or any(installed.get(k) != v for k, v in desired.items()):
        return result
    composed = {}
    if installed["runtime_sha256"] != spec["archives"]["runtime"]["sha256"]:
        _, manifest = probe.runtime_files(
            inst.runtime_path(installed) / "runtime.tar", installed["runtime_sha256"], inst.environment
        )
        if (
            manifest.get("source_runtime_sha256") != spec["archives"]["runtime"]["sha256"]
            or installed["runtime_sha256"] != spec["expected"]["runtime_sha256"]
            and manifest.get("previous_runtime_sha256") != spec["expected"]["runtime_sha256"]
        ):
            return result
        composed = {
            "source_runtime_sha256": manifest["source_runtime_sha256"],
            "previous_runtime_sha256": spec["expected"]["runtime_sha256"],
        }
    return {
        **result,
        "state": "observed",
        "installation": record.get("outcome", "installed"),
        "error_code": "",
        "version": installed["version"],
        "source_revision": installed["revision"],
        **composed,
        **{
            k: installed[k] for k in ("artifact_sha256", "config_sha256", "registry_sha256", "runtime_sha256")
        },
    }


def execute(inst, spec, *, observe_only=False):
    if (
        set(spec) != {"id", "environment", "expires_at", "expected", "archives"}
        or not re.fullmatch(r"task_[0-9a-f]{32}", spec["id"])
        or spec["environment"] != inst.environment
        or type(spec["expires_at"]) not in (int, float)
        or not math.isfinite(spec["expires_at"])
        or set(spec["expected"]) != {"artifact_sha256", "config_sha256", "registry_sha256", "runtime_sha256"}
        or set(spec["archives"]) != {"application", "candidate", "runtime"}
    ):
        raise ValueError("invalid installation task")
    for value in spec["archives"].values():
        if set(value) not in ({"path", "sha256"}, {"path", "sha256", "revision"}):
            raise ValueError("invalid task artifact")
        if not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
            raise ValueError("invalid task artifact digest")
        path = Path(value["path"])
        if not path.is_absolute():
            raise ValueError("invalid task artifact path")
        install.trusted(path, private=True)
        if path.stat().st_nlink != 1:
            raise ValueError("artifact hardlink refused")
    directory = inst.root / "installation-tasks"
    directory.mkdir(mode=0o700, exist_ok=True)
    install.trusted(directory, private=True)
    file = directory / (spec["id"] + ".json")
    if file.exists():
        install.trusted(file, private=True)
        if file.stat().st_nlink != 1 or file.stat().st_size > 65536:
            raise ValueError("invalid installation record")
        record = json.loads(file.read_text())
        return observe(inst, spec, record)
    if observe_only or spec["expires_at"] <= time.time():
        return observe(inst, spec, None)
    previous = inst.read_record("installed.json")
    if any(previous.get(k, "absent") != v for k, v in spec["expected"].items()):
        raise ValueError("installation baseline changed")
    if (inst.root / "pending.json").exists():
        return {
            **observe(inst, spec, None),
            "state": "reconcile",
            "error_code": "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED",
        }
    archives = spec["archives"]
    files, candidate = probe.candidate_files(
        Path(archives["candidate"]["path"]), archives["candidate"]["sha256"], inst.environment
    )
    _, runtime = probe.runtime_files(
        Path(archives["runtime"]["path"]), archives["runtime"]["sha256"], inst.environment
    )
    if any(runtime[k] != candidate[k] for k in ("plan_id", "plan_digest")):
        raise ValueError("configuration and runtime plans differ")
    meta = probe.manifest(
        Path(archives["application"]["path"]), archives["application"]["sha256"], previous["python"]
    )
    if meta["revision"] != inst.revision:
        raise ValueError("installer and application revisions differ")
    base = f"/etc/aiops-access-{inst.environment}/runtime/"
    files["production.env"] += (
        f'AIOPS_GATEWAY_OPERATOR_PROFILES_FILE="{base}operator-profiles.json"\n'
        f'AIOPS_GATEWAY_CONSUMER_PUBLICATION_TARGETS_FILE="{base}consumer-targets.json"\n'
    ).encode()
    files["production.env"] = inst.candidate_configuration(files["production.env"])
    files["environment.json"] = inst.candidate_directory(files["environment.json"])
    with tempfile.TemporaryDirectory(prefix="aiops-install-task-") as temporary:
        root = Path(temporary)
        for name in ("production.env", "environment.json"):
            install.write(root / name, files[name].decode())
        runtime_path, runtime_sha = inst.candidate_runtime(
            Path(archives["runtime"]["path"]), archives["runtime"]["sha256"], root / "runtime.tar"
        )
        desired = {
            "revision": meta["revision"],
            "artifact_sha256": archives["application"]["sha256"],
            "config_sha256": install.digest(root / "production.env"),
            "registry_sha256": install.digest(root / "environment.json"),
            "runtime_sha256": runtime_sha,
        }
        # 持久意图先于apply；崩溃后相同编号只能observe，不能再次写配置。
        record = {"spec": spec, "desired": desired}
        install.write(file, json.dumps(record))
        # 原install CLI的代码/venv穿越权限仍需0022，秘密和账本由原写入器保持私有。
        previous_umask = os.umask(0o022)
        try:
            outcome = inst.apply(
                Path(archives["application"]["path"]),
                archives["application"]["sha256"],
                probe.managed_python(previous["python"], run=inst.command),
                root / "production.env",
                desired["config_sha256"],
                spec["expected"]["config_sha256"],
                root / "environment.json",
                spec["expected"]["registry_sha256"],
                runtime_path,
                runtime_sha,
                spec["expected"]["runtime_sha256"],
            )
        finally:
            os.umask(previous_umask)
        record["outcome"] = outcome["result"]
        install.write(file, json.dumps(record))
    return observe(inst, spec, record)


def execute_cold(inst, spec, *, staging, observe_only=False):
    """真正空机的受管冷装：事实先分类，意图先于首写，事务仍归原apply。

    「未安装」只能由固定路径/单位/端口全空证明；已安装直接跳过，未知资产、
    半失败或旧单位按恢复/拒绝对账，绝不当空机重放。冷装意图放受管staging，
    inst.root在apply原子接管前保持不存在，安装器自身pending仍权威。
    """
    if (
        set(spec) != {"id", "environment", "expires_at", "python", "archives"}
        or not re.fullmatch(r"task_[0-9a-f]{32}", spec["id"])
        or spec["environment"] != inst.environment
        or type(spec["expires_at"]) not in (int, float)
        or not math.isfinite(spec["expires_at"])
        or spec["python"] not in probe.SUPPORTED_PYTHON_MINORS
        or set(spec["archives"]) != {"application", "candidate"}
    ):
        raise ValueError("invalid cold installation task")
    for value in spec["archives"].values():
        if set(value) not in ({"path", "sha256"}, {"path", "sha256", "revision"}):
            raise ValueError("invalid task artifact")
        if not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
            raise ValueError("invalid task artifact digest")
        path = Path(value["path"])
        if not path.is_absolute():
            raise ValueError("invalid task artifact path")
        install.trusted(path, private=True)
        if path.stat().st_nlink != 1:
            raise ValueError("artifact hardlink refused")
    result = {
        "id": spec["id"],
        "environment": inst.environment,
        "scope": "site-cold-installation-only",
        "state": "blocked",
        "installation": "unconfirmed",
        "executable": False,
        "error_code": "ENVIRONMENT_INSTALLATION_OBSERVATION_REQUIRED",
    }
    install.trusted(staging, private=True)
    intent = staging / "cold-intent.json"
    record = None
    if intent.exists() or intent.is_symlink():
        install.trusted(intent, private=True)
        if intent.stat().st_nlink != 1 or intent.stat().st_size > 65536:
            raise ValueError("invalid cold installation record")
        record = json.loads(intent.read_text())
        if record.get("spec") != spec:
            # 别的任务意图占用本staging：只对账不执行。
            return result
    residue = [p for p in inst.root.parent.glob(inst.name + "-preparing-*") if p.is_dir() or p.is_symlink()]
    ledger = inst.root / "installed.json"
    if ledger.exists() or ledger.is_symlink():
        # 同一安装器切换断点：pending与installed并存是中断事实，先恢复再谈收养。
        pending = inst.root / "pending.json"
        if pending.exists() or pending.is_symlink():
            return {
                **result,
                "state": "reconcile",
                "error_code": "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED",
            }
        # 已有installed目标跳过冷装；账本身份或期望漂移都只观察对账不重放。
        try:
            installed = inst.read_record("installed.json")
        except ValueError:
            return {
                **result,
                "state": "reconcile",
                "error_code": "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED",
            }
        desired = (record or {}).get("desired", {})
        if desired and any(installed.get(k) != v for k, v in desired.items()):
            return result
        return {
            **result,
            "state": "observed",
            "installation": (record or {}).get(
                "outcome", "installed" if record is not None else "already_installed"
            ),
            "error_code": "",
            "version": installed["version"],
            "source_revision": installed["revision"],
            **{k: installed[k] for k in ("artifact_sha256", "config_sha256")},
            # 旧config-only账本没有registry_sha256；与site_observation同一absent哨兵。
            "registry_sha256": installed.get("registry_sha256", "absent"),
        }
    occupied = any(p.exists() or p.is_symlink() for p in (inst.root, inst.state, inst.config, inst.unit))
    if not occupied and (inst.backups.exists() or inst.backups.is_symlink()):
        # 声明backups根允许受管staging预建；仅可信私有空目录仍属真空，内容即旧资产。
        try:
            install.trusted(inst.backups, private=True)
            occupied = not inst.backups.is_dir() or any(inst.backups.iterdir())
        except (OSError, ValueError):
            occupied = True
    if (
        occupied
        or residue
        or inst.command("systemctl", "show", inst.name, "-p", "LoadState", "--value") != "not-found"
    ):
        # 半失败/未知资产/旧单位按恢复原语对账，不进入冷装。
        return {
            **result,
            "state": "reconcile",
            "error_code": "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED",
        }
    if record is not None:
        # 本任务意图在场而根从未接管：apply未越过首写，事实不明先对账。
        return {**result, "state": "reconcile", "error_code": "ENVIRONMENT_INSTALLATION_RECOVERY_REQUIRED"}
    if observe_only or spec["expires_at"] <= time.time():
        # 空机事实可证，但安装目标未达：不算已安装，不推进后续预检。
        return {**result, "installation": "not_installed"}
    try:
        with socket.socket() as sock:
            # uvicorn同样允许TIME_WAIT复用；listen仍拒绝已有活动listener。
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", inst.port))
            sock.listen(1)
    except OSError:
        return {**result, "error_code": "ENVIRONMENT_COLD_INSTALLATION_REFUSED"}
    archives = spec["archives"]
    files, _candidate = probe.candidate_files(
        Path(archives["candidate"]["path"]), archives["candidate"]["sha256"], inst.environment
    )
    meta = probe.manifest(
        Path(archives["application"]["path"]), archives["application"]["sha256"], spec["python"]
    )
    if meta["revision"] != inst.revision:
        raise ValueError("installer and application revisions differ")
    # 冷装仅投影停用目录与公司连接；运行工件指针与runtime不在本步。
    files["production.env"] = inst.candidate_configuration(files["production.env"])
    files["environment.json"] = inst.candidate_directory(files["environment.json"])
    with tempfile.TemporaryDirectory(prefix="aiops-cold-task-") as temporary:
        root = Path(temporary)
        for name in ("production.env", "environment.json"):
            install.write(root / name, files[name].decode())
        desired = {
            "revision": meta["revision"],
            "artifact_sha256": archives["application"]["sha256"],
            "config_sha256": install.digest(root / "production.env"),
            "registry_sha256": install.digest(root / "environment.json"),
        }
        # 冷装意图先于apply首写持久到受管staging，inst.root仍不存在。
        install.write(intent, json.dumps({"spec": spec, "desired": desired}))
        # 原install CLI的代码/venv穿越权限仍需0022，秘密和账本由原写入器保持私有。
        previous_umask = os.umask(0o022)
        try:
            outcome = inst.apply(
                Path(archives["application"]["path"]),
                archives["application"]["sha256"],
                probe.managed_python(spec["python"], run=inst.command),
                root / "production.env",
                desired["config_sha256"],
                registry_file=root / "environment.json",
            )
        finally:
            os.umask(previous_umask)
        record = {"spec": spec, "desired": desired, "outcome": outcome["result"]}
        install.write(intent, json.dumps(record))
    return {
        **result,
        "state": "observed",
        "installation": outcome["result"],
        "error_code": "",
        "version": outcome["version"],
        "source_revision": desired["revision"],
        **{k: desired[k] for k in ("artifact_sha256", "config_sha256", "registry_sha256")},
    }


def register(inst, spec, *, observe_only=False):
    """登记意图先持久；失联后只观察原同事务登记，不补写缺失资源。"""
    if (
        set(spec) != {"id", "environment", "expires_at", "expected", "tenant_id"}
        or not isinstance(spec["id"], str)
        or not re.fullmatch(r"task_[a-f0-9]{32}", spec["id"])
        or spec["environment"] != inst.environment
        or not isinstance(spec["tenant_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec["tenant_id"])
        or type(spec["expires_at"]) not in (int, float)
        or not math.isfinite(spec["expires_at"])
        or set(spec["expected"])
        != {"revision", "artifact_sha256", "config_sha256", "registry_sha256", "runtime_sha256"}
    ):
        raise ValueError("invalid registration task")
    installed = inst.read_record("installed.json")
    if any(installed.get(k) != v for k, v in spec["expected"].items()):
        raise ValueError("registration baseline changed")
    runtime = inst.runtime_path(installed)
    registration = json.loads((runtime / "registrations.json").read_text())
    if registration["tenant_id"] != spec["tenant_id"] or registration["environment"] != inst.environment:
        raise ValueError("registration tenant mismatch")
    directory = inst.root / "registration-tasks"
    file = directory / (spec["id"] + ".json")
    previous = None
    if directory.exists():
        install.trusted(directory, private=True)
    if file.exists() or file.is_symlink():
        install.trusted(file, private=True)
        if file.stat().st_nlink != 1 or file.stat().st_size > 65536:
            raise ValueError("invalid registration record")
        previous = json.loads(file.read_text())
        if previous != spec:
            raise ValueError("registration intent changed")
    observing = observe_only or previous is not None or spec["expires_at"] <= time.time()
    if not observing:
        directory.mkdir(mode=0o700, exist_ok=True)
        install.trusted(directory, private=True)
        install.write(file, json.dumps(spec))
    result = inst.register_runtime(observe_only=observing)
    if result["tenant_id"] != spec["tenant_id"]:
        raise ValueError("registration receipt tenant mismatch")
    return {
        **result,
        "id": spec["id"],
        "state": "observed" if result["result"] in {"registered", "unchanged"} else "reconcile",
        "executable": False,
        "source_revision": installed["revision"],
        **{k: installed[k] for k in ("artifact_sha256", "config_sha256", "registry_sha256")},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "run",
            "observe",
            "register",
            "observe-registration",
            "publication-stage",
            "publication-activate",
            "publication-restore",
            "publication-observe",
            "cold-run",
            "cold-observe",
        ),
    )
    for key in ("environment", "user", "port"):
        parser.add_argument("--" + key, required=True, type=int if key == "port" else str)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("host maintenance identity required")
    os.umask(0o077)
    bundle = Path(__file__).parent
    revision = (bundle / "REVISION").read_text().strip()
    checks = dict(line.split("  ", 1)[::-1] for line in (bundle / "SHA256SUMS").read_text().splitlines())
    expected = {
        "REVISION",
        "install.py",
        "probe.py",
        "runtime_check.py",
        "artifact_checks.py",
        "task.py",
    }
    if (bundle / "publication.py").exists():
        expected.add("publication.py")
    if set(checks) != expected or any(
        install.digest(bundle / name) != digest for name, digest in checks.items()
    ):
        raise ValueError("installer bundle mismatch")
    if args.action == "cold-run":
        install.ensure_service_identity(args.environment, args.user)
    inst = install.Installer(args.environment, args.user, args.port)
    if args.action == "cold-run":
        inst.ensure_prerequisites = True
    inst.revision = revision
    for parent in (
        inst.root.parent,
        inst.state.parent,
        inst.config.parent,
        inst.unit.parent,
        inst.backups.parent,
    ):
        for p in (parent, *parent.parents):
            install.trusted(p)
    raw = __import__("sys").stdin.read(65537)
    if len(raw) > 65536:
        raise ValueError("task request too large")
    spec = json.loads(raw)
    lockpath = Path("/run/lock", inst.name + ".lock")
    fd = os.open(lockpath, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        install.trusted(lockpath, private=True)
        if os.fstat(lock.fileno()).st_nlink != 1:
            raise ValueError("invalid installation lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action.startswith("publication-"):
            import publication

            file = Path(spec["path"])
            install.trusted(file, private=True)
            if file.stat().st_size > 1048576 or install.digest(file) != spec["sha256"]:
                raise ValueError("release artifact mismatch")
            document = json.loads(file.read_text())
            action = args.action.removeprefix("publication-")
            result = (
                publication.stage(inst, document, observe_only=spec["observe_only"])
                if action == "stage"
                else publication.activate(
                    inst, document, spec["verification"], observe_only=spec["observe_only"]
                )
                if action == "activate"
                else publication.recover(inst, document["id"])
                if action == "restore"
                else publication.observe(inst, document)
            )
            print(json.dumps(result))
            return
        result = (
            register(inst, spec, observe_only=args.action == "observe-registration")
            if args.action in {"register", "observe-registration"}
            else execute_cold(inst, spec, staging=bundle.parent, observe_only=args.action == "cold-observe")
            if args.action in {"cold-run", "cold-observe"}
            else execute(inst, spec, observe_only=args.action == "observe")
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
