"""原受管worker执行发布步骤；进程消失后只有同operation对账。"""

import hashlib
import json
import re
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from aiops_diagnostics.gateway.environment_drafts import REFERENCE_PATTERN
from aiops_diagnostics.gateway.environment_validation_worker import local_trial


def target_task(root, staged, bundle, action, *, observe_only=False, verification=None, authorize=None):
    target = staged["request"]["target"]
    declared = tomllib.loads((root / ".dev-host.toml").read_text())["targets"]
    matches = [
        r["name"] for r in declared if r.get("name") == target["host"] or r.get("host") == target["host"]
    ]
    if len(matches) != 1 or not REFERENCE_PATTERN.fullmatch(matches[0]):
        raise ValueError("fixed publication host required")
    identifier = staged["id"]
    if not re.fullmatch(r"task_[a-f0-9]{32}", identifier):
        raise ValueError("fixed publication id required")
    host = matches[0]
    installer = staged["request"]["installer"]
    remote = (
        "/var/backups/aiops-access-" + staged["request"]["plan"]["environment"] + "/publication-" + identifier
    )
    with tempfile.TemporaryDirectory(prefix="aiops-publication-transfer-") as directory:
        directory = Path(directory)
        archive = directory / "installer.tar.gz"
        subprocess.run(
            ["bash", "ops/environments/gateway/build-installer.sh", installer["revision"], str(archive)],
            cwd=root,
            check=True,
            capture_output=True,
        )
        if hashlib.sha256(archive.read_bytes()).hexdigest() != installer["sha256"]:
            raise ValueError("installer identity changed")
        publication = directory / "release.json"
        publication.touch(mode=0o600)
        script = (
            "import sys,hashlib\nfrom pathlib import Path\n"
            f"p=Path({bundle['path']!r})\n"
            "assert p.is_file() and not p.is_symlink() and p.stat().st_mode&0o077==0\n"
            f'assert hashlib.file_digest(p.open("rb"),"sha256").hexdigest()=={bundle["sha256"]!r}\n'
            "sys.stdout.buffer.write(p.read_bytes())\n"
        )
        with publication.open("wb") as stream:
            subprocess.run(
                [
                    "dev-host",
                    "exec",
                    "41",
                    "--allow-service-exec",
                    "--timeout",
                    "60",
                    "--",
                    "runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python -",
                ],
                cwd=root,
                input=script.encode(),
                stdout=stream,
                stderr=subprocess.PIPE,
                check=True,
                timeout=65,
            )
        if hashlib.sha256(publication.read_bytes()).hexdigest() != bundle["sha256"]:
            raise ValueError("release artifact changed")
        for file, sha in [(archive, installer["sha256"]), (publication, bundle["sha256"])]:
            guard = "\n".join(
                [
                    "from pathlib import Path",
                    f"p=Path({remote!r})",
                    "p.parent.mkdir(mode=0o700,exist_ok=True)",
                    "assert not p.parent.is_symlink() and p.parent.stat().st_uid==0",
                    "assert p.parent.stat().st_mode&0o077==0",
                    "p.mkdir(mode=0o700,exist_ok=True)",
                    "assert p.is_dir() and not p.is_symlink()",
                    "assert p.stat().st_uid==0 and p.stat().st_mode&0o077==0",
                    f"q=p/{file.name!r}",
                    "if q.exists() or q.is_symlink():",
                    " assert q.is_file() and not q.is_symlink() and q.stat().st_uid==0",
                    " assert q.stat().st_nlink==1",
                    " import hashlib",
                    f" assert hashlib.sha256(q.read_bytes()).hexdigest()=={sha!r}",
                ]
            )
            subprocess.run(
                ["dev-host", "exec", host, "--allow-service-exec", "--", "python3 -"],
                cwd=root,
                input=guard,
                text=True,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "dev-host",
                    "cp",
                    host,
                    str(file),
                    "--dest",
                    remote + "/" + file.name,
                    "--artifact-sha256",
                    sha,
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )
        arguments = [
            remote + "/installer/task.py",
            "publication-" + action,
            "--environment",
            staged["request"]["plan"]["environment"],
            "--user",
            target["user"],
            "--port",
            str(target["port"]),
        ]
        payload = {
            "path": remote + "/release.json",
            "sha256": bundle["sha256"],
            "observe_only": observe_only,
            "verification": verification,
        }
        script = "\n".join(
            [
                "import tarfile,hashlib,subprocess,sys",
                "from pathlib import Path",
                f"p=Path({remote!r})",
                'sha=hashlib.sha256((p/"installer.tar.gz").read_bytes()).hexdigest()',
                f"assert sha=={installer['sha256']!r}",
                'd=p/"installer";d.mkdir(mode=0o700,exist_ok=True)',
                "assert not d.is_symlink() and d.stat().st_uid==0",
                'with tarfile.open(p/"installer.tar.gz") as tar:',
                " for m in tar:",
                "  if m.name=='.' and m.isdir():continue",
                "  n=m.name[2:] if m.name.startswith('./') else m.name",
                "  assert m.isfile() and n in {'REVISION','SHA256SUMS','install.py','runtime_check.py',"
                "'probe.py','artifact_checks.py','task.py','publication.py'}",
                "  b=tar.extractfile(m).read();q=d/n",
                "  if q.exists():assert q.is_file() and not q.is_symlink() and q.read_bytes()==b",
                "  else:q.write_bytes(b)",
                "sys.path.insert(0,str(d))",
                "import probe",
                "py=probe.system_python(probe.managed_python())[0]"
                " if hasattr(probe,'managed_python') else '/usr/bin/python3'",
                f"r=subprocess.run([py]+{arguments!r},input={json.dumps(payload)!r},"
                "stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)",
                "sys.stdout.write(r.stdout);sys.exit(r.returncode)",
            ]
        )
        if authorize is not None:
            authorize()
        result = subprocess.check_output(
            ["dev-host", "exec", host, "--allow-service-exec", "--timeout", "240", "--", "python3 -"],
            cwd=root,
            input=script,
            text=True,
            timeout=245,
        )
        if len(result) > 65536:
            raise ValueError("publication result too large")
        return json.loads(result)


