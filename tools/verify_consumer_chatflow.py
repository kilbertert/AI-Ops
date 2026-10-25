#!/usr/bin/env python3
"""真 Dify Chatflow 三轮会话；模型为合成，未使用生产配置/身份/知识库。"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/m2_close/m3node"))
import verify_dify_chat_entry as stack  # noqa: E402

from aiops_diagnostics.dify_chatflow import DifyChatflowClient  # noqa: E402


class Model(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[{"id":"synthetic","object":"model"}]}')

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        messages = body.get("messages", [])
        remembered = any("枪号A7" in str(m.get("content")) for m in messages)
        answer = "请核对枪号A7。" if remembered else "请先提供枪号。"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
        self.end_headers()
        if any("本轮知识" in str(m.get("content")) for m in messages):
            answer = json.dumps({"kind": "knowledge", "faq_id": "KB-C-007"})
        if body.get("stream"):
            chunk = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "synthetic",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": None}
                ],
            }
            self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
            chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n")
        else:
            self.wfile.write(
                json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "synthetic",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": answer},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    },
                    ensure_ascii=False,
                ).encode()
            )


def chatflow_dsl(_gateway: str) -> bytes:
    doc = yaml.safe_load((ROOT / "deploy/dify-chat/consumer-order-status.dsl.yml").read_text())
    doc["app"].update(mode="advanced-chat", name="consumer-chatflow-acceptance")
    graph = doc["workflow"]["graph"]
    by_id = {n["id"]: n for n in graph["nodes"]}
    start, llm, end = by_id["start"], by_id["answer"], by_id["end"]
    start["data"]["variables"] = [
        {
            "variable": "language",
            "label": "语言",
            "type": "text-input",
            "required": True,
            "max_length": 16,
            "options": [],
        }
    ]
    llm["data"]["prompt_template"] = [
        {"role": "system", "text": "根据当前对话回答简短的充电操作指导，不执行任何业务写操作。"},
        {"role": "user", "text": "{{#sys.query#}}"},
    ]
    llm["data"]["memory"] = {
        "window": {"enabled": True, "size": 6},
        "role_prefix": {"user": "User", "assistant": "Assistant"},
        "query_prompt_template": "{{#sys.query#}}",
    }
    end["data"] = {"type": "answer", "title": "回复", "answer": "{{#answer.text#}}", "variables": []}
    graph["nodes"] = [start, llm, end]
    graph["edges"] = []
    for source, target, stype, ttype in [
        ("start", "answer", "start", "llm"),
        ("answer", "end", "llm", "answer"),
    ]:
        graph["edges"].append(
            {
                "id": f"{source}-{target}",
                "source": source,
                "target": target,
                "sourceHandle": "source",
                "targetHandle": "target",
                "type": "custom",
                "data": {"sourceType": stype, "targetType": ttype, "isInIteration": False, "isInLoop": False},
            }
        )
    return yaml.safe_dump(doc, allow_unicode=True).encode()


def isolated(args):
    root = Path(tempfile.mkdtemp(prefix="consumer-chatflow-"))
    root.chmod(0o700)
    name = "consumer-chatflow-" + secrets.token_hex(4)
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--group-add",
        str(Path("/var/run/docker.sock").stat().st_gid),
        "-e",
        f"DIFY_TEST_NAMESPACE={name}",
        "-e",
        f"DIFY_TEST_ROOT={root}",
        "-e",
        f"HOME={root}",
        "-e",
        f"PYTHONPATH={ROOT / 'src'}",
        "-v",
        f"{ROOT}:{ROOT}:ro",
        "-v",
        f"{root}:{root}",
        "-v",
        "/home/claude/miniconda3:/home/claude/miniconda3:ro",
        "-v",
        "/usr/bin/docker:/usr/bin/docker:ro",
        "-v",
        "/usr/libexec/docker/cli-plugins:/usr/libexec/docker/cli-plugins:ro",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-w",
        str(ROOT),
        "python:3.13-slim",
        sys.executable,
        str(Path(__file__).resolve()),
        "--out",
        str(root / "report.json"),
    ]
    try:
        result = subprocess.run(command, check=False)
        if (root / "report.json").exists():
            shutil.copyfile(root / "report.json", args.out)
            Path(args.out).chmod(0o600)
        print("private evidence:", root)
        return result.returncode
    finally:
        # 仅清本次创建的Dify状态卷；报告与日志保留。
        if (root / "volumes").exists():
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{root / 'volumes'}:/cleanup",
                    "busybox:latest",
                    "sh",
                    "-c",
                    "rm -rf /cleanup/*",
                ],
                check=True,
            )


class Knowledge(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append((self.path, self.headers.get("tenant-id"), json.loads(raw)))
        data = json.dumps(
            {
                "data": [
                    {
                        "kb_id": "kb-own",
                        "doc_id": "doc-1",
                        "chunk_id": "chunk-1",
                        "docnm_kwd": "KB-C-007.md",
                        "content_with_weight": "# KB-C-007\n按设备提示启动。",
                        "score": 0.9,
                    }
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def verify_knowledge(root, base, env, setup, model, check):
    kb = ThreadingHTTPServer(("127.0.0.1", 0), Knowledge)
    threading.Thread(target=kb.serve_forever, daemon=True).start()
    gateway = None
    try:
        port = stack._free_port()
        doc = yaml.safe_load((ROOT / "deploy/dify-chat/consumer-service.dsl.yml").read_text())
        doc["workflow"]["graph"]["nodes"][1]["data"]["url"] = (
            f"http://127.0.0.1:{port}/v1/internal/consumer-knowledge"
        )
        for node in doc["workflow"]["graph"]["nodes"]:
            if node["data"]["type"] == "llm":
                node["data"]["model"]["name"] = "synthetic"
        stack._dsl_for = lambda _: yaml.safe_dump(doc, allow_unicode=True).encode()
        new = stack._import_second_app(base, setup["console_headers"], f"http://127.0.0.1:{port}")
        check("knowledge Chatflow published", bool(new.get("workflow_id") and new.get("service_key")))
        content = root / "reviewed.json"
        content.write_text(
            json.dumps(
                {
                    "schema": "aiops-consumer-reviewed/1",
                    "state": "published",
                    "tenant_id": stack.TENANT,
                    "environment": "env-local",
                    "entries": {
                        "KB-C-007": {
                            "state": "approved",
                            "title": "开始充电",
                            "answers": {"zh": "按设备提示启动。", "en": "Follow the charger instructions."},
                        }
                    },
                    "support": {},
                }
            )
        )
        content.chmod(0o600)
        gateway, gateway_base, diag = stack._start_gateway(
            root=root / "knowledge-test",
            model_url=f"http://127.0.0.1:{model.server_port}/v1",
            cpms_base="http://127.0.0.1:1",
            dify_base=base,
            workflow_id=new["workflow_id"],
            key=new["service_key"],
            port=port,
            service_app_id=new["app_id"],
            service_content=str(content),
            knowledge_base=f"http://127.0.0.1:{kb.server_port}",
        )
        check("knowledge Gateway ready", not diag.get("error"))
        headers = {"Authorization": "Bearer " + stack.TOKEN, "X-Business-Entry": "consumer"}
        code, post = stack.http_json(
            gateway_base + "/v1/assistant/questions", "POST", {"question": "怎么开始充电？"}, headers
        )
        check("knowledge App admitted", code == 202)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            code, final = stack.http_json(
                gateway_base + "/v1/assistant/questions/" + post["qa_id"], headers=headers
            )
            if code != 200 or final.get("status") not in ["queued", "running"]:
                break
            time.sleep(0.1)
        check(
            "knowledge App completed from approved source",
            code == 200
            and final.get("status") == "completed"
            and final.get("result", {}).get("blocks", [{}])[0].get("text") == "按设备提示启动。",
        )
        check(
            "Dify HTTP node made one authorized retrieval",
            len(Knowledge.requests) == 1 and Knowledge.requests[0][1] == stack.TENANT,
        )
        check(
            "model received knowledge without grant",
            any("本轮知识" in json.dumps(m, ensure_ascii=False) for m in Model.requests)
            and all("cdg_" not in json.dumps(m) for m in Model.requests),
        )
    finally:
        if gateway:
            stack._stop(gateway)
        kb.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if "DIFY_TEST_NAMESPACE" not in os.environ:
        return isolated(args)
    root = Path(os.environ["DIFY_TEST_ROOT"])
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:5001"
    env = stack._dify_env(
        root,
        model_url=f"http://127.0.0.1:{model.server_port}/v1",
        port=5001,
        project="wf01" + secrets.token_hex(4),
        network="unused",
    )
    import aiops_diagnostics.dify_chatflow as transport

    event_reader = transport._events
    observed_events = []

    def capture(response, deadline):
        for event in event_reader(response, deadline):
            observed_events.append(event)
            yield event

    transport._events = capture
    report = {
        "checks": [],
        "substituted": ["model"],
        "real": ["Dify 1.17.1", "official provider", "Service API client"],
        "production_requests": 0,
    }

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)

    gateway = None
    try:
        up = stack._compose(env, "up", "-d", "--wait", "--wait-timeout", "600", "api", "worker", timeout=900)
        (root / "compose-up.log").write_text(up.stdout + up.stderr)
        check("dify ready", up.returncode == 0 and stack._wait_ready(base, time.monotonic() + 120))
        stack._dsl_for = chatflow_dsl
        setup = stack._setup_dify(
            base, env, model_url=f"http://127.0.0.1:{model.server_port}/v1", gateway_base="http://127.0.0.1:1"
        )
        report["setup"] = {k: v for k, v in setup.items() if k not in ["service_key", "console_headers"]}
        check(
            "real Chatflow published",
            setup.get("app_detail", {}).get("mode") == "advanced-chat"
            and all(s["ok"] for s in setup["steps"]),
        )
        (root / "chatflow.yml").write_bytes(chatflow_dsl(""))
        client = DifyChatflowClient(base, timeout_seconds=120)
        args_run = {
            "key": setup["service_key"],
            "workflow_id": setup["workflow_id"],
            "user": "consumer-a",
            "inputs": {"language": "zh"},
            "on_started": lambda *ids: None,
        }
        replies = []
        for query in ["我的枪号A7，怎么开始？", "我刚才说的是哪把枪？", "再说一遍枪号"]:
            reply = client.run(
                **args_run, query=query, conversation_id=replies[-1].conversation_id if replies else ""
            )
            replies.append(reply)
            check(f"turn {len(replies)} completed", "A7" in reply.answer)
        check("same upstream conversation", len({r.conversation_id for r in replies}) == 1)
        check(
            "different upstream runs/messages",
            len({r.workflow_run_id for r in replies}) == len({r.message_id for r in replies}) == 3,
        )
        other = client.run(**{**args_run, "user": "consumer-b"}, query="怎么开始？")
        check(
            "second user has separate conversation",
            other.conversation_id != replies[0].conversation_id and "A7" not in other.answer,
        )
        check(
            "model received actual prior conversation",
            any(
                len(r.get("messages", [])) >= 4 and "枪号A7" in json.dumps(r, ensure_ascii=False)
                for r in Model.requests
            ),
        )
        try:
            client.run(
                **{**args_run, "user": "consumer-b"}, query="继续", conversation_id=replies[0].conversation_id
            )
        except Exception as exc:
            from aiops_diagnostics.dify_chat_workflow import DifyWorkflowError

            check("foreign user cannot reuse upstream conversation", isinstance(exc, DifyWorkflowError))
        else:
            check("foreign user cannot reuse upstream conversation", False)
        (root / "model-requests.json").write_text(json.dumps(Model.requests, ensure_ascii=False, indent=2))
        gateway, gateway_base, diag = stack._start_gateway(
            root=root,
            model_url=f"http://127.0.0.1:{model.server_port}/v1",
            cpms_base="http://127.0.0.1:1",
            dify_base=base,
            workflow_id=setup["workflow_id"],
            key=setup["service_key"],
            port=stack._free_port(),
            service_app_id=setup["app_id"],
        )
        check("real Gateway ready", not diag.get("error"))
        headers = {"Authorization": "Bearer " + stack.TOKEN, "X-Business-Entry": "consumer"}
        conversation = ""
        qa_ids = []
        for query in ["我的枪号A7，怎么开始？", "我刚才说的是哪把枪？", "再说一遍枪号"]:
            body = {"question": query, **({"conversation_id": conversation} if conversation else {})}
            code, post = stack.http_json(gateway_base + "/v1/assistant/questions", "POST", body, headers)
            check(f"App turn {len(qa_ids) + 1} admitted", code == 202)
            conversation = post["conversation_id"]
            qa = post["qa_id"]
            qa_ids.append(qa)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                code, final = stack.http_json(gateway_base + "/v1/assistant/questions/" + qa, headers=headers)
                if code != 200 or final.get("status") not in ("queued", "running"):
                    break
                time.sleep(0.1)
            check(
                f"App turn {len(qa_ids)} completed",
                code == 200
                and final.get("status") == "completed"
                and "A7" in json.dumps(final, ensure_ascii=False),
            )
        code, denied = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "继续", "conversation_id": conversation},
            {**headers, "Authorization": "Bearer " + stack.OTHER_TOKEN},
        )
        check("App foreign conversation refused", code == 404)
        code, history = stack.http_json(gateway_base + "/v1/conversations/" + conversation, headers=headers)
        check(
            "App history contains three verified answers", code == 200 and len(history.get("turns", [])) == 3
        )
        verify_knowledge(root, base, env, setup, model, check)
        report["passed"] = True
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["passed"] = False
    finally:
        if gateway is not None:
            stack._stop(gateway)
        (root / "sse-events.json").write_text(json.dumps(observed_events, ensure_ascii=False, indent=2))
        logs = stack._compose(env, "logs", "--no-color", "api", timeout=60)
        (root / "api.log").write_text(logs.stdout + logs.stderr)
        down = stack._compose(env, "down", "-v", "--remove-orphans", timeout=180)
        report["cleanup_ok"] = down.returncode == 0
        model.shutdown()
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "passed": report.get("passed"),
                    "checks": report["checks"],
                    "error": report.get("error"),
                    "root": str(root),
                },
                ensure_ascii=False,
            )
        )
    return 0 if report.get("passed") and report.get("cleanup_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
