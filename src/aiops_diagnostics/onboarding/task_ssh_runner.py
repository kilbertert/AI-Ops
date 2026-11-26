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
from aiops_diagnostics.onboarding import remote_scripts
from aiops_diagnostics.onboarding.artifacts import installer_manifest
from aiops_diagnostics.onboarding.drafts import REFERENCE_PATTERN
from aiops_diagnostics.onboarding.tasks import validate_receipt, validate_verify


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
        guard = remote_scripts.guard_remote_archive(remote + ".tar.gz", expected)
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
        script = remote_scripts.extract_preflight_installer(remote, expected)
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
        script = remote_scripts.run_preflight_driver(remote, args)
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
        script = remote_scripts.probe_host_identity(target["user"])
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
        or payload.get("wizard_job") in {"prepare_bundle", "connect_consumer_plugin", "activate_registry"}
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
            or payload.get("wizard_job") in {"prepare_bundle", "connect_consumer_plugin", "activate_registry"}
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


#: 安装面本次无可领（区别于「已领到并处理完毕」的返回值）。
_NOTHING = object()


def _installation_once(root, request, owner):
    """领一条安装面任务并处理；无可领返回 ``_NOTHING``，否则返回处理结果。"""
    claimed = request({"installation_claim": True, "owner": owner})
    if not (isinstance(claimed, dict) and claimed.get("claimed") is True):
        return _NOTHING
    if claimed.get("action") == "environment-publish":
        from aiops_diagnostics.onboarding.publication_ssh_runner import execute as publish_environment

        return publish_environment(root, request, claimed["id"], owner)
    if claimed.get("action") == "candidate-validate":
        from aiops_diagnostics.onboarding.validation_ssh_runner import execute as validate_business

        return validate_business(root, request, claimed["id"], owner)
    return installation_run(
        root,
        request,
        claimed["id"],
        owner,
        registration=claimed.get("action") == "candidate-register",
        cold=claimed.get("action") == "site-install",
    )


def run(root, gateway, key, identifier=None, *, central_target=None):
    if identifier is not None and not re.fullmatch(r"task_[0-9a-f]{32}", identifier):
        raise ValueError("invalid task identifier")
    # 操作者从规范checkout运行；受追踪代码树必须干净且已合并，避免未跟踪文档文件阻塞全站任务调度。
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True
    ).strip():
        raise ValueError("clean merged checkout required")
    subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=root, check=True)
    owner = uuid.uuid4().hex

    def request(payload):
        return central_call(root, central_target, payload) if central_target else call(gateway, key, payload)

    if identifier is None:
        advanced = request({"advance_candidates": True})
        # 两条认领面严格交替（ADR-0326）：两侧都可能长期不还名额——未知登记
        # checking 已派发即跳过计划窗/取消闸、永不终态；现场 reconcile 同样跳过
        # 计划窗。任一侧长期先手都会把另一侧饿死（单毒丸、跨环境多毒丸均已实测）。
        # 轮转游标在一次唤醒内固定本次先领哪一侧，该侧无可领时才退回另一侧。
        site_first = not (isinstance(advanced, dict) and advanced.get("turn") == "installation")
        claim = None
        for side in ("site", "installation") if site_first else ("installation", "site"):
            if side == "site":
                candidate = request({"owner": owner})
                if isinstance(candidate, dict) and candidate.get("claimed") is True:
                    claim = candidate
                    break
                claim = claim or candidate  # 未领到时留作「都空闲」的最终返回
                continue
            handled = _installation_once(root, request, owner)
            if handled is not _NOTHING:
                return handled
    else:
        claim = request({"owner": owner, "id": identifier})
    if not isinstance(claim, dict) or type(claim.get("claimed")) is not bool:
        raise ValueError("central claim unavailable")
    if not claim["claimed"]:
        if central_target:
            from aiops_diagnostics.onboarding import wizard_worker

            wizard_worker.run_pending_wizard_jobs(
                root,
                central_target,
                request=request,
            )
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
    script = remote_scripts.probe_registration_intent(lockpath, marker)
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