def verify(root, source, spec, identifier):
    from concurrent.futures import ThreadPoolExecutor

    def consumer():
        started = local_trial(
            root,
            source["target"],
            spec["environment"],
            {"action": "publication-trial-start", "operation": identifier, "spec": spec},
        )
        end = time.monotonic() + 210
        while True:
            result = local_trial(
                root,
                source["target"],
                spec["environment"],
                {
                    "action": "publication-trial-read",
                    "operation": identifier,
                    "trial_id": started["trial_id"],
                    "expected_reference": started["expected_reference"],
                },
            )
            if result["state"] not in {"running", "queued", "pending"}:
                if result["state"] != "passed":
                    raise ValueError("formal consumer trial failed")
                return result
            if time.monotonic() > end:
                raise ValueError("formal consumer trial unconfirmed")
            time.sleep(2)

    def operator(capability):
        started = local_trial(
            root,
            source["target"],
            spec["environment"],
            {
                "action": "publication-operator-start",
                "operation": identifier,
                "spec": spec,
                "capability": capability,
            },
        )
        deadline = time.monotonic() + 200
        while True:
            result = local_trial(
                root,
                source["target"],
                spec["environment"],
                {"action": "read", "app_id": spec["operator_app"], "trial_id": started["trial_id"]},
            )
            if result["status"] not in {"running", "queued", "pending"}:
                if result["status"] != "completed":
                    raise ValueError("formal operator trial failed")
                return started["trial_id"]
            if time.monotonic() > deadline:
                raise ValueError("formal operator trial unconfirmed")
            time.sleep(2)

    with ThreadPoolExecutor(max_workers=3) as pool:
        customer = pool.submit(consumer)
        runs = {cap: pool.submit(operator, cap) for cap in ("order", "offline")}
        customer.result()
        trials = {cap: future.result() for cap, future in runs.items()}
    return local_trial(
        root,
        source["target"],
        spec["environment"],
        {"action": "publication-trial-complete", "operation": identifier, "spec": spec, "trials": trials},
    )


def target_staged(root, staged, identifier):
    """目标宿主本任务的 staged 目录是否在场；宿主不可定位或探测失败按在场处理。"""
    target = staged["request"]["target"]
    declared = tomllib.loads((root / ".dev-host.toml").read_text())["targets"]
    matches = [r for r in declared if r.get("name") == target["host"] or r.get("host") == target["host"]]
    if len(matches) != 1 or not REFERENCE_PATTERN.fullmatch(matches[0]["name"]):
        return True
    environment = staged["request"]["plan"]["environment"]
    # 标记目录与锁都按安装器的推导（environment → aiops-access-<env>），
    # 与宿主注册的 paths.access 无关：标记在 inst.root 下，锁在 /run/lock 下。
    lockpath = "/run/lock/aiops-access-" + environment + ".lock"
    marker = "/opt/aiops-access-" + environment + "/publication-tasks/" + identifier
    # 远端以 7 报告「缺席」：dev-host 自身失败（传输/权限）落在 1/255 等普通码，
    # 只有远端脚本真实回答缺席才判不在场。9 = 环境锁被占（远端 task.py 可能在飞，
    # SSH 掉线不等于进程已死）或锁文件不可信，一律按在场收敛到受管恢复侧。
    script = (
        "import fcntl,os,sys\nfrom pathlib import Path\n"
        f"fd=os.open({lockpath!r},os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)\n"
        "lock=os.fdopen(fd,'w')\n"
        "st=os.fstat(lock.fileno())\n"
        "if st.st_uid!=0 or st.st_mode&0o077 or st.st_nlink!=1:sys.exit(9)\n"
        "try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
        "except BlockingIOError:sys.exit(9)\n"
        f"sys.exit(0 if Path({marker!r}).is_dir() else 7)\n"
    )
    probe = subprocess.run(
        [
            "dev-host",
            "exec",
            matches[0]["name"],
            "--allow-service-exec",
            "--",
            "python3",
            "-B",
            "-",
        ],
        cwd=root,
        input=script,
        text=True,
        capture_output=True,
        timeout=70,
    )
    return probe.returncode != 7


