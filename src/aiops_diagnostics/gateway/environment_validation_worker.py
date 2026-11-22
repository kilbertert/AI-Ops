"""受管验证执行端只调用现有候选通道，不保管公司长期凭据。"""

import contextlib
import json
import re
import shlex
import subprocess
import time
import tomllib


def local_trial(root, target, environment, payload, *, control=False):
    from aiops_diagnostics.gateway.environment_drafts import REFERENCE_PATTERN

    if control:
        host, user, executable, config = (
            "41",
            "aiops41",
            "/opt/aiops-41/.venv/bin/aiops-admin",
            "/etc/aiops-41/gateway.env",
        )
    else:
        rows = tomllib.loads((root / ".dev-host.toml").read_text()).get("targets", [])
        matches = [
            r["name"] for r in rows if r.get("name") == target["host"] or r.get("host") == target["host"]
        ]
        if (
            len(matches) != 1
            or not REFERENCE_PATTERN.fullmatch(matches[0])
            or matches[0].startswith("-")
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", environment)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", target["user"])
        ):
            raise ValueError("fixed validation target unavailable")
        host, user = matches[0], target["user"]
        executable, config = (
            f"/opt/aiops-access-{environment}/current/venv/bin/aiops-admin",
            f"/etc/aiops-access-{environment}/production.env",
        )
    environment_args = [] if control else ["env", "AIOPS_GATEWAY_PORT=" + str(target["port"])]
    if not control and (type(target["port"]) is not int or not 1024 <= target["port"] <= 65535):
        raise ValueError("invalid validation port")
    command = shlex.join(
        [
            "runuser",
            "-u",
            user,
            "--",
            *environment_args,
            executable,
            "--config",
            config,
            "environment-trial-dispatch",
        ]
    )
    try:
        result = subprocess.check_output(
            ["dev-host", "exec", host, "--allow-service-exec", "--timeout", "240", "--", command],
            cwd=root,
            input=json.dumps(payload),
            text=True,
            timeout=245,
        )
    except subprocess.CalledProcessError as exc:
        if exc.output and len(exc.output) <= 1024:
            try:
                refused = json.loads(exc.output)
            except ValueError:
                refused = None
            if refused == {"error": "ENVIRONMENT_VALIDATION_KEY_REQUIRED"}:
                return {
                    "capability": payload.get("capability"),
                    "state": "blocked",
                    "error_code": refused["error"],
                }
        raise
    if len(result) > 65536:
        raise ValueError("validation response too large")
    return json.loads(result)


def execute(root, request, identifier, owner):
    staged = request({"validation": "stage", "id": identifier, "owner": owner})
    spec = staged["spec"]
    started_at = time.monotonic()
    if staged.get("cancelled"):
        return request({"validation": "complete", "id": identifier, "owner": owner})
    for capability in spec["capabilities"]:
        previous = staged["steps"].get(capability)
        if previous and previous["state"] in {"passed", "blocked"}:
            continue
        result = {
            "capability": capability,
            "state": "blocked",
            "error_code": "ENVIRONMENT_VALIDATION_UNCONFIRMED",
        }
        try:
            if time.monotonic() - started_at > 560:
                return request({"validation": "complete", "id": identifier, "owner": owner})
            if not previous:
                try:
                    admitted = request(
                        {"validation": "step", "id": identifier, "owner": owner, "capability": capability}
                    )
                except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                    return request({"validation": "complete", "id": identifier, "owner": owner})
                if not admitted["execute"]:
                    continue
            # 已开始的消费者可能在中央创建过临时应用；不能以目标opening失败推断未执行。
            if previous and capability == "consumer":
                raise ValueError("consumer outcome unknown; explicit new validation required")
            target = local_trial(
                root,
                staged["target"],
                spec["environment"],
                {
                    "action": "business-step",
                    "spec": spec,
                    "capability": capability,
                    "observe_only": bool(previous),
                },
            )
            if capability != "consumer" or target.get("state") == "blocked":
                result = target
            else:
                request({"validation": "continue", "id": identifier, "owner": owner})
                start = local_trial(
                    root,
                    {},
                    "",
                    {
                        "action": "consumer-start",
                        "candidate": target["candidate"],
                        "token": target["token"],
                        "question": target["question"],
                    },
                    control=True,
                )
                trial_id = start["trial_id"]
                end = time.monotonic() + 200
                try:
                    while True:
                        observed = local_trial(
                            root,
                            {},
                            "",
                            {
                                "action": "consumer-validation-evidence",
                                "trial_id": trial_id,
                                "expected_reference": target["expected_reference"],
                                "expected_scope": target["scope_fingerprint"],
                                "case_sha256": target["case_sha256"],
                            },
                            control=True,
                        )
                        if observed["status"] not in {"pending", "queued", "running"}:
                            break
                        if time.monotonic() > end:
                            raise ValueError("consumer validation unfinished")
                        time.sleep(2)
                    if observed["status"] != "completed" and observed.get("state") != "blocked":
                        raise ValueError("consumer validation failed")
                    result = {k: v for k, v in observed.items() if k != "status"}
                finally:
                    # 已完成的原取消入口返回403但仍先撤权；不能因此覆盖本轮证据。
                    with contextlib.suppress(ValueError, OSError, subprocess.SubprocessError):
                        local_trial(
                            root, {}, "", {"action": "consumer-cancel", "trial_id": trial_id}, control=True
                        )
        except (ValueError, OSError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        request(
            {
                "validation": "step",
                "id": identifier,
                "owner": owner,
                "capability": capability,
                "result": result,
            }
        )
    return request({"validation": "complete", "id": identifier, "owner": owner})
