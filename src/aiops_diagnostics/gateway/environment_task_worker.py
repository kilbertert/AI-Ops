"""单次受管现场预检：中央任务HMAC与dev-host；不安装/恢复或激活。"""

import hashlib
import json
import re
import shlex
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
from aiops_diagnostics.gateway.environment_tasks import validate_receipt


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
                "  name=m.name.removeprefix('./')",
                "  allowed={'REVISION','SHA256SUMS','install.py','runtime_check.py',"
                "'probe.py','artifact_checks.py','task.py'}",
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
            "python3",
            remote + "/install.py",
            "preflight",
            "--environment",
            environment,
            "--user",
            target["user"],
            "--port",
            str(target["port"]),
        ]
        output = subprocess.check_output(
            [
                "dev-host",
                "exec",
                host,
                "--allow-service-exec",
                "--timeout",
                "60",
                "--",
                shlex.join(args),
            ],
            cwd=root,
            text=True,
            timeout=65,
        )
        if len(output) > 65536:
            raise ValueError("receipt too large")
        result = json.loads(output)
        validate_receipt(result, environment)
        return result


def call(gateway, key, payload, *, trial=False):
    headers = {**build_internal_token_headers(key, 300, int(time.time())), "Content-Type": "application/json"}
    request = RequestSpec(
        gateway + ("/v1/internal/environment-trial" if trial else "/v1/internal/environment-tasks"),
        method="POST",
        body=json.dumps(payload).encode(),
        headers=headers,
        timeout=240
        if payload.get("prepare") == "knowledge" or payload.get("installation") == "stage"
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
    duration = 250 if payload.get("prepare") == "knowledge" or payload.get("installation") == "stage" else 45
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
            return installation_run(root, request, installation["id"], owner)
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
    try:
        result = execute(root, claim)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        # 不知道远端是否完成；只读重查可恢复，不能据此重复安装/开户。
        return request({"id": identifier, "owner": owner, "error": "ENVIRONMENT_TASK_TARGET_UNCONFIRMED"})
    return request({"id": identifier, "owner": owner, "result": result})


def installation_run(root, request, identifier, owner):
    """运维宿主搬运中央私有制品；未知结果只核对同一远端任务。"""
    staged = request({"installation": "stage", "id": identifier, "owner": owner})
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
    try:
        with tempfile.TemporaryDirectory(prefix="aiops-install-transfer-") as temporary:
            directory = Path(temporary)
            artifacts = staged["archives"] | {"installer": staged["installer"]}
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
                    script = (
                        "import sys,hashlib\nfrom pathlib import Path\n"
                        f"p=Path({source['path']!r})\n"
                        "assert p.is_file() and not p.is_symlink() and p.stat().st_mode&0o077==0\n\nactual=hashlib.file_digest(p.open('rb'),'sha256').hexdigest()\n"
                        f"assert actual=={source['sha256']!r}\n"
                        "sys.stdout.buffer.write(p.read_bytes())\n"
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
                    f" assert hashlib.file_digest(p.open('rb'),'sha256').hexdigest()=={actual!r}\n"
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
                "expected": staged["expected"],
                "archives": spec_archives,
            }
            arguments = [
                "/usr/bin/python3",
                remote + "/installer/task.py",
                "observe" if staged["observe_only"] else "run",
                "--environment",
                staged["environment"],
                "--user",
                target["user"],
                "--port",
                str(target["port"]),
            ]
            if not staged["observe_only"] and request(
                {"installation": "authorize", "id": identifier, "owner": owner}
            ) != {"authorized": True}:
                raise ValueError("installation authorization changed")
            # 核固定包身份后执行task.py；observe严格不再次apply，不向目标传共享凭据。
            script = (
                "import hashlib,json,subprocess,tarfile,sys\nfrom pathlib import Path\n"
                f"archive=Path({(remote + '/installer.tar.gz')!r})\n"
                "actual=hashlib.file_digest(archive.open('rb'),'sha256').hexdigest()\n"
                f"assert actual=={staged['installer']['sha256']!r}\n"
                f"d=Path({(remote + '/installer')!r});d.mkdir(mode=0o700,exist_ok=True)\n"
                "assert not d.is_symlink() and d.stat().st_uid==0 and d.stat().st_mode&0o077==0\n\nwith tarfile.open(archive) as t:\n"
                " for m in t:\n"
                "  if m.name=='.' and m.isdir():continue\n"
                "  n=m.name.removeprefix('./')\n"
                "  assert m.isfile() and n in {'REVISION','SHA256SUMS','install.py','runtime_check.py',"
                "'probe.py','artifact_checks.py','task.py'}\n"
                "  q=d/n;body=t.extractfile(m).read()\n"
                "  if q.exists() or q.is_symlink():\n"
                "   assert q.is_file() and not q.is_symlink() and q.stat().st_uid==0\n"
                "   assert q.read_bytes()==body\n"
                "  else:\n"
                "   with q.open('xb') as f:f.write(body)\n"
                f"assert (d/'REVISION').read_text().strip()=={staged['installer']['revision']!r}\n"
                "assert (d/'task.py').is_file()\n"
                f"r=subprocess.run({arguments!r},"
                f"input={json.dumps(spec)!r},text=True,capture_output=True)\n"
                "sys.stdout.write(r.stdout);sys.exit(r.returncode)\n"
            )
            raw = subprocess.check_output(command, cwd=root, input=script, text=True, timeout=805)
            if len(raw) > 65536:
                raise ValueError("installation response too large")
            result = json.loads(raw)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        pass  # 远端可能已切换：记录未知，只允许本任务后续observe。
    return request({"installation": "complete", "id": identifier, "owner": owner, "result": result})
