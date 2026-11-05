"""在既有隔离栈核验原HTTP入口、真Dify Agent和受控多工具；上游数据/模型合成。"""

import json
import sqlite3
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import yaml

from aiops_diagnostics.consumer.consumer_agent_flow import AGENT_PLUGIN
from aiops_diagnostics.consumer.consumer_flow_contract import publication_settings
from aiops_diagnostics.consumer.consumer_knowledge_grants import KnowledgeGrants
from aiops_diagnostics.consumer.consumer_knowledge_selection import PROVIDER, catalogue_token
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin


def verify(root, base, env, setup, model, check):
    import verify_consumer_chatflow as vf
    import verify_dify_chat_entry as stack
    from verify_consumer_knowledge_picker import install_signed_plugin

    sys.path.insert(0, str(vf.ROOT / "tools/content"))
    from upgrade_consumer_flow import agent_conversation, live_documents, select_knowledge

    headers = setup["console_headers"]
    workspace = stack._console_workspace(base, headers)
    admin = DifyConsoleAdmin(ConsoleApiSettings(base, stack.ADMIN_API_KEY, install_timeout=180))
    code, installed = stack.http_json(
        base + "/console/api/workspaces/current/plugin/install/marketplace",
        "POST",
        {"plugin_unique_identifiers": [AGENT_PLUGIN]},
        headers,
    )
    check("official Agent installation accepted", code == 200)
    if installed.get("task_id"):
        admin._await_install_task(workspace, installed["task_id"], expected=[AGENT_PLUGIN])
    check("official Agent installed", AGENT_PLUGIN in admin.installed_plugin_ids(workspace))
    identifier = install_signed_plugin(root, base, headers, workspace, check)
    cpms = stack._serve(stack.CpmsHandler)
    kb = ThreadingHTTPServer(("127.0.0.1", 0), vf.Knowledge)
    threading.Thread(target=kb.serve_forever, daemon=True).start()
    gateway = None
    stack.CpmsHandler.calls = []
    vf.Knowledge.requests = []
    try:
        doc = yaml.safe_load((vf.ROOT / "deploy/dify-chat/consumer-service.dsl.yml").read_text())
        for node in doc["workflow"]["graph"]["nodes"]:
            if node["data"]["type"] == "llm":
                node["data"]["model"].update(provider=stack.PROVIDER_NAME, name="synthetic", mode="chat")
        doc = agent_conversation(
            live_documents(select_knowledge(doc, ["kb-own"], identifier), replace_system=True), identifier
        )
        publication_settings(
            doc["workflow"],
            {"live_order_status": True, "live_billing_record": True},
            base,
            owned_libraries=[{"knowledge_base_id": "kb-own", "audience": "customer"}],
        )
        stack._dsl_for = lambda _: yaml.safe_dump(doc, allow_unicode=True).encode()
        app = stack._import_second_app(base, headers, base)
        check("Agent Chatflow published", bool(app.get("workflow_id") and app.get("service_key")))
        content = root / "agent-content.json"
        content.write_text(
            json.dumps(
                {
                    "schema": "aiops-consumer-live/1",
                    "state": "published",
                    "tenant_id": stack.TENANT,
                    "environment": "env-local",
                    "entries": {},
                }
            )
        )
        content.chmod(0o600)
        gateway, gateway_base, diag = stack._start_gateway(
            root=root / "agent-test",
            model_url=f"http://127.0.0.1:{model.server_port}/v1",
            cpms_base=f"http://127.0.0.1:{cpms.server_port}",
            dify_base=base,
            workflow_id=app["workflow_id"],
            key=app["service_key"],
            port=stack._free_port(),
            service_app_id=app["app_id"],
            service_content=str(content),
            knowledge_base=f"http://127.0.0.1:{kb.server_port}",
            service_live_order=True,
            service_billing=True,
            service_callback_version=6,
        )
        check("Agent Gateway ready", not diag.get("error"))
        database = root / "agent-test/gateway/data/gateway.db"
        KnowledgeGrants(database).grant(stack.TENANT, "env-local", "kb-own", "acceptance")
        check(
            "workspace mapping ready",
            stack._seed_workspace(database, stack.TENANT, workspace, "dify-chat-acceptance"),
        )
        code, _ = stack.http_json(
            base + "/console/api/workspaces/current/tool-provider/builtin/" + PROVIDER + "/add",
            "POST",
            {
                "credentials": {
                    "gateway_url": gateway_base,
                    "catalogue_token": catalogue_token(
                        "synthetic-catalogue-secret", "dify-chat-acceptance", workspace
                    ),
                },
                "name": "隔离验收",
                "type": "api-key",
                "visibility": "all_team_members",
            },
            headers,
        )
        check("Agent tool credential configured", code == 200)
        app_headers = {"Authorization": "Bearer " + stack.TOKEN, "X-Business-Entry": "consumer"}
        conversation, answers = "", []
        for question in ("查最新订单状态、账单和操作指引", "第2步仍然没有反应"):
            code, post = stack.http_json(
                gateway_base + "/v1/assistant/questions",
                "POST",
                {"question": question, **({"conversation_id": conversation} if conversation else {})},
                app_headers,
            )
            check("Agent question admitted", code == 202)
            conversation = post["conversation_id"]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                code, answer = stack.http_json(
                    gateway_base + "/v1/assistant/questions/" + post["qa_id"], headers=app_headers
                )
                if code != 200 or answer.get("status") not in ("queued", "running"):
                    break
                time.sleep(0.2)
            answers.append(answer)
            check("Agent question completed", code == 200 and answer.get("status") == "completed")
            if len(answers) == 1:
                with sqlite3.connect(database) as db:
                    db.execute(
                        "UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE qa_id=?",
                        (post["qa_id"],),
                    )
        check("two actual CPMS reads", len(stack.CpmsHandler.calls) == 2)
        check("one actual knowledge search", len(vf.Knowledge.requests) == 1)
        check(
            "history reaches model after grant expiry",
            any(
                "第2步的设备提示" in str(b.get("messages")) and "第2步仍然" in str(b.get("messages"))
                for b in model.RequestHandlerClass.requests
            ),
        )
        check("model never sees grant", "cdg_" not in json.dumps(model.RequestHandlerClass.requests))
        code, _ = stack.http_json(
            gateway_base + "/v1/assistant/questions",
            "POST",
            {"question": "继续", "conversation_id": conversation},
            {**app_headers, "Authorization": "Bearer " + stack.OTHER_TOKEN},
        )
        check("foreign conversation refused", code == 404)
        (root / "agent-evidence.json").write_text(
            json.dumps(
                {
                    "answers": answers,
                    "model_requests": model.RequestHandlerClass.requests,
                    "cpms_calls": stack.CpmsHandler.calls,
                    "knowledge_calls": vf.Knowledge.requests,
                },
                ensure_ascii=False,
            )
        )
    finally:
        if gateway is not None:
            stack._stop(gateway)
        cpms.shutdown()
        kb.shutdown()
