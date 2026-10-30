"""管家页面验收装配：真实Dify、Gateway、Java；合成公司身份、CPMS和模型。"""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tools = [m for m in body["messages"] if m["role"] == "tool"]
        if len(tools) < 2:
            name = ("cpms_order_detail", "cpms_charging_samples")[len(tools)]
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + name,
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ]
            }
            finish = "tool_calls"
        else:
            marker = (
                "管家提示词页面核对"
                if "管家提示词页面核对" in body["messages"][0]["content"]
                else "已核对本轮记录"
            )
            answer = {
                "direct_answer": "本轮记录如下。",
                "summary": "本轮记录。",
                "probable_cause": "原因待核查。",
                "next_steps": ["核对现场记录。"],
                "limitations": ["有限样本不代表完整时间线。"],
                "evidence_tools": ["cpms_order_detail", "cpms_charging_samples"],
                "reply": marker + "。\n{{fact:order_facts}}\n建议结合现场记录进一步核查。",
            }
            delta, finish = {"content": json.dumps(answer, ensure_ascii=False)}, "stop"
        chunk = {
            "id": "operator-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "synthetic",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())


def verify_operator(root, base, env, setup, check):
    import verify_dify_chat_entry as stack
    from operations.test_operator_cpms import environment

    from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
    from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
    from aiops_diagnostics.dify.dify_console_publish import BridgeSettings, make_server
    from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
    from aiops_diagnostics.execution import node_publication
    from aiops_diagnostics.operator import operator_cpms, operator_tools
    from aiops_diagnostics.operator.ops_provider import ensure_node_provider

    work = root / "operator"
    work.mkdir()
    workspace = stack._console_workspace(base, setup["console_headers"])
    console = DifyConsoleAdmin(ConsoleApiSettings(base, stack.ADMIN_API_KEY))
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    model_url = f"http://127.0.0.1:{model.server_port}/v1"
    try:
        with MonkeyPatch.context() as patch, environment(work, patch) as f:
            patch.setattr(node_publication, "DifyDslClient", DifyDslClient)
            ensure_node_provider(console, workspace, f["gateway_base"])
            providers = console._call(workspace, "/console/api/workspaces/current/tool-providers?type=api")
            provider = next(p["id"] for p in providers if p["name"] == "cpms-node")
            doc = yaml.safe_load(
                (ROOT / "src/aiops_diagnostics/dify/dify_workspace_template.dsl.yml").read_text()
            )
            doc["app"].update(mode="agent-chat", name="管家提示词验收")
            mc = doc["model_config"]
            mc["pre_prompt"] = "用清楚直接的语气回答，依据本轮证据，不编造事实。"
            mc["model"].update(provider=stack.PROVIDER_NAME, name="synthetic", mode="chat")
            mc["agent_mode"] = {
                "enabled": True,
                "strategy": "function_call",
                "max_iteration": 10,
                "tools": [
                    {
                        "provider_id": provider,
                        "provider_name": "cpms-node",
                        "provider_type": "api",
                        "tool_name": name,
                        "enabled": True,
                        "tool_parameters": {},
                    }
                    for name in ("cpms_order_detail", "cpms_charging_samples")
                ],
            }
            app_id = console.import_app_dsl(
                workspace, yaml_content=yaml.safe_dump(doc, allow_unicode=True), name="管家提示词验收"
            )
            target = replace(f["target"], app_id=app_id)
            Path(f["settings"].chat_node_targets_file).write_text(
                json.dumps({"schema": "aiops-node-targets/1", "targets": [target.__dict__]})
            )
            raw = json.loads(f["manifest"].read_text())
            raw["model"].update(
                provider_key="openai-compatible",
                adapter_key="openai-compatible",
                model_key="synthetic-model",
                endpoint=model_url,
            )
            raw["tools"] = [
                {
                    "name": name,
                    "description": operator_tools.tool(name).description,
                    "input_schema": operator_tools.tool(name).input_schema(),
                }
                for name in ("cpms_order_detail", "cpms_charging_samples")
            ]
            f["manifest"].write_text(json.dumps(raw))
            settings, ctx = f["settings"], f["ctx"]
            settings.environment = "env-a"
            settings.dify_console_base_url = base
            settings.dify_console_api_key = stack.ADMIN_API_KEY
            settings.dify_publish_api_key = stack.PUBLISH_KEY
            settings.dify_trial_api_key = stack.TRIAL_KEY
            credentials = work / "model-credentials.json"
            credentials.write_text(
                json.dumps(
                    {
                        "api_key": "synthetic",
                        "function_calling_type": "tool_call",
                        "max_tokens_to_sample": "4096",
                    }
                )
            )
            credentials.chmod(0o600)
            profiles = work / "profiles.json"
            profiles.write_text(
                json.dumps(
                    {
                        "schema": "operator-profiles/1",
                        "profiles": [
                            {
                                "id": "local",
                                "name": "隔离节点",
                                "target": {
                                    k: v
                                    for k, v in target.__dict__.items()
                                    if k not in {"app_id", "tenant_id"}
                                },
                                "model": {
                                    "plugin": stack.PLUGIN_ID,
                                    "provider": stack.PROVIDER_NAME,
                                    "name": "synthetic",
                                    "credential_file": str(credentials),
                                    "completion_params": {"max_tokens": 2000},
                                },
                            }
                        ],
                    }
                )
            )
            profiles.chmod(0o600)
            settings.operator_profiles_file = str(profiles)
            from aiops_diagnostics.dify import self_service_identity
            from aiops_diagnostics.dify.self_service_store import SelfServiceStore

            SelfServiceStore(f["db"]).register(
                app_id=app_id,
                tenant_id="TENANT-A",
                workspace_id=workspace,
                agent_name="operator-node",
                profile_id="local",
                actor="isolated-acceptance",
                environment="env-a",
            )
            ctx.self_service.console = console
            patch.setattr(
                self_service_identity,
                "company_get",
                lambda context, token, path: {
                    "sysUser": {"id": "OPERATOR-A", "tenantId": "TENANT-A", "type": "5"},
                    "permissions": ["aiops:agent:edit", "aiops:agent:publish"],
                },
            )
            ctx.runtime.dify_app_registry.put(DifyAppBinding("TENANT-A", "operator", app_id, "operator-node"))
            with DifyWorkspaces(f["db"])._connection(write=True) as db:
                db.execute(
                    "INSERT INTO dify_workspaces "
                    "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
                    "VALUES (?,?,?,'ready',?,?)",
                    ("TENANT-A", "env-a", workspace, "2026-10-30", "2026-10-30"),
                )
            patch.setattr(
                ctx.operator_cpms,
                "console_identity",
                lambda session: {
                    "token": "operator-private",
                    "user_id": "OPERATOR-A",
                    "tenant_id": "TENANT-A",
                },
            )

            def dispatch(_, config, run):
                classes = os.environ["PROMPT_NODE_CLASSES"]
                cmd = [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "container:" + env["DIFY_TEST_NAMESPACE"],
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    classes + ":/out:ro",
                    "-v",
                    os.environ["M3NODE_DEPS"] + ":/deps:ro",
                    "eclipse-temurin@sha256:5d90b26c0784ccf55af55f97cbcf7f3969c66a06299c93bdc72649219942a48f",
                    "java",
                    "-cp",
                    "/out:/deps/lib/*",
                    "m3node.NodeLinkAcceptance",
                    "--run-id=" + run,
                    "--gateway=" + f["gateway_base"],
                    "--model=" + model_url,
                    "--scenario=normal",
                    "--node-token=node-token-alpha-0123456789",
                    "--managed-publication",
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=110)
                (work / (run + ".log")).write_text(result.stdout + result.stderr)
                result.check_returncode()

            patch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
            code, published = stack.http_json(
                f["gateway_base"] + "/v1/dify/publish",
                "POST",
                {"app_id": app_id},
                {"Authorization": "Bearer " + stack.PUBLISH_KEY},
            )
            (work / "publication.json").write_text(json.dumps({"http": code, "body": published}))
            check("管家隔离应用已发布", code == 200)
            origin = (
                "http://"
                + subprocess.check_output(
                    ["docker", "port", env["DIFY_TEST_NAMESPACE"], "3000"], text=True
                ).strip()
            )
            bridge = make_server(
                BridgeSettings(
                    listen_host="0.0.0.0",
                    listen_port=5004,
                    dify_base_url=base,
                    gateway_base_url=f["gateway_base"],
                    publish_key=stack.PUBLISH_KEY,
                    trial_key=stack.TRIAL_KEY,
                    console_origin=origin,
                    workspace_id=workspace,
                    login_proxy_url="http://127.0.0.1:1",
                )
            )
            threading.Thread(target=bridge.serve_forever, daemon=True).start()
            try:
                (root / "operator-browser-ready.json").write_text(
                    json.dumps(
                        {
                            "headers": setup["console_headers"],
                            "app_id": app_id,
                            "gateway": f["gateway_base"],
                            "version_no": published["version_no"],
                        }
                    )
                )
                until = time.monotonic() + 420
                while not (root / "operator-browser-result.json").exists() and time.monotonic() < until:
                    time.sleep(0.2)
                result = root / "operator-browser-result.json"
                check(
                    "管家编排页提示词闭环",
                    result.exists() and json.loads(result.read_text()).get("passed") is True,
                )
            finally:
                bridge.shutdown()
                bridge.server_close()
    finally:
        model.shutdown()
        model.server_close()
