"""三能力实际运行期间采样目标和已登记共享服务；不使用手填passed。"""

import json
import re
import subprocess
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor

from aiops_diagnostics.gateway.environment_drafts import REFERENCE_PATTERN
from aiops_diagnostics.gateway.environment_validation_worker import local_trial


def sample(root, hosts):
    declared = tomllib.loads((root / ".dev-host.toml").read_text())["targets"]
    result = []
    for row in hosts:
        if set(row) != {"host", "service", "kind"} or row["kind"] not in {"systemd", "docker"}:
            raise ValueError("capacity target invalid")
        matches = [
            r["name"] for r in declared if r.get("name") == row["host"] or r.get("host") == row["host"]
        ]
        if (
            len(matches) != 1
            or not REFERENCE_PATTERN.fullmatch(matches[0])
            or not re.fullmatch(
                r"aiops[-a-zA-Z0-9_.]{1,100}|m2live03-(node|platform|gateway)|docker-(api|worker|plugin_daemon)-1",
                row["service"],
            )
        ):
            raise ValueError("capacity target not project service")
        script = "\n".join(
            [
                "import json,subprocess",
                "from pathlib import Path",
                'memory_lines=Path("/proc/meminfo").read_text().splitlines()',
                'line=next(x for x in memory_lines if x.startswith("MemAvailable:"))',
                "available=int(line.split()[1])*1024",
                f"kind={row['kind']!r};service={row['service']!r}",
                'if kind=="systemd":',
                ' fields=["-p","MemoryCurrent","-p","MemoryPeak","-p","MemoryMax"]',
                ' p=subprocess.check_output(["systemctl","show",service,*fields],text=True)',
                ' v=dict(line.split("=",1) for line in p.splitlines())',
                ' memory=int(v["MemoryCurrent"])',
                ' limit=int(v["MemoryMax"]) if v["MemoryMax"].isdigit() else 0',
                ' peak=int(v.get("MemoryPeak",memory))',
                "else:",
                ' v=json.loads(subprocess.check_output(["docker","inspect",service],text=True))[0]',
                ' assert v["State"]["Running"] and not v["State"].get("OOMKilled")',
                ' pid=v["State"]["Pid"];limit=int(v["HostConfig"]["Memory"])',
                ' group=Path(f"/proc/{pid}/cgroup").read_text().strip().split("::")[1].lstrip("/")',
                ' cg=Path("/sys/fs/cgroup")/group',
                ' memory=int((cg/"memory.current").read_text());peak=int((cg/"memory.peak").read_text())',
                "assert memory>0 and limit>=0",
                'print(json.dumps({"memory_bytes":memory,"peak_bytes":peak,"limit_bytes":limit,"available_bytes":available}))',
            ]
        )
        out = subprocess.check_output(
            ["dev-host", "exec", matches[0], "--allow-service-exec", "--timeout", "15", "--", "python3 -"],
            cwd=root,
            input=script,
            text=True,
            timeout=20,
        )
        value = json.loads(out)
        if value["available_bytes"] <= 0 or (
            value["limit_bytes"] and value["limit_bytes"] <= value["memory_bytes"]
        ):
            raise ValueError("capacity exhausted")
        result.append({"target": row, **value, "at": time.time()})
    return result


