"""真 Gateway + 真 Java 节点；CPMS、公司身份、Dify草稿和模型为合成。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
from operations.test_operator_cpms import HEADERS, environment, request, tenant_owner  # noqa: E402
from pytest import MonkeyPatch  # noqa: E402

from aiops_diagnostics.operator import operator_cpms, operator_tools  # noqa: E402

ORDER_TOOLS = ("cpms_order_detail", "cpms_charging_samples", "cpms_order_tariff", "cpms_order_billing")


class Model(BaseHTTPRequestHandler):
    calls = []
    fail = False
    provider_error = ""
    truncate = False
    invalid = ""
    conversation_kind = "general"
    plan_arguments = {}

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).calls.append(body)
        if type(self).provider_error:
            label = type(self).provider_error
            message = "用户额度不足 PRIVATE-UPSTREAM" if label == "quota-message" else "PRIVATE-UPSTREAM"
            payload = json.dumps(
                {
                    "error": {
                        "message": message,
                        "type": "error",
                        "code": "insufficient_user_quota" if label == "quota-code" else "permission_denied",
                    }
                }
            ).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        tools = [m for m in body.get("messages", []) if m.get("role") == "tool"]
        conversation = not any(t["function"]["name"] == "cpms_order_detail" for t in body.get("tools", []))
        if type(self).fail and tools:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if conversation and type(self).conversation_kind == "plan":
            if not tools:
                delta = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_plan",
                            "type": "function",
                            "function": {
                                "name": "cpms_station_plan",
                                "arguments": json.dumps(type(self).plan_arguments),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta = {
                    "role": "assistant",
                    "content": json.dumps(
                        {"kind": "station_plan", "text": "", "name": "", "address_terms": []}
                    ),
                }
                finish = "stop"
        elif conversation and type(self).conversation_kind == "sites" and not tools:
            delta = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_site",
                        "type": "function",
                        "function": {
                            "name": "cpms_site_search",
                            "arguments": json.dumps({"address_terms": ["广州", "天河"]}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        elif conversation:
            answer = (
                {"kind": "site_search", "text": "", "name": "", "address_terms": ["广州", "天河"]}
                if type(self).conversation_kind == "sites"
                else {"kind": "answer", "text": "状态3表示异常已处理。", "name": "", "address_terms": []}
            )
            delta = {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}
            finish = "stop"
        elif len(tools) < len(ORDER_TOOLS):
            tool = ORDER_TOOLS[len(tools)]
            delta = {
                "role": "assistant",
                "content": "正在读取订单证据。",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(tools)}",
                        "type": "function",
                        "function": {"name": tool, "arguments": "{}"},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            answer = {
                "direct_answer": "订单以通信中断结束，现场网络需核查。",
                "summary": "订单异常结束。",
                "probable_cause": "通信中断线索，需现场核查。",
                "next_steps": ["检查网络。"],
                "limitations": ["有限样本不是完整时间线。"],
                "evidence_tools": list(ORDER_TOOLS),
                "reply": "本轮记录如下。\n{{fact:order_facts}}\n请结合现场情况核对网络。",
            }
            delta = {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}
            if body.get("response_format") != {"type": "json_object"}:
                delta["content"] = "订单异常结束，通信中断线索仍需现场核查。"
            if type(self).invalid == "free-text":
                delta["content"] = "PRIVATE-ANSWER 订单异常结束。"
            elif type(self).invalid == "missing-field":
                del answer["limitations"]
                delta["content"] = json.dumps(answer)
            elif type(self).invalid == "forged-evidence":
                answer["evidence_tools"].append("unread_tool")
                delta["content"] = json.dumps(answer)
            elif type(self).invalid == "mutation-claim":
                answer["summary"] = "已退款。"
                delta["content"] = json.dumps(answer)
            elif type(self).invalid == "unanswered":
                # 问的是电费，交回的却是与提问无关的通用概述 —— 2026-10-29 反馈的形状。
                answer["direct_answer"] = ""
                delta["content"] = json.dumps(answer)
            elif type(self).invalid == "empty":
                delta["content"] = ""
            elif type(self).invalid == "oversized":
                delta["content"] = "x" * 24001
            finish = "stop"
            if type(self).truncate:
                finish = None
        chunk = {
            "id": "synthetic",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "synthetic-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if "tool_calls" not in delta:
            content = delta["content"]
            split = len(content) // 2
            for part in (content[:split], content[split:]):
                chunk["choices"] = [{"index": 0, "delta": {"content": part}, "finish_reason": None}]
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": finish}]
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--tenant-owner", action="store_true", help="使用租户主账号的合成身份")
    args = parser.parse_args()
    out = Path(args.out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    deps = Path(os.environ["M3NODE_DEPS"]).absolute()
    image = os.environ["M3NODE_JDK_IMAGE"]
    if "@sha256:" not in image or not (deps / "lib").is_dir():
        raise ValueError("locked JDK and dependency directory required")
    classes = out / "classes"
    classes.mkdir(exist_ok=True)
    shutil.copyfile(
        ROOT / "src/aiops_diagnostics/operator/operator_tools.json", classes / "operator_tools.json"
    )
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
    compile_cmd = (
        'find /repo/services/execution-node/src/main/java -name "*.java" > /tmp/sources; '
        'javac -nowarn -proc:none -cp "/deps/lib/*" -d /out @/tmp/sources '
        "/repo/tools/experiments/m2_close/m3node/NodeLinkAcceptance.java "
        "/repo/tools/experiments/m2_close/m3node/CpmsToolAcceptance.java"
    )
    result = subprocess.run(
        [*docker, "--network=none", image, "bash", "-c", compile_cmd],
        capture_output=True,
        text=True,
        timeout=180,
    )
    (out / "compile.log").write_text(result.stdout + result.stderr)
    result.check_returncode()
    result = subprocess.run(
        [
            *docker,
            "--network=none",
            image,
            "java",
            "-cp",
            "/out:/deps/lib/*",
            "com.trendpower.aiops.node.CpmsToolAcceptance",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    (out / "tools.log").write_text(result.stdout + result.stderr)
    result.check_returncode()
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    model_url = f"http://127.0.0.1:{model.server_port}/v1"
    cases = []
    try:
        with (
            tempfile.TemporaryDirectory(prefix="operator-cpms-") as root,
            MonkeyPatch.context() as patch,
            environment(Path(root), patch) as f,
        ):
            if args.tenant_owner:
                f["resolver"].current = tenant_owner()
            f["candidate"]["enabled"] = list(ORDER_TOOLS)
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
            current = f["publish"](f["route"].route_revision)

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
                        "--scenario="
                        + (
                            "round2-500"
                            if Model.fail or Model.provider_error
                            else "truncated"
                            if Model.truncate
                            else "claim-refused"
                            if Model.invalid
                            else "normal"
                        ),
                        "--node-token=" + "node-token-alpha-0123456789",
                        "--managed-publication",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=110,
                )
                (out / (run + ".log")).write_text(result.stdout + result.stderr)
                result.check_returncode()

            patch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
            response = request(f["client"])
            assert response.status_code == 202, response.text
            qa = response.json()["qa_id"]
            final = None
            for _ in range(100):
                import time

                time.sleep(0.5)
                final = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
                if final.status_code != 200 or final.json()["status"] not in {"queued", "running"}:
                    break
            cases.append({"case": "java-loop", "status": final.status_code, "body": final.json()})
            assert final.status_code == 200 and final.json()["status"] == "completed", final.text
            assert len(f["state"]["calls"]) == 9  # 四次取证、两次再验订单、签发/完成/回读各一次。
            assert len(Model.calls) == 5
            assert "订单金额验证" in final.text and "规则1 服务费折扣优惠" in final.text
            assert all(call.get("response_format") == {"type": "json_object"} for call in Model.calls)
            assert all(
                "evidence_tools" in call["messages"][0]["content"] for call in Model.calls
            )  # 系统已有五字段提示词；缺的是供应商输出模式与末轮边界。
            assert all(
                "direct_answer" in call["messages"][0]["content"] for call in Model.calls
            )  # 报告必须有「回答本次提问」的位置；否则任何问题都只能被概述一遍。
            assert "正在读取订单证据" not in final.text
            messages = json.dumps(Model.calls)
            assert "operator-private" not in messages and "PRIVATE-PHONE" not in messages
            run = f["ctx"].agent_run_store.by_qa(qa)
            assert run.exec_config_confirmed and run.actual_node_id == run.expected_node_id
            for label in (
                "quota-code",
                "quota-message",
                "permission-denied",
                "supplier-error",
                "truncated",
                "free-text",
                "missing-field",
                "forged-evidence",
                "mutation-claim",
                "unanswered",
                "empty",
                "oversized",
            ):
                Model.provider_error = (
                    label if label in {"quota-code", "quota-message", "permission-denied"} else ""
                )
                before_model = len(Model.calls)
                before_cpms = len(f["state"]["calls"])
                Model.fail = label == "supplier-error"
                Model.truncate = label == "truncated"
                Model.invalid = label if label not in {"supplier-error", "truncated"} else ""
                response = request(f["client"])
                assert response.status_code == 202, response.text
                failed_qa = response.json()["qa_id"]
                for _ in range(120):
                    time.sleep(0.5)
                    failed = f["client"].get("/v1/assistant/questions/" + failed_qa, headers=HEADERS)
                    if failed.status_code != 200 or failed.json()["status"] not in {"queued", "running"}:
                        break
                assert failed.status_code == 200 and failed.json()["status"] == "failed", failed.text
                assert not failed.json()["result"]
                assert f["ctx"].agent_run_store.by_qa(failed_qa).state == "failed"
                if Model.provider_error:
                    expected = (
                        "OPERATOR_DEPENDENCY_UNAVAILABLE"
                        if label == "permission-denied"
                        else "OPERATOR_MODEL_QUOTA_EXHAUSTED"
                    )
                    assert failed.json()["error"]["code"] == expected, failed.text
                    assert len(Model.calls) == before_model + 1
                    assert len(f["state"]["calls"]) == before_cpms + 1
                    assert "PRIVATE-UPSTREAM" not in failed.text
                    if label != "permission-denied":
                        assert "额度不足" in failed.json()["error"]["message"]
                        assert failed.json()["error"]["retryable"] is False
                cases.append({"case": label, "status": "failed-without-answer"})
            Model.provider_error = ""
            Model.fail = Model.truncate = False
            Model.invalid = ""
            from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

            f["settings"].environment = "env-a"
            f["settings"].dify_trial_api_key = "trial-only"
            with DifyWorkspaces(f["db"])._connection(write=True) as connection:
                connection.execute(
                    "INSERT INTO dify_workspaces "
                    "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
                    "VALUES (?,?,?,'ready',?,?)",
                    ("TENANT-A", "env-a", "workspace", "2026-10-26", "2026-10-26"),
                )
            patch.setattr(
                f["ctx"].operator_cpms,
                "console_identity",
                lambda session: {
                    "token": "operator-private",
                    "user_id": "OPERATOR-A",
                    "tenant_id": "TENANT-A",
                },
            )
            f["candidate"]["prompt"] = "TRIAL_UNPUBLISHED"
            headers = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "synthetic-session"}
            response = f["client"].post(
                "/v1/dify/trial",
                headers=headers,
                json={
                    "app_id": f["target"].app_id,
                    "order_no": "ORDER-1001",
                    "operator_preview": {
                        "pre_prompt": "NATIVE_UNSAVED",
                        "tools": [
                            {
                                "provider_id": "provider-id",
                                "provider_type": "api",
                                "tool_name": name,
                                "enabled": True,
                            }
                            for name in ORDER_TOOLS
                        ],
                    },
                },
            )
            assert response.status_code == 202, response.text
            trial = response.json()["trial_id"]
            for _ in range(120):
                time.sleep(0.5)
                result = f["client"].get("/v1/dify/trial/" + trial, headers=headers)
                if result.status_code != 200 or result.json()["status"] not in {"queued", "running"}:
                    break
            assert result.status_code == 200 and result.json()["status"] == "completed", result.text
            assert all(call.get("response_format") == {"type": "json_object"} for call in Model.calls)
            trial_run = f["ctx"].agent_run_store.by_qa(trial)
            assert "NATIVE_UNSAVED" in trial_run.execution_manifest
            assert trial_run.execution_manifest != current.manifest_text
            from aiops_diagnostics.execution import chat_execution

            assert chat_execution.binding_for_key(f["settings"], current.key) == current
            cases.append({"case": "java-native-draft-trial", "status": "completed-without-publishing"})
            response = f["client"].post(
                "/v1/dify/trial",
                headers=headers,
                json={"app_id": f["target"].app_id, "order_no": "ORDER-1001"},
            )
            assert response.status_code == 202, response.text
            saved_trial = response.json()["trial_id"]
            for _ in range(120):
                time.sleep(0.5)
                result = f["client"].get("/v1/dify/trial/" + saved_trial, headers=headers)
                if result.status_code != 200 or result.json()["status"] not in {"queued", "running"}:
                    break
            assert result.status_code == 200 and result.json()["status"] == "completed", result.text
            assert "TRIAL_UNPUBLISHED" in f["ctx"].agent_run_store.by_qa(saved_trial).execution_manifest
            assert chat_execution.binding_for_key(f["settings"], current.key) == current
            assert all(call.get("response_format") == {"type": "json_object"} for call in Model.calls)
            cases.append({"case": "java-saved-draft-trial", "status": "completed-without-publishing"})
            for label, question in (
                ("general", "订单状态3是什么意思"),
                ("sites", "帮我找一下广州天河附近的充电站"),
            ):
                Model.conversation_kind = label
                before = len(f["state"]["calls"])
                response = f["client"].post(
                    "/v1/assistant/questions", headers=HEADERS, json={"question": question}
                )
                assert response.status_code == 202, response.text
                for _ in range(120):
                    time.sleep(0.5)
                    result = f["client"].get(
                        "/v1/assistant/questions/" + response.json()["qa_id"], headers=HEADERS
                    )
                    if result.status_code != 200 or result.json()["status"] not in {"queued", "running"}:
                        break
                assert result.status_code == 200 and result.json()["status"] == "completed", result.text
                if label == "general":
                    assert len(f["state"]["calls"]) == before and "异常已处理" in result.text
                else:
                    from urllib.parse import parse_qs, urlsplit

                    calls = f["state"]["calls"][before:]
                    assert [parse_qs(urlsplit(path).query)["address"] for path, _ in calls] == [
                        ["广州"],
                        ["天河"],
                    ]
                    assert "天河测试站" in result.text, result.text
                    run = f["ctx"].agent_run_store.by_qa(response.json()["qa_id"])
                    assert run.snapshot["evidence"]["cpms_site_search"]["inputs"]["address_terms"] == [
                        "广州",
                        "天河",
                    ]
                    assert "PRIVATE-PHONE" not in result.text
                assert [t["function"]["name"] for t in Model.calls[-1]["tools"]] == [
                    "cpms_site_search",
                    "cpms_station_plan",
                ]
                assert Model.calls[-1]["response_format"] == {"type": "json_object"}
                cases.append({"case": "java-no-order-" + label, "status": "completed"})
            from operations.test_station_tariff import responses

            data = responses()
            original = operator_cpms.cpms_get

            def pricing_upstream(url, bearer, tenant, **kwargs):
                from urllib.parse import urlsplit

                path = urlsplit(url).path
                return data[path] if path in data else original(url, bearer, tenant, **kwargs)

            patch.setattr(operator_cpms, "cpms_get", pricing_upstream)
            Model.conversation_kind = "plan"
            Model.plan_arguments = {
                "name": "天河测试站",
                "date": (datetime.now(UTC) + timedelta(days=2)).date().isoformat(),
                "start": "06:00",
                "end": "10:00",
                "energy": "3",
                "power": "2",
                "timezone": "Asia/Shanghai",
                "basis": "普通按量、不用优惠、不预约、不买保险、充完离场",
            }
            question = (
                "天河测试站 "
                + Model.plan_arguments["date"]
                + " 06:00至10:00 3kWh 2kW Asia/Shanghai "
                + Model.plan_arguments["basis"]
            )
            response = f["client"].post(
                "/v1/assistant/questions", headers=HEADERS, json={"question": question}
            )
            assert response.status_code == 202, response.text
            # Java JVM启动时间长于HTTP单测轮询窗口。
            for _ in range(120):
                time.sleep(0.5)
                final = f["client"].get(
                    "/v1/assistant/questions/" + response.json()["qa_id"], headers=HEADERS
                )
                if final.json()["status"] not in {"queued", "running"}:
                    break
            assert final.json()["status"] == "completed", final.text
            assert "含税合计 5.50" in final.text and "06:00–07:30" in final.text, final.text
            run = f["ctx"].agent_run_store.by_qa(response.json()["qa_id"])
            assert run.snapshot["evidence"]["cpms_station_plan"]["data"]["rules"]["template"]["vat"] == 0
            cases.append({"case": "java-station-plan", "status": "completed-with-rules"})
            f["candidate"]["enabled"] = []
            f["publish"](current.route_revision)
            denied = request(f["client"])
            assert denied.status_code == 403
            cases.append({"case": "published-all-off", "status": denied.status_code})
    finally:
        model.shutdown()
        model.server_close()
        (out / "report.json").write_text(
            json.dumps(
                {
                    "cases": cases,
                    "model_calls": len(Model.calls),
                    "identity": "tenant-owner" if args.tenant_owner else "site-employee",
                    "scope": "real Java and Gateway; synthetic upstreams",
                    "finished_at": datetime.now(UTC).isoformat(),
                    "base_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                    "jdk_image": image,
                    "dependencies_sha256": sha256(
                        "\n".join(
                            path.name + ":" + sha256(path.read_bytes()).hexdigest()
                            for path in sorted((deps / "lib").glob("*.jar"))
                        ).encode()
                    ).hexdigest(),
                    "sources": {
                        str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
                        for path in sorted((ROOT / "services/execution-node/src/main/java").rglob("*.java"))
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    print("PASS: real Java tool loop, four CPMS tools, five model turns, all-off refuses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
