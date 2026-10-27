#!/usr/bin/env python3
"""真 Dify Chatflow 三轮会话；模型为合成，未使用生产配置/身份/知识库。"""

from __future__ import annotations

import argparse
import base64
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
        if any(
            "本轮知识" in str(m.get("content")) or "本轮取数结果" in str(m.get("content")) for m in messages
        ):
            last = str(
                next(
                    m.get("content")
                    for m in reversed(messages)
                    if m.get("role") == "user"
                    and ("本轮知识" in str(m.get("content")) or "本轮取数结果" in str(m.get("content")))
                )
            )
            answer = json.dumps(
                {"kind": "realtime", "faq_id": ""}
                if "live_snapshot" in last
                else {"kind": "knowledge", "faq_id": "KB-C-007"}
            )
        if any("仅识别图片" in str(m.get("content")) for m in messages):
            answer = json.dumps({"readable": True, "text": "Startup failed."})
        if body.get("response_format", {}).get("json_schema", {}).get("name") == "consumer_intent":
            # 固定合成输入只证明传输接线，语义质量由真实供应商语料另验。
            query = json.loads(messages[-1]["content"])
            question = query["question"]
            intent, reference = {
                "我上一个订单现在是什么情况": ("order_status", "latest"),
                "看看选定这单的情况": ("order_status", "explicit"),
                "帮我查看最新的订单的情况": ("order_status", "latest"),
                "查询我的最新订单退款记录": ("billing_record", "latest"),
            }.get(question, ("knowledge", "none"))
            answer = json.dumps(
                {
                    "intent": intent,
                    "order_reference": reference,
                    "uses_context": bool(query["previous_question"]) and intent == "knowledge",
                }
            )
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
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )

    def do_GET(self):
        if self.path != "/kb/documents/images/accepted-image":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.image_bytes)))
        self.end_headers()
        self.wfile.write(self.image_bytes)

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
    import sqlite3
    import uuid

    def node_trace(qa_id, expected):
        with sqlite3.connect(root / "knowledge-test/gateway/data/gateway.db") as connection:
            run_id = connection.execute(
                "SELECT workflow_run_id FROM chat_dify_runs WHERE qa_id=?", (qa_id,)
            ).fetchone()[0]
        uuid.UUID(run_id)
        sql = (
            f"SELECT node_id FROM workflow_node_executions WHERE workflow_run_id='{run_id}' "
            "AND node_id IN ('knowledge','cpms_status','cpms_billing') AND status='succeeded'"
        )
        result = stack._compose(
            env,
            "exec",
            "-T",
            "db_postgres",
            "psql",
            "-U",
            env["DB_USERNAME"],
            "-d",
            env["DB_DATABASE"],
            "-At",
            "-c",
            sql,
            timeout=30,
        )
        check("actual Dify node " + expected, result.returncode == 0 and result.stdout.strip() == expected)

    cpms = stack._serve(stack.CpmsHandler)
    kb = ThreadingHTTPServer(("127.0.0.1", 0), Knowledge)
    threading.Thread(target=kb.serve_forever, daemon=True).start()
    gateway = None
    try:
        port = stack._free_port()
        doc = yaml.safe_load((ROOT / "deploy/dify-chat/consumer-service.dsl.yml").read_text())
        for node in doc["workflow"]["graph"]["nodes"]:
            if node["data"]["type"] == "http-request":
                node["data"]["url"] = node["data"]["url"].replace(
                    "http://gateway.invalid", f"http://127.0.0.1:{port}"
                )
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
                            "media_ids": ["IMG-001"],
                            "answers": {"zh": "按设备提示启动。", "en": "Follow the charger instructions."},
                        }
                    },
                    "media": {
                        "IMG-001": {
                            "state": "approved",
                            "kind": "image",
                            "mime_type": "image/png",
                            "knowledge_base_id": "kb-own",
                            "document_id": "image-document",
                            "backend_id": "accepted-image",
                            "title": "合成验收图",
                        }
                    },
                    "support": {"zh": {"label": "官方客服", "url": "https://support.example.test/"}},
                }
            )
        )
        content.chmod(0o600)
        gateway, gateway_base, diag = stack._start_gateway(
            root=root / "knowledge-test",
            model_url=f"http://127.0.0.1:{model.server_port}/v1",
            cpms_base=f"http://127.0.0.1:{cpms.server_port}",
            dify_base=base,
            workflow_id=new["workflow_id"],
            key=new["service_key"],
            port=port,
            service_app_id=new["app_id"],
            service_content=str(content),
            service_live_order=True,
            service_billing=True,
            service_photo=True,
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
        media = [b["media"] for b in (final.get("result") or {}).get("blocks", []) if b["kind"] == "image"]
        check("approved image block returned", len(media) == 1)
        import urllib.request

        with urllib.request.urlopen(gateway_base + media[0]["url"], timeout=10) as response:
            check(
                "signed image endpoint returns original bytes",
                response.status == 200 and response.read() == Knowledge.image_bytes,
            )
        (root / "knowledge-answer.json").write_text(json.dumps(final, ensure_ascii=False))
        check(
            "Dify HTTP node made one authorized retrieval",
            len(Knowledge.requests) == 1 and Knowledge.requests[0][1] == stack.TENANT,
        )
        check(
            "model received knowledge without grant",
            any("本轮取数结果" in json.dumps(m, ensure_ascii=False) for m in Model.requests)
            and all("cdg_" not in json.dumps(m) for m in Model.requests),
        )
        code, followup = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "还是不行", "conversation_id": post["conversation_id"]},
            headers,
        )
        check("first unresolved feedback admitted", code == 202)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            code, final = stack.http_json(
                gateway_base + "/v1/assistant/questions/" + followup["qa_id"], headers=headers
            )
            if code != 200 or final.get("status") not in ["queued", "running"]:
                break
            time.sleep(0.1)
        check(
            "first feedback asks for step and error",
            code == 200
            and final.get("status") == "completed"
            and "哪一步" in final.get("result", {}).get("blocks", [{}])[0].get("text", ""),
        )
        check(
            "followup retrieval retained original topic",
            len(Knowledge.requests) == 2
            and Knowledge.requests[-1][2]["question"] == "怎么开始充电？\n还是不行",
        )
        node_trace(post["qa_id"], "knowledge")
        calls = len(Model.requests), len(Knowledge.requests)
        code, escalated = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "还是不行", "conversation_id": post["conversation_id"]},
            headers,
        )
        check(
            "second feedback offers support without more upstream calls",
            code == 200
            and "https://support.example.test/" in escalated.get("message", "")
            and "已转接" not in escalated.get("message", "")
            and calls == (len(Model.requests), len(Knowledge.requests)),
        )
        knowledge_calls = len(Knowledge.requests)
        code, live = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "我上一个订单现在是什么情况", "conversation_id": post["conversation_id"]},
            headers,
        )
        check("live order Chatflow admitted", code == 202)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            code, final = stack.http_json(
                gateway_base + "/v1/assistant/questions/" + live["qa_id"], headers=headers
            )
            if code != 200 or final.get("status") not in ["queued", "running"]:
                break
            time.sleep(0.1)
        text = (final.get("result") or {}).get("blocks", [{}])[0].get("text", "")
        check(
            "live order returns same round CPMS state",
            code == 200
            and final.get("status") == "completed"
            and stack.ORDER_LATEST in text
            and "cpms.chargingInfo" in text
            and "DO-NOT-LEAK" not in text,
        )
        check(
            "live order reads once without knowledge",
            len(stack.CpmsHandler.calls) == 1 and len(Knowledge.requests) == knowledge_calls,
        )
        node_trace(live["qa_id"], "cpms_status")
        with sqlite3.connect(root / "knowledge-test/gateway/data/gateway.db") as connection:
            connection.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
        before = len(Model.requests), len(Knowledge.requests), len(stack.CpmsHandler.calls)
        code, feedback = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "还是不行", "conversation_id": post["conversation_id"]},
            headers,
        )
        check(
            "expired feedback asks to restate without upstream calls",
            code == 200
            and feedback.get("code") == "CONSUMER_CONTEXT"
            and before == (len(Model.requests), len(Knowledge.requests), len(stack.CpmsHandler.calls)),
        )
        code, fresh = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "帮我查看最新的订单的情况", "conversation_id": post["conversation_id"]},
            headers,
        )
        check(
            "expired previous run accepts independent new order query",
            code == 202 and fresh["conversation_id"] == post["conversation_id"],
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            code, final = stack.http_json(
                gateway_base + "/v1/assistant/questions/" + fresh["qa_id"], headers=headers
            )
            if code != 200 or final.get("status") not in ["queued", "running"]:
                break
            time.sleep(0.1)
        check(
            "fresh query reads new CPMS snapshot",
            code == 200 and final.get("status") == "completed" and len(stack.CpmsHandler.calls) == 2,
        )
        check(
            "old expired answer still refused",
            stack.http_json(gateway_base + "/v1/assistant/questions/" + live["qa_id"], headers=headers)[0]
            == 410,
        )
        code, bill = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "查询我的最新订单退款记录"},
            headers,
        )
        check("billing Chatflow admitted", code == 202)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            code, final = stack.http_json(
                gateway_base + "/v1/assistant/questions/" + bill["qa_id"], headers=headers
            )
            if code != 200 or final.get("status") not in ["queued", "running"]:
                break
            time.sleep(0.1)
        text = (final.get("result") or {}).get("blocks", [{}])[0].get("text", "")
        check(
            "billing returns record without claiming bank receipt",
            code == 200
            and final.get("status") == "completed"
            and "部分退款记录" in text
            and "1.10" in text
            and "不证明银行到账" in text
            and "未提供币种" in text
            and "DO-NOT-LEAK" not in text,
        )
        check(
            "billing reads once without knowledge",
            len(stack.CpmsHandler.calls) == 3 and len(Knowledge.requests) == knowledge_calls,
        )
        node_trace(bill["qa_id"], "cpms_billing")
        import io

        from PIL import Image

        photo = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(photo, format="JPEG")
        request = urllib.request.Request(
            gateway_base + "/v1/assistant/photos",
            data=photo.getvalue(),
            headers={**headers, "Content-Type": "image/jpeg"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            uploaded = json.load(response)
        check("original App photo upload accepted", bool(uploaded.get("photo_id")))
        status, photo_qa = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "这张图的提示怎么办？", "photo_id": uploaded["photo_id"]},
            headers,
        )
        check("photo question admitted", status == 202)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status, photo_result = stack.http_json(
                gateway_base + "/v1/assistant/questions/" + photo_qa["qa_id"], headers=headers
            )
            if status != 200 or photo_result.get("status") not in {"queued", "running"}:
                break
            time.sleep(0.1)
        check(
            "photo reaches real Dify then grounded answer",
            status == 200 and photo_result.get("status") == "completed",
        )
        check(
            "Dify vision actually received image content",
            any("image_url" in json.dumps(m) for m in Model.requests),
        )
        check(
            "visual clue reaches current retrieval",
            "Startup failed." in Knowledge.requests[-1][2]["question"],
        )
        # 原运营发布接口必须自己冻结内容并发布Dify图，验收不代做该步骤。
        from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
        from aiops_diagnostics.agent_manifest import admin_context
        from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore

        stack._stop(gateway)
        gateway = None
        case = root / "knowledge-test/gateway"
        database = case / "data/gateway.db"
        manager = AgentManager(AgentStore(database))
        admin = admin_context(stack.TENANT)
        agent = manager.create(
            admin,
            name="WF01 acceptance",
            description="test",
            config=AgentConfig(
                agent_type="customer",
                prompt="original",
                knowledge_base_ids=(),
                model="aiops-api",
                output_contract="blocks-v1",
            ),
        )
        manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
        stack._seed_registry(
            database,
            [
                {
                    "tenant_id": stack.TENANT,
                    "business_entry": "consumer",
                    "app_id": new["app_id"],
                    "agent_name": "WF01 acceptance",
                }
            ],
        )
        workspace = stack._console_workspace(base, setup["console_headers"])
        check(
            "publication workspace seeded",
            stack._seed_workspace(database, stack.TENANT, workspace, "dify-chat-acceptance"),
        )
        key = case / "keys" / (new["app_id"] + ".key")
        key.write_text(new["service_key"])
        key.chmod(0o600)
        target = root / "consumer-targets.json"
        target.write_text(
            json.dumps(
                {
                    "schema": "aiops-consumer-targets/1",
                    "targets": [
                        {
                            "app_id": new["app_id"],
                            "tenant_id": stack.TENANT,
                            "environment": "env-local",
                            "content_file": str(content),
                            "knowledge_base_ids": ["kb-own"],
                            "live_order_status": True,
                            "live_billing_record": True,
                        }
                    ],
                }
            )
        )
        target.chmod(0o600)
        gateway, gateway_base, diag = stack._start_gateway(
            root=root / "knowledge-test",
            model_url=f"http://127.0.0.1:{model.server_port}/v1",
            cpms_base=f"http://127.0.0.1:{cpms.server_port}",
            dify_base=base,
            workflow_id=new["workflow_id"],
            key=new["service_key"],
            port=port,
            console_key=stack.ADMIN_API_KEY,
            managed_route=True,
            service_app_id=new["app_id"],
            knowledge_base=f"http://127.0.0.1:{kb.server_port}",
            service_publication_target=str(target),
        )
        check("managed consumer Gateway ready", not diag.get("error"))
        publish_headers = {"Authorization": "Bearer " + stack.PUBLISH_KEY}

        def action(name, **fields):
            return stack.http_json(
                gateway_base + "/v1/dify/" + name,
                "POST",
                {"app_id": new["app_id"], **fields},
                publish_headers,
            )

        def question(text="怎么开始充电？"):
            status, qa = stack.http_json(
                gateway_base + "/v1/assistant/questions", "POST", {"question": text}, headers
            )
            if status != 202:
                return status, qa
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                status, result = stack.http_json(
                    gateway_base + "/v1/assistant/questions/" + qa["qa_id"], headers=headers
                )
                if status != 200 or result.get("status") not in {"queued", "running"}:
                    return status, result
                time.sleep(0.1)
            raise RuntimeError("published question timed out")

        def text_of(result):
            return (result.get("result") or {}).get("blocks", [{}])[0].get("text", "")

        status, first = action("publish")
        check("operator publishes WF01", status == 200 and first.get("action") == "synced")
        v1 = first["version_no"]
        check("published content is executed", text_of(question()[1]) == "按设备提示启动。")
        draft = json.loads(content.read_text())
        draft["entries"]["KB-C-007"]["answers"]["zh"] = "第二版已审核操作指引。"
        content.write_text(json.dumps(draft))
        check("saving content does not switch App", text_of(question()[1]) == "按设备提示启动。")
        routes = ChatRouteBindingStore(database)
        before = routes.get("env-local", stack.TENANT, "consumer", "consumer_service")
        trial_headers = {"Authorization": "Bearer " + stack.TRIAL_KEY}
        status, trial = stack.http_json(
            gateway_base + "/v1/dify/trial",
            "POST",
            {"app_id": new["app_id"], "order_no": stack.ORDER_LATEST, "question": "怎么开始充电？"},
            trial_headers,
        )
        check("operator starts real Chatflow trial", status == 202)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status, trial_result = stack.http_json(
                gateway_base + "/v1/dify/trial/" + trial["trial_id"], headers=trial_headers
            )
            if status != 200 or trial_result.get("status") not in {"queued", "running"}:
                break
            time.sleep(0.1)
        check(
            "trial executes draft without changing publication",
            status == 200
            and trial_result.get("status") == "completed"
            and trial_result.get("result", {}).get("report", {}).get("summary") == "第二版已审核操作指引。"
            and routes.get("env-local", stack.TENANT, "consumer", "consumer_service") == before,
        )
        check("App remains V1 after trial", text_of(question()[1]) == "按设备提示启动。")
        status, second = action("publish")
        check("operator publishes changed content", status == 200 and second.get("version_no") != v1)
        status, v2_answer = question()
        check("App executes V2", text_of(v2_answer) == "第二版已审核操作指引。")
        status, result = action("retract")
        check("operator retracts WF01", status == 200)
        check("retracted App does not execute", question()[0] == 410)
        check(
            "old answer is unavailable after retraction",
            stack.http_json(gateway_base + "/v1/assistant/questions/" + v2_answer["qa_id"], headers=headers)[
                0
            ]
            == 410,
        )
        status, restored = action("restore", version_no=v1)
        check("operator restores V1", status == 200 and text_of(question()[1]) == "按设备提示启动。")
        count = len(stack.CpmsHandler.calls)
        code, trial = stack.http_json(
            gateway_base + "/v1/dify/trial",
            "POST",
            {"app_id": new["app_id"], "order_no": stack.ORDER_LATEST, "question": "看看选定这单的情况"},
            trial_headers,
        )
        check("semantic CPMS trial admitted", code == 202)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            code, result = stack.http_json(
                gateway_base + "/v1/dify/trial/" + trial["trial_id"], headers=trial_headers
            )
            if result.get("status") not in {"queued", "running"}:
                break
            time.sleep(0.1)
        check(
            "semantic CPMS trial runs real node",
            code == 200
            and result.get("status") == "completed"
            and "cpms.chargingInfo" in result.get("result", {}).get("report", {}).get("summary", "")
            and len(stack.CpmsHandler.calls) == count + 1,
        )
        # 草稿开关独立于服务器能力上限；发布才切实际App行为。
        draft_url = base + "/console/api/apps/" + new["app_id"] + "/workflows/draft"
        code, flow = stack.http_json(draft_url, headers=setup["console_headers"])
        assert code == 200
        for entry in flow["environment_variables"]:
            if entry["name"] == "cpms_status_enabled":
                entry["value"] = "false"
        code, _ = stack.http_json(
            draft_url,
            "POST",
            {key: flow[key] for key in ("graph", "features", "hash", "conversation_variables")}
            | {"environment_variable_patch": {"environment_variables": flow["environment_variables"]}},
            setup["console_headers"],
        )
        check("operator saves disabled draft", code == 200)
        code, old = question("我上一个订单现在是什么情况")
        check(
            "saving flag does not disable published CPMS",
            code == 200 and old.get("status") == "completed" and "cpms.chargingInfo" in text_of(old),
        )
        count = len(stack.CpmsHandler.calls)
        code, trial = stack.http_json(
            gateway_base + "/v1/dify/trial",
            "POST",
            {"app_id": new["app_id"], "order_no": stack.ORDER_LATEST, "question": "看看选定这单的情况"},
            trial_headers,
        )
        check("disabled draft trial accepted", code == 202)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            code, result = stack.http_json(
                gateway_base + "/v1/dify/trial/" + trial["trial_id"], headers=trial_headers
            )
            if result.get("status") not in {"queued", "running"}:
                break
            time.sleep(0.1)
        check(
            "disabled draft trial refuses without CPMS",
            result.get("status") == "failed"
            and result.get("error_code") == "CHATFLOW_LIVE_DISABLED"
            and "尚未开通" in result.get("error_message", "")
            and len(stack.CpmsHandler.calls) == count,
        )
        check("publish disabled capability", action("publish")[0] == 200)
        code, disabled = question("我上一个订单现在是什么情况")
        check(
            "published flag disables live CPMS",
            code == 200
            and disabled.get("code") == "CONSUMER_DISABLED"
            and len(stack.CpmsHandler.calls) == count,
        )
        check("restore capability version", action("restore", version_no=v1)[0] == 200)
        code, restored = question("我上一个订单现在是什么情况")
        check(
            "restored version executes CPMS",
            code == 200
            and restored.get("status") == "completed"
            and len(stack.CpmsHandler.calls) == count + 1,
        )

    finally:
        import sqlite3

        diagnostic = root / "knowledge-test/gateway/data/gateway.db"
        if diagnostic.exists():
            with sqlite3.connect(diagnostic) as c:
                report_rows = c.execute("SELECT qa_id,status,error_code FROM assistant_questions").fetchall()
            (root / "knowledge-qa-status.json").write_text(json.dumps(report_rows))
        # 隔离Dify的节点输出用于定位跨系统协议，凭据节点不出报告。
        diagnostic_sql = (
            "SELECT node_id, status, inputs, outputs, error FROM workflow_node_executions "
            "WHERE node_id IN ('photo', 'photo_context', 'knowledge') ORDER BY created_at DESC LIMIT 12"
        )
        data = stack._compose(
            env,
            "exec",
            "-T",
            "db_postgres",
            "psql",
            "-U",
            env["DB_USERNAME"],
            "-d",
            env["DB_DATABASE"],
            "-c",
            diagnostic_sql,
            timeout=30,
        )
        (root / "knowledge-node-output.private.txt").write_text(data.stdout + data.stderr)
        logs = stack._compose(env, "logs", "--no-color", "worker", timeout=30)
        (root / "knowledge-worker.log").write_text(logs.stdout + logs.stderr)
        (root / "knowledge-model-requests.json").write_text(json.dumps(Model.requests, ensure_ascii=False))
        if gateway:
            stack._stop(gateway)
        kb.shutdown()
        cpms.shutdown()


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
