"""统一管家真Java工具循环；外部模型/CPMS为合成，只证明执行合同。"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
from operations.test_operator_cpms import HEADERS, ORDER, environment  # noqa: E402
from pytest import MonkeyPatch  # noqa: E402

from aiops_diagnostics.operator import operator_agent, operator_cpms, operator_tools  # noqa: E402


class Model(BaseHTTPRequestHandler):
    calls = []
    mode = "amount"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(body)
        # Only current-run tool observations; stored conversation contains no raw tool-role messages.
        observations = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        seen = {item["tool"] for item in observations}
        sequence = ["cpms_order_detail", "cpms_order_tariff", "cpms_order_billing", "cpms_order_amount_check"]
        if self.mode == "site":
            sequence = ["cpms_site_search"]
        name = next((n for n in sequence if n not in seen), None)
        if self.mode == "model_failure" and "cpms_order_detail" in seen:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"synthetic model failed"}}')
            return
        if name:
            args = (
                {"order_no": ORDER}
                if name == "cpms_order_detail"
                else {"address_terms": ["天河"]}
                if name == "cpms_site_search"
                else {}
            )
            delta = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + str(len(seen)),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            if self.mode == "site":
                reply = "找到" + observations[0]["data"]["records"][0]["name"] + "。"
            else:
                assert any(
                    item["tool"] == "cpms_order_amount_check" and item["data"]["checks"]
                    for item in observations
                )
                reply = "核验结果待验证。需补充原始证据。"
            delta = {
                "role": "assistant",
                "content": reply,
            }
            finish = "stop"
        chunk = {
            "id": "test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "synthetic-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out = args.out.absolute()
    out.mkdir(parents=True, exist_ok=True)
    classes = out / "classes"
    classes.mkdir(exist_ok=True)
    deps = Path(os.environ["M3NODE_DEPS"]).absolute()
    image = os.environ["M3NODE_JDK_IMAGE"]
    assert "@sha256:" in image and (deps / "lib").is_dir()
    shutil.copyfile(
        ROOT / "src/aiops_diagnostics/operator/operator_tools.json", classes / "operator_tools.json"
    )
    sources = [
        *ROOT.glob("services/execution-node/src/main/java/**/*.java"),
        ROOT / "tools/experiments/m2_close/m3node/NodeLinkAcceptance.java",
    ]
    (classes / "sources.txt").write_text("\n".join("/repo/" + str(p.relative_to(ROOT)) for p in sources))
    docker = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--cap-drop=ALL",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{ROOT}:/repo:ro",
        "-v",
        f"{deps}:/deps:ro",
        "-v",
        f"{classes}:/out",
    ]
    result = subprocess.run(
        [
            *docker,
            "--network=none",
            image,
            "javac",
            "-nowarn",
            "-proc:none",
            "-cp",
            "/deps/lib/*",
            "-d",
            "/out",
            "@/out/sources.txt",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    (out / "compile.log").write_text(result.stdout + result.stderr)
    result.check_returncode()
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    model_url = f"http://127.0.0.1:{model.server_port}/v1"
    cases = []
    try:
        with (
            tempfile.TemporaryDirectory(prefix="agent-loop-") as root,
            MonkeyPatch.context() as patch,
            environment(Path(root), patch) as f,
        ):
            raw = json.loads(f["manifest"].read_text())
            raw["model"] = {
                "provider_key": "openai-compatible",
                "adapter_key": "openai-compatible",
                "model_key": "synthetic-model",
                "endpoint": model_url,
            }
            raw["model_options"] = {"temperature": 0.2, "top_p": 0.9, "max_tokens": 64}
            for tool in raw["tools"]:
                declared = operator_tools.tool(tool["name"])
                tool["description"] = declared.description
                tool["input_schema"] = declared.input_schema()
            f["manifest"].write_text(json.dumps(raw))
            f["candidate"]["prompt"] = operator_agent.sop()
            f["candidate"]["enabled"] = list(operator_tools.names())
            f["publish"](f["route"].route_revision)

            def dispatch(_, config, run):
                result = subprocess.run(
                    [
                        *docker,
                        "--network=host",
                        image,
                        "java",
                        "-cp",
                        "/out:/deps/lib/*",
                        "m3node.NodeLinkAcceptance",
                        "--run-id=" + run,
                        "--gateway=" + f["gateway_base"],
                        "--model=" + model_url,
                        "--scenario=round2-500" if Model.mode == "model_failure" else "--scenario=normal",
                        "--node-token=node-token-alpha-0123456789",
                        "--managed-publication",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=140,
                )
                (out / (run + ".log")).write_text(result.stdout + result.stderr)
                result.check_returncode()

            patch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
            conversation = None
            for mode, question in [
                ("amount", "帮我查" + ORDER + "是不是多收了"),
                ("amount", "为什么这一项还是待验证？"),
                ("site", "那天河有什么站点？"),
            ]:
                Model.mode = mode
                before = len(Model.calls)
                payload = {"question": question}
                if conversation:
                    payload["conversation_id"] = conversation
                start = f["client"].post("/v1/assistant/questions", headers=HEADERS, json=payload)
                assert start.status_code == 202, start.text
                conversation = start.json()["conversation_id"]
                qa = start.json()["qa_id"]
                for _ in range(280):
                    end = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
                    if end.json().get("status") not in {"queued", "running"}:
                        break
                    time.sleep(0.5)
                assert end.json()["status"] == "completed", end.text
                run = f["ctx"].agent_run_store.by_qa(qa)
                messages = Model.calls[before]["messages"]
                if cases:
                    assert any(
                        m["role"] == "assistant" and "需补充" in m.get("content", "") for m in messages
                    )
                assert run.order_no == (ORDER if mode == "amount" else "")
                assert all(not c.get("response_format") for c in Model.calls[before:])
                cases.append(
                    {
                        "mode": mode,
                        "qa": qa,
                        "calls": len(Model.calls) - before,
                        "state": run.state,
                        "has_history": len(messages) > 2,
                    }
                )
            from aiops_diagnostics.operator import operator_answer_review

            patch.setattr(operator_answer_review, "review", lambda *a, **kw: {"verdict": "reject"})
            Model.mode = "amount"
            start = f["client"].post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={"question": "查询订单" + ORDER},
            )
            assert start.status_code == 202, start.text
            qa = start.json()["qa_id"]
            for _ in range(280):
                end = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
                if end.json().get("status") not in {"queued", "running"}:
                    break
                time.sleep(0.5)
            assert end.json()["status"] == "completed", end.text
            run = f["ctx"].agent_run_store.by_qa(qa)
            # ADR-0252：复核 reject 只记原因不拦截——答复照常交付，快照仍留 verdict。
            assert run.snapshot["delivery_mode"] == "diagnosis" and run.snapshot["answer"]
            assert run.snapshot["answer_review"]["verdict"] == "reject"
            assert "诊断结论未完成" not in end.json()["result"]["blocks"][0]["text"]
            assert not any(event["phase"] == "resample" for event in run.trace)
            cases.append({"mode": "reject_delivered", "qa": qa, "state": run.state, "resamples": 0})
            Model.mode = "model_failure"
            start = f["client"].post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": "查询订单" + ORDER}
            )
            assert start.status_code == 202, start.text
            qa = start.json()["qa_id"]
            for _ in range(280):
                end = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
                if end.json().get("status") not in {"queued", "running"}:
                    break
                time.sleep(0.5)
            assert end.json()["status"] == "completed", end.text
            run = f["ctx"].agent_run_store.by_qa(qa)
            assert run.snapshot["delivery_mode"] == "evidence_only"
            assert run.snapshot["answer_review"]["reason"] == "model_failed"
            cases.append({"mode": "model_failure_after_evidence", "qa": qa, "state": run.state})
            (out / "model-calls.json").write_text(json.dumps(Model.calls, ensure_ascii=False, indent=2))
            (out / "report.json").write_text(
                json.dumps({"cases": cases, "all_passed": True}, ensure_ascii=False, indent=2)
            )
            print(json.dumps(cases, ensure_ascii=False))
    finally:
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    main()
