"""三能力实际运行期间采样目标和已登记共享服务；不使用手填passed。"""

import json
import re
import subprocess
import threading
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


def consumer_wait_seconds():
    """短腿等待窗口起点信号的上限。consumer 腿起跑失败时由采样循环提前放行，
    正常情形下探针在预热结束后数秒内为真，故 150s 远足；模块级函数便于测试收紧。"""
    return 150


def consumer_probe(root, trial_id):
    """窗口起点探针：被测 run 行是否已落库（只读，不触发授权或副作用）。"""
    return bool(
        local_trial(root, {}, "", {"action": "consumer-run-started", "trial_id": trial_id}, control=True).get(
            "run_started"
        )
    )


def business_run(root, staged, spec, capability, *, consumer_running=None):
    # consumer 腿在 run 落库前有约十余秒的串行准备（租约、会话解析、候选快照与
    # claim），被测窗口从 run 创建才起算；短腿若同时发令会先跑完，测出的是两段
    # 错开的时间而非并发。故 consumer 先起跑，run 已创建后再放行其余两腿。
    if (
        capability != "consumer"
        and consumer_running is not None
        and not consumer_running.wait(timeout=consumer_wait_seconds())
    ):
        raise ValueError("consumer capacity run did not start")
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
    # consumer-start 只把作业排进执行器；临时应用导入/发布与意图分类都发生在
    # 被测 run 落库之前（ConsumerTrial.execute）。窗口起点是那个 run 的创建时刻，
    # 所以放行信号必须取自窗口本身，不能取自 consumer-start 返回。
    if consumer_running is not None:
        window_end = time.monotonic() + 150
        while not consumer_probe(root, started["trial_id"]):
            if time.monotonic() > window_end:
                raise ValueError("consumer capacity run did not start")
            time.sleep(2)
        consumer_running.set()
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
    consumer_running = threading.Event()
    with ThreadPoolExecutor(max_workers=3) as pool:
        consumer_future = pool.submit(
            business_run, root, staged, spec, "consumer", consumer_running=consumer_running
        )
        futures = [consumer_future] + [
            pool.submit(business_run, root, staged, spec, cap, consumer_running=consumer_running)
            for cap in ("offline", "order")
        ]
        while not all(f.done() for f in futures):
            # consumer 在 set 之前失败时不能让其余两腿空等；失败经 f.result() 上抛。
            if consumer_future.done() and not consumer_running.is_set():
                consumer_running.set()
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