def business_run(root, staged, spec, capability):
    target = local_trial(
        root,
        staged["request"]["target"],
        spec["environment"],
        {"action": "business-step", "spec": spec, "capability": capability, "observe_only": False},
    )
    if target.get("state") == "blocked":
        return target
    if capability != "consumer":
        window = local_trial(
            root,
            staged["request"]["target"],
            spec["environment"],
            {
                "action": "publication-run-window",
                "app_id": spec["operator_app"],
                "trial_id": target["evidence"]["trial_id"],
                "entry": "operator",
            },
        )
        return {**target, "window": window}
    started = local_trial(
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
    end = time.monotonic() + 200
    while True:
        result = local_trial(
            root,
            {},
            "",
            {
                "action": "consumer-validation-evidence",
                "trial_id": started["trial_id"],
                "expected_reference": target["expected_reference"],
                "expected_scope": target["scope_fingerprint"],
                "case_sha256": target["case_sha256"],
            },
            control=True,
        )
        if result["status"] not in {"running", "queued", "pending"}:
            if result.get("state") != "passed":
                return result
            window = local_trial(
                root,
                {},
                "",
                {
                    "action": "publication-run-window",
                    "entry": "consumer",
                    "trial_id": started["trial_id"],
                    "app_id": spec["consumer"]["app_id"],
                },
                control=True,
            )
            return {**result, "window": window}
        if time.monotonic() > end:
            raise ValueError("capacity business unfinished")
        time.sleep(2)


def run(root, staged, spec, reference):
    if (
        not isinstance(reference, dict)
        or set(reference) != {"shared"}
        or not isinstance(reference["shared"], list)
        or not reference["shared"]
    ):
        raise ValueError("shared capacity configuration required")
    target = {
        "host": staged["request"]["target"]["host"],
        "service": "aiops-access-" + spec["environment"] + ".service",
        "kind": "systemd",
    }
    hosts = [target, *reference["shared"]]
    idle = sample(root, hosts)
    # 由原安装器在真正旧版+候选共存时生成，不接页面/人工passed文件。
    script = "\n".join(
        [
            "import json",
            "from pathlib import Path",
            f"root=Path('/opt/aiops-access-{spec['environment']}')",
            "receipt=root/'installed.json'",
            "assert receipt.stat().st_uid==0 and receipt.stat().st_mode&0o077==0",
            "current=json.loads(receipt.read_text())",
            f"assert current['revision']=={spec['installed']['source_revision']!r}",
            "p=root/'current/capacity.json'",
            "assert not p.is_symlink() and p.stat().st_uid==0 and p.stat().st_mode&0o077==0",
            "print(p.read_text())",
        ]
    )
    declared = tomllib.loads((root / ".dev-host.toml").read_text())["targets"]
    host = next(
        r["name"] for r in declared if r.get("name") == target["host"] or r.get("host") == target["host"]
    )
    observed = json.loads(
        subprocess.check_output(
            ["dev-host", "exec", host, "--allow-service-exec", "--timeout", "15", "--", "python3 -"],
            cwd=root,
            input=script,
            text=True,
            timeout=20,
        )
    )
    if (
        observed.get("revision") != spec["installed"]["source_revision"]
        or observed.get("environment") != spec["environment"]
        or not 0 <= time.time() - observed.get("sampled_at", 0) <= 86400
        or len(observed.get("services", [])) != 2
        or any(row["memory_bytes"] <= 0 or row["pid"] <= 0 for row in observed["services"])
    ):
        raise ValueError("matching actual upgrade overlap evidence required")
    samples = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(business_run, root, staged, spec, cap) for cap in ("consumer", "offline", "order")
        ]
        while not all(f.done() for f in futures):
            samples.append(sample(root, hosts))
            time.sleep(2)
        outcomes = [f.result() for f in futures]
    if (
        any(r.get("state") != "passed" for r in outcomes)
        or not samples
        or max(r["window"]["started_at"] for r in outcomes)
        >= min(r["window"]["completed_at"] for r in outcomes)
    ):
        raise ValueError("concurrent business capacity proof incomplete")
    if any(
        (r["limit_bytes"] and r["memory_bytes"] >= r["limit_bytes"] * 0.8) or r["available_bytes"] < 1024**3
        for batch in [idle, *samples]
        for r in batch
    ):
        raise ValueError("concurrent capacity headroom insufficient")
    if sum(row["memory_bytes"] for row in observed["services"]) >= idle[0]["available_bytes"]:
        raise ValueError("upgrade overlap headroom insufficient")
    return {
        "state": "passed",
        "idle": idle,
        "peak": [max(batch[i]["memory_bytes"] for batch in samples) for i in range(len(hosts))],
        "samples": len(samples),
        "overlap_memory_bytes": sum(row["memory_bytes"] for row in observed["services"]),
        "runs": [r["evidence"]["run_id"] for r in outcomes],
        "overlap": {
            "started_at": max(r["window"]["started_at"] for r in outcomes),
            "completed_at": min(r["window"]["completed_at"] for r in outcomes),
        },
        "observed_at": time.time(),
    }


def run_window(context, payload):
    if set(payload) != {"entry", "app_id", "trial_id"}:
        raise ValueError("invalid capacity run window")
    if payload["entry"] == "operator":
        from .environment_trial import read

        result = read(context, {"app_id": payload["app_id"], "trial_id": payload["trial_id"]})
        run = context.agent_run_store.by_qa(payload["trial_id"])
        started = run.claimed_at if run else None
    elif payload["entry"] == "consumer":
        result = context.consumer_candidate_trials.read({"trial_id": payload["trial_id"]})
        adapter = context.consumer_candidate_trials.leases[payload["trial_id"]]
        if adapter.scope["app_id"] != payload["app_id"]:
            raise ValueError("capacity app mismatch")
        run = context.dify_run_store.by_qa(payload["trial_id"])
        started = run.created_at if run else None
    else:
        raise ValueError("invalid capacity entry")
    if result["status"] != "completed" or not run or not started or not run.completed_at:
        raise ValueError("capacity run window unavailable")
    return {"started_at": started.timestamp(), "completed_at": run.completed_at.timestamp()}