def execute(root, request, identifier, owner):
    def call(action, **data):
        return request({"publication": action, "id": identifier, "owner": owner, **data})

    staged = call("stage")
    source, steps = staged["request"], staged["request"]["steps"]
    described = call("describe")
    spec = described["spec"]
    result = {"state": "reconcile", "activated": False, "business_accuracy": "unconfirmed"}
    bundle = steps.get("central", {}).get("bundle")
    try:
        if staged["cancelled"]:
            if (
                bundle
                and "target" in steps
                and steps["target"].get("state") == "started"
                and not target_staged(root, staged, identifier)
            ):
                # 目标步只消费了 intent 且远端 staged 目录不在场：publication-tasks/<id>
                # 是 setup() 首个落盘写，缺席证明目标侧零副作用，与未启动同案恢复；
                # 在场或探测失败都不能排除副作用，仍走原受管恢复。
                result.update(state="cancelled", phase="restored")
            elif bundle and "target" in steps:
                result = target_task(root, staged, bundle, "restore")
                if result.get("phase") != "restored":
                    raise ValueError("target restoration unconfirmed")
                call("source-configuration", restore=True)
                call("directory", result=result, restore=True)
            elif "central" in steps and not bundle:
                # intent.json 是中央步首个落盘点；工件标记皆无证明未越过比对、无共享副作用，
                # 可与未启动同案恢复；标记在场或键缺失（旧网关不支持）都不能排除副作用，维持核查。
                markers = described.get("markers")
                if not markers or markers.get("intent") or markers.get("release"):
                    result.update(error_code="ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED")
                else:
                    result.update(state="cancelled", phase="restored")
            else:
                result.update(state="cancelled", phase="restored")
            return call("complete", result=result)
        if "capacity" not in steps or steps["capacity"].get("state") != "passed":
            call("intent", step="readiness")
            readiness = local_trial(
                root, source["target"], spec["environment"], {"action": "publication-readiness", "spec": spec}
            )
            call("record", step="readiness", result={"state": "passed", "observed_at": time.time()})
            intent = call("intent", step="capacity")
            if not intent["execute"]:
                raise ValueError("capacity outcome unconfirmed; do not replay models")
            from .environment_capacity import run as measure_capacity

            capacity = measure_capacity(root, staged, spec, call("capacity-reference"))
            call("record", step="capacity", result=capacity)
            return call("yield")
        if "target" not in steps or steps["target"].get("phase") != "staged":
            if not bundle:
                readiness = local_trial(
                    root,
                    source["target"],
                    spec["environment"],
                    {"action": "publication-readiness", "spec": spec},
                )
                intent = call("intent", step="central")
                bundle = call("central", spec=spec, readiness=readiness, observe_only=not intent["execute"])
                call("record", step="central", result={"state": "prepared", "bundle": bundle})
            intent = call("intent", step="target")
            result = target_task(
                root,
                staged,
                bundle,
                "stage",
                observe_only=not intent["execute"],
                authorize=lambda: call("intent", step="target"),
            )
            if result.get("phase") != "staged":
                raise ValueError("target release requires restore")
            call("record", step="target", result=result)
            call("connect", bundle=bundle)
            return call("yield")
        if "verify" not in steps or steps["verify"].get("state") != "passed":
            # 每种模型启动在目标先记同operation意图，重入只读已有trial而不重跑。
            call("intent", step="verify")
            call("connect", bundle=bundle)
            verification = verify(root, source, spec, identifier)
            call("record", step="verify", result=verification)
            return call("yield")
        verification = steps["verify"]
        call("intent", step="source")
        updated = call("source-configuration")
        call("record", step="source", result=updated)
        intent = call("intent", step="activate")
        result = target_task(
            root,
            staged,
            bundle,
            "activate",
            observe_only=not intent["execute"],
            verification=verification,
            authorize=lambda: call("intent", step="activate"),
        )
        call("record", step="activate", result=result)
        if result.get("activated"):
            call("directory", result=result)
            result["directory"] = "synchronized"
    except (ValueError, OSError, RuntimeError, KeyError, subprocess.SubprocessError):
        result.update(state="reconcile", error_code="ENVIRONMENT_PUBLICATION_RECONCILIATION_REQUIRED")
    return call("complete", result=result)