def registration_assets_present(root, host, environment, tenant):
    """远端登记资产是否已提交（只读核对）；None=读不出，按未知处理。

    register_runtime 在单事务里写工作区与双端应用，部分登记先被拒
    （SETUP_PARTIAL_REGISTRATION）再整体回滚——任一资产行在场即「某次
    登记已成套提交」，全缺即「提交未发生」；已派发登记任务的远端副作用
    只剩意图文件本身。与 registration_intent_absent 同契约：环境锁内
    只读，7=缺席、0=在场，其余（含传输失败、锁占）一律收敛到未知。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant):
        return None
    lockpath = "/run/lock/aiops-access-" + environment + ".lock"
    database = "/var/lib/aiops-access-" + environment + "/gateway/gateway.db"
    script = remote_scripts.probe_registration_assets(lockpath, database, tenant, environment)
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
        return None
    if probe is None:
        return None
    if probe.returncode == 7:
        return False
    if probe.returncode == 0:
        return True
    return None


def installation_run(root, request, identifier, owner, *, registration=False, cold=False):
    """运维宿主搬运中央私有制品；未知结果只核对同一远端任务。"""
    deadline = time.monotonic() + 840
    report_seconds = 210 if registration else 70
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
        # 慢链路共享有限传输预算；为派发前复核留时，观察回收不续签原计划。
        seconds = 600 if staged["observe_only"] else min(600, staged["expires_at"] - time.time() - 60)
        transfer_deadline = time.monotonic() + seconds

        def transfer_timeout(maximum):
            remaining = transfer_deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("installation artifact transfer", 0)
            return min(maximum, remaining)

        with tempfile.TemporaryDirectory(prefix="aiops-install-transfer-") as temporary:
            directory = Path(temporary)
            artifacts = ({} if registration else staged["archives"]) | {"installer": staged["installer"]}
            spec_archives = {}
            for name, source in artifacts.items():
                local = directory / (name + ".tar.gz")
                expected = source["sha256"]
                guard = remote_scripts.guard_staged_archive(remote, local.name, expected)
                guard_proc = subprocess.run(
                    command,
                    cwd=root,
                    input=guard,
                    text=True,
                    check=True,
                    capture_output=True,
                    timeout=transfer_timeout(65),
                )
                already_staged = (guard_proc.stdout or "").strip() == "present"
                if not already_staged:
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
                            timeout=transfer_timeout(65),
                        )
                    else:
                        # 仅受管服务身份读task目录中已冻结归档；原路径不通过shell拼接。
                        script = remote_scripts.read_central_archive(source["path"], expected)
                        with local.open("wb") as stream:
                            subprocess.run(
                                [
                                    "dev-host",
                                    "exec",
                                    "41",
                                    "--allow-service-exec",
                                    "--timeout",
                                    "180",
                                    "--",
                                    "runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python -",
                                ],
                                cwd=root,
                                input=script.encode(),
                                stdout=stream,
                                stderr=subprocess.PIPE,
                                check=True,
                                timeout=transfer_timeout(190),
                            )
                    actual = hashlib.sha256(local.read_bytes()).hexdigest()
                    if actual != expected:
                        raise ValueError("installation artifact mismatch")
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
                        timeout=transfer_timeout(480),
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
            if deadline - time.monotonic() <= report_seconds:
                raise subprocess.TimeoutExpired("installation action budget", 0)
            if not staged["observe_only"] and request(
                {action: "authorize", "id": identifier, "owner": owner}
            ) != {"authorized": True}:
                raise ValueError("installation authorization changed")
            # 核固定包身份后执行task.py；observe严格不再次apply，不向目标传共享凭据。
            script = remote_scripts.run_installer_task(
                remote,
                staged["installer"]["sha256"],
                staged["installer"]["revision"],
                arguments,
                spec,
            )
            action_seconds = min(800, deadline - time.monotonic() - report_seconds)
            if action_seconds <= 0:
                raise subprocess.TimeoutExpired("installation action budget", 0)
            command[command.index("--timeout") + 1] = str(max(1, int(action_seconds)))
            raw = subprocess.check_output(
                command, cwd=root, input=script, text=True, timeout=action_seconds + 5
            )
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
            elif registration and result.get("result") == "absent":
                # 意图在场（含探针读不出时收敛到在场）时，正常回报的 absent
                # 同样核登记资产：单事务原子落盘，缺席即除意图文件外零写。
                assets = registration_assets_present(root, host, staged["environment"], staged["tenant_id"])
                if assets is not None:
                    result["remote_assets"] = "present" if assets else "absent"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        # 登记意图缺席证明登记步从未越过首写：已派发任务凭此安全终态化，
        # 否则 observe-only 无法重放登记、reconcile 会永远占住互斥。
        if registration and registration_intent_absent(root, host, staged["environment"], identifier):
            result["result"] = "absent"
            result["remote_intent"] = "absent"
        elif registration:
            # 意图在场（含探针读不出时收敛到在场）后，再核登记资产是否已提交：
            # 工作区与双端应用属同一事务，原子落盘——在场即「原登记已完成」，
            # 缺席即「除意图文件外零写」。中央凭此对已取消任务诚实终态化；
            # 资产探针读不出则不带字段，任务维持 checking 待核。
            assets = registration_assets_present(root, host, staged["environment"], staged["tenant_id"])
            if assets is not None:
                result["remote_assets"] = "present" if assets else "absent"
    return request({action: "complete", "id": identifier, "owner": owner, "result": result})
