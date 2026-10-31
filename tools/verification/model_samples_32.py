#!/usr/bin/env python3
"""在 41 上用**真实配置的模型**跑固定的 32 次样本（两端各 8 场景 × 2 风格）。

判据与线上同一套校验器：客户端 `consumer_knowledge.final_answer`，
管家 `operator_cpms.validate_answer`。模型输出原样保留，不重试、不挑选。

用法（在能 ssh 到 41 的开发机上）：
    uv run python tools/verification/model_samples_32.py --out <私有目录>

凭据从 41 的服务环境现场读取，**不打印**；结果目录必须是空目录，避免覆盖旧证据。
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ---- 固定场景（先冻结，跑之前不看模型输出）--------------------------------
SNAPSHOT = {
    "order_no": "123456789012345",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T00:00:00Z",
    "order_amount": "27.36",
    "recorded_payment_amount": "20",
    "recorded_refund_amount": "0",
    "refund_status": "0",
    "status_code": "1",
    "status_role": "finished",
}
STATUS_SNAPSHOT = {
    "order_no": "123456789012345",
    "source": "cpms.chargingInfo",
    "observed_at": "2026-10-30T00:00:00Z",
    "status_code": "1",
    "status_role": "finished",
}
KNOWLEDGE_SOURCE = {
    "reference_id": "s1",
    "retrieved_reference_id": "doc-s1",
    "title": "启动指南",
    "content": (
        "插好充电枪，在App扫描设备二维码，确认枪口后点击开始充电。遇到错误提示时先记录提示，不要反复启动。"
    ),
    "media": [],
}
IMAGE_SOURCE = {
    "reference_id": "s2",
    "retrieved_reference_id": "doc-s2",
    "title": "充电枪故障处理",
    "content": "枪锁异常时先按充电桩上的急停，再联系现场运维。",
    "media": [{"kind": "image", "url": "https://example.invalid/gun.png", "type": "image"}],
}

STYLES = {
    "concise": "用简洁直接的语气，先说结论，不寒暄，不超过三句话。",
    "warm": "用亲切自然的语气，先回应用户的处境，再用日常语言解释，必要时温和追问。",
}

CLIENT_CASES = [
    ("knowledge", "怎么开始充电？", {"mode": "documents", "status": "found", "sources": [KNOWLEDGE_SOURCE]}),
    (
        "knowledge_image",
        "充电枪锁住了怎么办？",
        {"mode": "documents", "status": "found", "sources": [IMAGE_SOURCE]},
    ),
    (
        "billing",
        "查一下这单的账单。",
        {"mode": "documents", "status": "live", "fact_contract": "slots", "live_snapshot": SNAPSHOT},
    ),
    (
        "status",
        "我的订单现在什么状态？",
        {"mode": "documents", "status": "live", "fact_contract": "slots", "live_snapshot": STATUS_SNAPSHOT},
    ),
    (
        "clarify",
        "我想问个问题。",
        {"mode": "documents", "status": "notice", "notice_kind": "clarify", "sources": []},
    ),
    ("not_found", "这个功能的说明呢？", {"mode": "documents", "status": "no_result", "sources": []}),
    (
        "multi_order",
        "我最近两单的情况？",
        {"mode": "documents", "status": "live", "fact_contract": "slots", "live_snapshot": SNAPSHOT},
    ),
    (
        "knowledge_en",
        "How do I start charging?",
        {"mode": "documents", "status": "found", "sources": [KNOWLEDGE_SOURCE]},
    ),
]

OPERATOR_ORDER = {
    "order_no": "123456789012345",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T00:00:00Z",
    "order_amount": "27.36",
    "recorded_payment_amount": "20",
    "recorded_refund_amount": "0",
    "status_code": "1",
    "status_role": "finished",
}
OPERATOR_SITES = {
    "sites": [
        {"name": "峨眉山龙栖电力充电站", "address": "峨眉山市胜利镇", "distance_m": 850},
        {"name": "趋势智能停车场站", "address": "峨眉山市绥山镇", "distance_m": 1600},
    ],
    "truncated": False,
}

OPERATOR_CASES = [
    ("consult_status_meaning", "状态码 1 是什么意思？", {"tool": None}),
    ("consult_howto", "怎么帮客户重新开一张单？", {"tool": None}),
    (
        "diagnose_order",
        "帮我看看 123456789012345 这单有没有异常。",
        {"tool": "cpms_order_detail", "evidence": OPERATOR_ORDER},
    ),
    (
        "diagnose_billing",
        "这单为什么收了 27.36？",
        {"tool": "cpms_order_detail", "evidence": OPERATOR_ORDER},
    ),
    ("site_search", "附近有哪些站点？", {"tool": "cpms_sites", "evidence": OPERATOR_SITES}),
    ("site_empty", "附近有哪些站点？", {"tool": "cpms_sites", "evidence": {"sites": [], "truncated": False}}),
    (
        "diagnose_en",
        "Check order 123456789012345 for anomalies.",
        {"tool": "cpms_order_detail", "evidence": OPERATOR_ORDER},
    ),
    (
        "site_no_match",
        "找个叫火星站的地方。",
        {"tool": "cpms_sites", "evidence": {"sites": [], "truncated": False}},
    ),
]

# 在 41 上执行的调用体：只读服务自身的 env，不落盘、不打印密钥。
REMOTE = r"""
import json, os, sys
import urllib.request

