"""单次受管现场预检：中央任务HMAC与dev-host；不安装/恢复或激活。

「worker」指发起机上的单次执行体（``aiops-admin environment-task-worker``），
它经 ``dev-host exec``（SSH）访问受管目标与中央网关；真正在受管主机上
跑的是对应的 ``environment-task-dispatch``。不是常驻守护进程。
"""

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

from aiops_diagnostics.common.bounded_http import RequestSpec, open_response
from aiops_diagnostics.common.http_auth import build_internal_token_headers
from aiops_diagnostics.gateway.environment_artifacts import installer_manifest
from aiops_diagnostics.gateway.environment_drafts import REFERENCE_PATTERN
from aiops_diagnostics.gateway.environment_tasks import validate_receipt, validate_verify


def execute(root, claim):
    target, installer, environment = claim["target"], claim["installer"], claim["environment"]
    revision, expected = installer["revision"], installer["sha256"]
    if (
        not re.fullmatch(r"[0-9a-f]{40}", revision)
        or not re.fullmatch(r"[0-9a-f]{64}", expected)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?", environment)
        or not REFERENCE_PATTERN.fullmatch(target["host"])
        or target["host"].startswith("-")
        or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", target["user"])
        or type(target["port"]) is not int
        or not 1024 <= target["port"] <= 65535
    ):
        raise ValueError("invalid fixed target")
    declaration = tomllib.loads((root / ".dev-host.toml").read_text())
    matches = [
        row["name"]
        for row in declaration.get("targets", [])
        if row.get("host") == target["host"] or row.get("name") == target["host"]
    ]
    if len(matches) != 1 or not REFERENCE_PATTERN.fullmatch(matches[0]) or matches[0].startswith("-"):
        raise ValueError("target must resolve uniquely in project declaration")
    host = matches[0]
    # 构建器从origin/main已合并修订取固定文件；远端从不执行工作树源码。
    subprocess.run(["git", "merge-base", "--is-ancestor", revision, "origin/main"], cwd=root, check=True)
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "installer.tar.gz"
        subprocess.run(
            ["bash", "ops/environments/gateway/build-installer.sh", revision, str(archive)],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected or installer_manifest(archive, expected) != revision:
            raise ValueError("installer identity mismatch")
        remote = "/var/backups/aiops-site-task-" + expected
        command = [
            "dev-host",
            "exec",
            host,
            "--allow-service-exec",
            "--timeout",
            "45",
            "--",
            "python3 -",
        ]
        # 先拒绝既有链接/未知所有者，避免scp覆盖非本任务文件。
        guard = "\n".join(
            [
                "from pathlib import Path",
                "import hashlib",
                "p=Path(" + repr(remote + ".tar.gz") + ")",
                "if p.exists() or p.is_symlink():",
                " assert not p.is_symlink() and p.is_file() and p.stat().st_uid==0 and p.stat().st_nlink==1",
                " assert hashlib.sha256(p.read_bytes()).hexdigest()==" + repr(expected),
            ]
        )
        subprocess.run(command, cwd=root, input=guard, text=True, check=True, capture_output=True, timeout=50)
        subprocess.run(
            [
                "dev-host",
                "cp",
                host,
                str(archive),
                "--dest",
                remote + ".tar.gz",
                "--artifact-sha256",
                expected,
            ],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=35,
        )
        script = "\n".join(
            [
                "import hashlib,os,tarfile",
                "from pathlib import Path",
                "p=Path(" + repr(remote + ".tar.gz") + ")",
                "assert p.is_file() and not p.is_symlink() and p.stat().st_uid==0",
                "assert hashlib.sha256(p.read_bytes()).hexdigest()==" + repr(expected),
                "d=Path(" + repr(remote) + ")",
                "os.umask(0o077)",
                "d.mkdir(mode=0o700,exist_ok=True)",
                "assert not d.is_symlink() and d.stat().st_uid==0 and d.stat().st_mode&0o077==0",
                "with tarfile.open(p) as t:",
                " for m in t:",
                "  if m.name=='.' and m.isdir(): continue",
                "  name=m.name[2:] if m.name.startswith('./') else m.name",
                "  allowed={'REVISION','SHA256SUMS','install.py','runtime_check.py',"
                "'probe.py','artifact_checks.py','task.py','publication.py'}",
                "  assert m.isfile() and name in allowed",
                "  q=d/name",
                "  body=t.extractfile(m).read()",
                "  if q.exists():",
                "   assert not q.is_symlink() and q.stat().st_uid==0 and q.read_bytes()==body",
                "  else:",
                "   with q.open('xb') as f: f.write(body)",
            ]
        )
        subprocess.run(
            command, cwd=root, input=script, text=True, check=True, capture_output=True, timeout=50
        )
        args = [
            remote + "/install.py",
            "preflight",
            "--environment",
            environment,
            "--user",
            target["user"],
            "--port",
            str(target["port"]),
        ]
        # 固定包自举受管解释器：目标的/usr/bin默认可能是平台Python3.6；
        # 旧固定包probe无managed_python时沿用原worker合同PATH的python3；
        # 执行面只走system_python核验后的解析目标。
        script = "\n".join(
            [
                "import subprocess,sys",
                f"sys.path.insert(0,{remote!r})",
                "import probe",
                "py=probe.system_python(probe.managed_python())[0]"
                " if hasattr(probe,'managed_python') else 'python3'",
                f"r=subprocess.run([py]+{args!r},stdout=subprocess.PIPE,stderr=subprocess.PIPE,"
                "universal_newlines=True)",
                "sys.stdout.write(r.stdout);sys.exit(r.returncode)",
            ]
        )
        output = subprocess.check_output(
            [
                "dev-host",
                "exec",
                host,
                "--allow-service-exec",
                "--timeout",
                "60",
                "--",
                "python3 -",
            ],
            cwd=root,
            input=script,
            text=True,
            timeout=65,
        )
        if len(output) > 65536:
            raise ValueError("receipt too large")
        result = json.loads(output)
        validate_receipt(result, environment)
        return result


def verify(root, claim):
    """受管主机身份取证：dev-host断言后目标只读采集；原始machine-id不出目标。"""
    target, environment, check = claim["target"], claim["environment"], claim["check"]
    if (
        check != "managed-host-identity"
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?", environment)
        or not REFERENCE_PATTERN.fullmatch(target["host"])
        or target["host"].startswith("-")
        or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", target["user"])
        or type(target["port"]) is not int
        or not 1024 <= target["port"] <= 65535
    ):
        raise ValueError("invalid fixed target")
    declaration = tomllib.loads((root / ".dev-host.toml").read_text())
    matches = [
        row.get("name")
        for row in declaration.get("targets", [])
        if isinstance(row, dict) and (row.get("host") == target["host"] or row.get("name") == target["host"])
    ]
    resolved = (
        matches[0]
        if len(matches) == 1
        and isinstance(matches[0], str)
        and REFERENCE_PATTERN.fullmatch(matches[0])
        and not matches[0].startswith("-")
        else ""
    )
    facts = {}
    if resolved:
        # 实际访问由dev-host断言；机器标识只在目标侧取摘要，不回传原始值。
        script = "\n".join(
            [
                "import hashlib,json,pwd,socket",
                "out={'hostname':socket.gethostname()}",
                "for p in ('/etc/machine-id','/var/lib/dbus/machine-id'):",
                " try:",
                "  body=open(p,'rb').read().strip()",
                "  if body:out['machine_id_sha256']=hashlib.sha256(body).hexdigest();break",
                " except OSError:pass",
                f"try:pwd.getpwnam({target['user']!r});out['service_user_present']=True",
                "except KeyError:out['service_user_present']=False",
                "print(json.dumps(out))",
            ]
        )
        output = subprocess.check_output(
            [
                "dev-host",
                "exec",
                resolved,
                "--allow-service-exec",
                "--timeout",
                "30",
                "--",
                "python3 -",
            ],
            cwd=root,
            input=script,
            text=True,
            timeout=35,
        )
        if len(output) > 4096:
            raise ValueError("verification response too large")
        lines = output.strip().splitlines()
        facts = json.loads(lines[-1]) if lines else None
        if not isinstance(facts, dict):
            raise ValueError("verification output malformed")
    hostname = facts.get("hostname")
    machine = facts.get("machine_id_sha256")
    present = facts.get("service_user_present") is True
    complete = (
        isinstance(hostname, str)
        and 0 < len(hostname) <= 253
        and isinstance(machine, str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", machine))
        and present
    )
    now = time.time()
    result = {
        "id": "verify_" + uuid.uuid4().hex,
        "scope": "managed-host-verification-only",
        "environment": environment,
        "check": check,
        # 声明解析歧义是确定性结论，写blocked；连接层失败在上层记待核对。
        "state": "observed" if complete else "blocked",
        "executable": False,
        "evidence": {
            "declared_target": target["host"],
            "resolved_host": resolved,
            "remote_hostname": hostname if isinstance(hostname, str) else "",
            "machine_id_sha256": machine if isinstance(machine, str) else "",
            "service_user_present": present,
        },
        "created_at": now,
        "expires_at": now + 900,
    }
    validate_verify(result, environment)
    return result


def call(gateway, key, payload, *, trial=False):
    headers = {**build_internal_token_headers(key, 300, int(time.time())), "Content-Type": "application/json"}
    request = RequestSpec(
        gateway + ("/v1/internal/environment-trial" if trial else "/v1/internal/environment-tasks"),
        method="POST",
        body=json.dumps(payload).encode(),
        headers=headers,
        timeout=240
        if payload.get("prepare") == "knowledge"
        or payload.get("installation") == "stage"
        or payload.get("publication") in {"central", "connect"}
        else 30,
        follow_redirects=False,
    )
    with open_response(request) as response:
        body = response.read(65537)
    if len(body) > 65536:
        raise ValueError("response too large")
    return json.loads(body)


def central_call(root, target, payload):
    if not REFERENCE_PATTERN.fullmatch(target) or target.startswith("-"):
        raise ValueError("invalid central target")
    declaration = tomllib.loads((root / ".dev-host.toml").read_text())
    if sum(row.get("name") == target for row in declaration.get("targets", [])) != 1:
        raise ValueError("central target not declared")
    command = (
        "runuser -u aiops41 -- /opt/aiops-41/.venv/bin/aiops-admin "
        "--config /etc/aiops-41/gateway.env environment-task-dispatch"
    )
    # 固定服务身份/路径只支持已声明控制Gateway档案；不接受任务中的shell或HTTP地址。
    if target != "41":
        raise ValueError("central deployment profile unavailable")
    duration = (
        250
        if (
            payload.get("prepare") == "knowledge"
            or payload.get("installation") == "stage"
            or payload.get("publication") in {"central", "connect"}
        )
        else 45
    )
    result = subprocess.check_output(
        ["dev-host", "exec", target, "--allow-service-exec", "--timeout", str(duration), "--", command],
        cwd=root,
        input=json.dumps(payload),
        text=True,
        timeout=duration + 5,
    )
    if len(result) > 65536:
        raise ValueError("central response too large")
    return json.loads(result)


def run(root, gateway, key, identifier=None, *, central_target=None):
    if identifier is not None and not re.fullmatch(r"task_[0-9a-f]{32}", identifier):
        raise ValueError("invalid task identifier")
    # 操作者从规范checkout运行；已有本地未提交工作或非合并修订不能作为运维代码源。
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise ValueError("clean merged checkout required")
    subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=root, check=True)
    owner = uuid.uuid4().hex

    def request(payload):
        return central_call(root, central_target, payload) if central_target else call(gateway, key, payload)

    if identifier is None:
        request({"advance_candidates": True})
        installation = request({"installation_claim": True, "owner": owner})
        if installation.get("claimed") is True:
            if installation.get("action") == "environment-publish":
                from .environment_publication_worker import execute as publish_environment

                return publish_environment(root, request, installation["id"], owner)
            if installation.get("action") == "candidate-validate":
                from .environment_validation_worker import execute as validate_business

                return validate_business(root, request, installation["id"], owner)
            return installation_run(
                root,
                request,
                installation["id"],
                owner,
                registration=installation.get("action") == "candidate-register",
                cold=installation.get("action") == "site-install",
            )
    claim = request({"owner": owner, **({"id": identifier} if identifier else {})})
    if not isinstance(claim, dict) or type(claim.get("claimed")) is not bool:
        raise ValueError("central claim unavailable")
    if not claim["claimed"]:
        return claim["task"]
    identifier = claim["id"] if identifier is None else identifier
    if claim.get("action", "site-preflight") == "workspace-prepare":
        return request({"id": identifier, "owner": owner, "prepare": "workspace"})
    if claim.get("action") == "knowledge-prepare":
        return request({"id": identifier, "owner": owner, "prepare": "knowledge"})
    if claim.get("action") == "knowledge-connect":
        return request({"id": identifier, "owner": owner, "prepare": "connection"})
    if claim.get("action") == "operator-prepare":
        return request({"id": identifier, "owner": owner, "prepare": "operator"})
    if claim.get("action") == "consumer-prepare":
        return request({"id": identifier, "owner": owner, "prepare": "consumer"})
    if claim.get("action") == "host-verify":
        try:
            result = verify(root, claim)
        except (
            OSError,
            ValueError,
            KeyError,
            IndexError,
            AttributeError,
            TypeError,
            subprocess.SubprocessError,
        ):
            # 未接通目标不算「验过不通过」；只读重查可恢复，不写阻塞结论。
            return request({"id": identifier, "owner": owner, "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"})
        return request({"id": identifier, "owner": owner, "result": result})
    try:
        result = execute(root, claim)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        # 不知道远端是否完成；只读重查可恢复，不能据此重复安装/开户。
        return request({"id": identifier, "owner": owner, "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"})
    return request({"id": identifier, "owner": owner, "result": result})


def registration_intent_absent(root, host, environment, identifier):
    """远端登记意图缺席才证明登记步从未越过首写；不可核验一律按在场处理。

    registration-tasks/<id>.json 是 register() 在写任何 store 行之前的唯一
    落盘意图：确定缺席即证明该次派发零副作用。与 target_staged 同契约——
    远端脚本以 7 回答「缺席」；dev-host 自身失败（传输/权限落在 1/255）
    与环境锁被占（9，远端 task.py 可能在飞）都收敛到在场侧。
    """
    lockpath = "/run/lock/aiops-access-" + environment + ".lock"
    marker = "/opt/aiops-access-" + environment + "/registration-tasks/" + identifier + ".json"
    script = "\n".join(
        [
            "import fcntl,os,sys",
            "from pathlib import Path",
            f"fd=os.open({lockpath!r},os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)",
            "lock=os.fdopen(fd,'w')",
            "st=os.fstat(lock.fileno())",
            "if st.st_uid!=0 or st.st_mode&0o077 or st.st_nlink!=1:sys.exit(9)",
            "try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)",
            "except BlockingIOError:sys.exit(9)",
            f"sys.exit(0 if Path({marker!r}).is_file() else 7)",
            "",
        ]
    )
    try:
        probe = subprocess.run(
            ["dev-host", "exec", host, "--allow-service-exec", "--", "python3", "-B", "-"],
            cwd=root,
            input=script,
            text=True,
            capture_output=True,
            timeout=70,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 7


def installation_run(root, request, identifier, owner, *, registration=False, cold=False):
    """运维宿主搬运中央私有制品；未知结果只核对同一远端任务。"""
    action = "registration" if registration else "installation"
    staged = request({action: "stage", "id": identifier, "owner": owner})
    declared_host = staged["target"]["host"]
    declaration = tomllib.loads((root / ".dev-host.toml").read_text())
    matches = [
        r["name"]
        for r in declaration.get("targets", [])
        if r.get("name") == declared_host or r.get("host") == declared_host
    ]
    if len(matches) != 1:
        raise ValueError("installation target must resolve uniquely")
    host = matches[0]
    # 实际访问由dev-host断言；任务不能提供SSH参数或任意执行路径。
    if (
        not REFERENCE_PATTERN.fullmatch(host)
        or host.startswith("-")
        or not re.fullmatch(r"task_[a-f0-9]{32}", identifier)
    ):
        raise ValueError("installation identity invalid")
    target = staged["target"]
    if (
        not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", target["user"])
        or type(target["port"]) is not int
        or not 1024 <= target["port"] <= 65535
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?", staged["environment"])
    ):
        raise ValueError("installation profile invalid")
    remote = "/var/backups/aiops-install-task-" + identifier
    command = ["dev-host", "exec", host, "--allow-service-exec", "--timeout", "800", "--", "python3 -"]
    result = {
        "id": identifier,
        "environment": staged["environment"],
        "scope": "candidate-installation-only",
        "state": "reconcile",
        "executable": False,
        "activation": "not_executed",
        "registration": "not_executed",
        "business": "unverified",
        "error_code": "ENVIRONMENT_INSTALLATION_OBSERVATION_REQUIRED",
    }
    if registration:
        result = {
            "id": identifier,
            "environment": staged["environment"],
            "tenant_id": staged["tenant_id"],
            "scope": "inactive-runtime-registration-only",
            "state": "reconcile",
            "result": "unconfirmed",
            "executable": False,
            "activated": False,
            "business_acceptance": False,
            "source_revision": staged["expected"]["revision"],
            **{k: v for k, v in staged["expected"].items() if k != "revision"},
        }
    if cold:
        result = {
            "id": identifier,
            "environment": staged["environment"],
            "scope": "site-cold-installation-only",
            "state": "reconcile",
            "executable": False,
            "installation": "unconfirmed",
            "error_code": "ENVIRONMENT_INSTALLATION_OBSERVATION_REQUIRED",
        }
    try:
        with tempfile.TemporaryDirectory(prefix="aiops-install-transfer-") as temporary:
            directory = Path(temporary)
            artifacts = ({} if registration else staged["archives"]) | {"installer": staged["installer"]}
            spec_archives = {}
            for name, source in artifacts.items():
                local = directory / (name + ".tar.gz")
                if name == "installer":
                    subprocess.run(
                        [
                            "bash",
                            "ops/environments/gateway/build-installer.sh",
                            source["revision"],
                            str(local),
                        ],
                        cwd=root,
                        check=True,
                        capture_output=True,
                    )
                else:
                    # 仅受管服务身份读task目录中已冻结归档；原路径不通过shell拼接。
                    script = "\n".join(
                        [
                            "import sys,hashlib",
                            "from pathlib import Path",
                            f"p=Path({source['path']!r})",
                            "assert p.is_file() and not p.is_symlink() and p.stat().st_mode&0o077==0",
                            "",
                            "actual=hashlib.file_digest(p.open('rb'),'sha256').hexdigest()",
                            f"assert actual=={source['sha256']!r}",
                            "sys.stdout.buffer.write(p.read_bytes())",
                        ]
                    )
                    with local.open("wb") as stream:
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
                actual = hashlib.sha256(local.read_bytes()).hexdigest()
                if actual != source["sha256"]:
                    raise ValueError("installation artifact mismatch")
                guard = (
                    "from pathlib import Path\nimport hashlib,os\n"
                    f"d=Path({remote!r});d.mkdir(mode=0o700,exist_ok=True)\n"
                    "assert not d.is_symlink() and d.stat().st_uid==0 and d.stat().st_mode&0o077==0\n"
                    f"p=d/{local.name!r}\n"
                    "if p.exists() or p.is_symlink():\n"
                    " assert p.is_file() and not p.is_symlink() and p.stat().st_uid==0\n"
                    " assert p.stat().st_nlink==1\n"
                    f" assert hashlib.sha256(p.read_bytes()).hexdigest()=={actual!r}\n"
                )
                subprocess.run(
                    command, cwd=root, input=guard, text=True, check=True, capture_output=True, timeout=65
                )
                subprocess.run(
                    [
                        "dev-host",
                        "cp",
                        host,
                        str(local),
                        "--dest",
                        remote + "/" + local.name,
                        "--artifact-sha256",
                        actual,
                    ],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    timeout=65,
                )
                if name != "installer":
                    spec_archives[name] = {**source, "path": remote + "/" + local.name}
            spec = {
                "id": identifier,
                "environment": staged["environment"],
                "expires_at": staged["expires_at"],
                "archives": spec_archives,
            }
            if cold:
                spec["python"] = staged["python"]
            else:
                spec["expected"] = staged["expected"]
            if registration:
                spec.pop("archives")
                spec["tenant_id"] = staged["tenant_id"]
            remote_action = "observe" if staged["observe_only"] else "run"
            if cold:
                remote_action = "cold-observe" if staged["observe_only"] else "cold-run"
            if registration:
                remote_action = "observe-registration" if staged["observe_only"] else "register"
            arguments = [
                remote + "/installer/task.py",
                remote_action,
                "--environment",
                staged["environment"],
                "--user",
                target["user"],
                "--port",
                str(target["port"]),
            ]
            if not staged["observe_only"] and request(
                {action: "authorize", "id": identifier, "owner": owner}
            ) != {"authorized": True}:
                raise ValueError("installation authorization changed")
            # 核固定包身份后执行task.py；observe严格不再次apply，不向目标传共享凭据。
            script = "\n".join(
                [
                    "import hashlib,json,subprocess,tarfile,sys",
                    "from pathlib import Path",
                    f"archive=Path({(remote + '/installer.tar.gz')!r})",
                    "actual=hashlib.sha256(archive.read_bytes()).hexdigest()",
                    f"assert actual=={staged['installer']['sha256']!r}",
                    f"d=Path({(remote + '/installer')!r});d.mkdir(mode=0o700,exist_ok=True)",
                    "assert not d.is_symlink() and d.stat().st_uid==0 and d.stat().st_mode&0o077==0",
                    "",
                    "with tarfile.open(archive) as t:",
                    " for m in t:",
                    "  if m.name=='.' and m.isdir():continue",
                    "  n=m.name[2:] if m.name.startswith('./') else m.name",
                    "  assert m.isfile() and n in {'REVISION','SHA256SUMS','install.py','runtime_check.py',"
                    "'probe.py','artifact_checks.py','task.py','publication.py'}",
                    "  q=d/n;body=t.extractfile(m).read()",
                    "  if q.exists() or q.is_symlink():",
                    "   assert q.is_file() and not q.is_symlink() and q.stat().st_uid==0",
                    "   assert q.read_bytes()==body",
                    "  else:",
                    "   with q.open('xb') as f:f.write(body)",
                    f"assert (d/'REVISION').read_text().strip()=={staged['installer']['revision']!r}",
                    "assert (d/'task.py').is_file()",
                    "sys.path.insert(0,str(d))",
                    "import probe",
                    "py=probe.system_python(probe.managed_python())[0]"
                    " if hasattr(probe,'managed_python') else '/usr/bin/python3'",
                    f"r=subprocess.run([py]+{arguments!r},input={json.dumps(spec)!r},"
                    "stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)",
                    "sys.stdout.write(r.stdout);sys.exit(r.returncode)",
                ]
            )
            raw = subprocess.check_output(command, cwd=root, input=script, text=True, timeout=805)
            if len(raw) > 65536:
                raise ValueError("installation response too large")
            result = json.loads(raw)
            # 远端自报 absent 只证 store 为空：意图文件同缺才证明登记步零副作用。
            if (
                registration
                and result.get("result") == "absent"
                and registration_intent_absent(root, host, staged["environment"], identifier)
            ):
                result["remote_intent"] = "absent"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        # 登记意图缺席证明登记步从未越过首写：已派发任务凭此安全终态化，
        # 否则 observe-only 无法重放登记、reconcile 会永远占住互斥。
        if registration and registration_intent_absent(root, host, staged["environment"], identifier):
            result["result"] = "absent"
            result["remote_intent"] = "absent"
    return request({action: "complete", "id": identifier, "owner": owner, "result": result})