RUNTIME_ENV = "/etc/aiops-node-runtime/runtime.env"


def runtime_value(name, default=""):
    try:
        with open(RUNTIME_ENV, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(name, default)


side = sys.argv[1]
payload = json.load(sys.stdin)
base = runtime_value("AIOPS_MODEL_UPSTREAM").rstrip("/") + (
    runtime_value("AIOPS_MODEL_BASE_PATH", "/v1") or "/v1"
)
with open("/etc/aiops-node-runtime/model-proxy/upstream_api_key", encoding="utf-8") as handle:
    key = handle.read().strip()
body = {
    "model": os.environ.get("AIOPS_SAMPLE_MODEL", "deepseek-v4-flash"),
    "messages": payload["messages"],
    "stream": False,
    "max_tokens": 2000,
    "response_format": {"type": "json_object"},
}
req = urllib.request.Request(
    base + "/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=120) as r:
    print(r.read().decode())
"""


def build_client_prompt(question, context, style):
    sys.path.insert(0, str(ROOT / "src"))
    from aiops_diagnostics.consumer.consumer_documents import _protocol_block

    return (
        "你是 C 端充电客服。只输出一个 JSON 对象。\n"
        + STYLES[style]
        + "\n"
        + _protocol_block()
        + "\n用户问题："
        + question
        + "\n本轮取数结果："
        + json.dumps(context, ensure_ascii=False)
    )


def build_operator_prompt(question, case_context, style):
    sys.path.insert(0, str(ROOT / "src"))
    from aiops_diagnostics.execution.node_publication import OPERATOR_OUTPUT

    if case_context.get("tool"):
        evidence = json.dumps(case_context["evidence"], ensure_ascii=False)
        tool_note = f"本轮已调用工具 {case_context['tool']}，结果：{evidence}"
    else:
        tool_note = "本轮未调用任何工具。"
    return (
        "你是充电场站运营助手。"
        + STYLES[style]
        + "\n"
        + OPERATOR_OUTPUT
        + "\n运营的问题："
        + question
        + "\n"
        + tool_note
    )


def call_remote(side, messages):
    payload = json.dumps({"messages": messages})
    # `dev-host exec` 用空格重新拼接 argv 交给远端 shell，所以整条命令必须是一个
    # **已引用好**的参数；拆成多个 argv 会让脚本在第一个换行处被拆散。
    remote_python = "/opt/aiops-41/.venv/bin/python"
    command = shlex.join([remote_python, "-c", REMOTE, side])
    proc = subprocess.run(
        [
            "dev-host",
            "exec",
            "41",
            "--allow-service-exec",
            "--timeout",
            "150",
            "--",
            command,
        ],
        input=payload,
        text=True,
        capture_output=True,
        timeout=170,
    )
    if proc.returncode != 0:
        return None, (proc.stderr or "")[-600:]
    return json.loads(proc.stdout), None


def judge_client(raw_text, context, language):
    sys.path.insert(0, str(ROOT / "src"))
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    final_answer(raw_text, context, language, None)
    return True, ""


def judge_operator(raw_text, evidence):
    sys.path.insert(0, str(ROOT / "src"))
    from aiops_diagnostics.operator.operator_cpms import validate_answer

    validate_answer(json.loads(raw_text), evidence if evidence else {})
    return True, ""


def run_one(job):
    side, name, question, context, style = job
    language = "en" if name.endswith("_en") else "zh"
    if side == "client":
        prompt = build_client_prompt(question, context, style)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": question}]
    else:
        prompt = build_operator_prompt(question, context, style)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": question}]

    raw, err = call_remote(side, messages)
    record = {"side": side, "case": name, "style": style, "language": language}
    if err:
        record.update(passed=False, stage="call", error=err)
        return record
    record["model"] = raw.get("model")
    record["usage"] = raw.get("usage")
    text = raw["choices"][0]["message"]["content"]
    record["output"] = text
    try:
        if side == "client":
            judge_client(text, context, language)
        else:
            judge_operator(text, context.get("evidence"))
        record.update(passed=True, stage="ok")
    except Exception as exc:  # noqa: BLE001 - 失败原样记录
        record.update(passed=False, stage="judge", error=f"{type(exc).__name__}: {exc}")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        parser.error("输出目录必须为空，不能覆盖旧证据")
    out.mkdir(parents=True, exist_ok=True)

    jobs = [("client", n, q, c, s) for n, q, c in CLIENT_CASES for s in STYLES]
    jobs += [("operator", n, q, c, s) for n, q, c in OPERATOR_CASES for s in STYLES]
    assert len(jobs) == 32, f"样本数应为 32，实际 {len(jobs)}"

    results, lock = [], threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for record in pool.map(run_one, jobs):
            with lock:
                results.append(record)
                (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
                print(
                    json.dumps(
                        {k: record.get(k) for k in ("side", "case", "style", "passed", "stage", "error")},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    passed = sum(1 for r in results if r.get("passed"))
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "by_side": {
            s: {
                "passed": sum(1 for r in results if r["side"] == s and r.get("passed")),
                "total": sum(1 for r in results if r["side"] == s),
            }
            for s in ("client", "operator")
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
