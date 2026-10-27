"""真Dify插件/选库/发布验证，由verify_consumer_chatflow的隔离栈调用。"""

import json
import time
import urllib.request
from urllib.parse import urlencode

from aiops_diagnostics.consumer_knowledge_selection import PROVIDER, catalogue_token
from aiops_diagnostics.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin


def verify_picker(root, base, env, setup, app, gateway, database, content, knowledge, check):
    import verify_dify_chat_entry as stack

    headers = setup["console_headers"]
    workspace = stack._console_workspace(base, headers)
    admin = DifyConsoleAdmin(ConsoleApiSettings(base, stack.ADMIN_API_KEY, install_timeout=180))
    boundary = "picker-upload-boundary"
    blob = (root / "knowledge.signed.difypkg").read_bytes()
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="pkg"; '
            'filename="knowledge.difypkg"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        + blob
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = urllib.request.Request(
        base + "/console/api/workspaces/current/plugin/upload/pkg",
        data=body,
        headers={**headers, "Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        uploaded = json.load(response)
    (root / "picker-upload.json").write_text(json.dumps(uploaded))
    identifier = uploaded["unique_identifier"]
    check("signed knowledge plugin verified", uploaded["manifest"]["verified"] is True)
    code, installed = stack.http_json(
        base + "/console/api/workspaces/current/plugin/install/pkg",
        "POST",
        {"plugin_unique_identifiers": [identifier]},
        headers,
    )
    check("plugin installation accepted", code == 200)
    admin._await_install_task(workspace, installed["task_id"], expected=[identifier])
    check("knowledge plugin installed", identifier in admin.installed_plugin_ids(workspace))
    credential = {
        "gateway_url": gateway,
        "catalogue_token": catalogue_token("synthetic-catalogue-secret", "dify-chat-acceptance", workspace),
    }
    code, body = stack.http_json(
        base + "/console/api/workspaces/current/tool-provider/builtin/" + PROVIDER + "/add",
        "POST",
        {
            "credentials": credential,
            "name": "本工作区目录",
            "type": "api-key",
            "visibility": "all_team_members",
        },
        headers,
    )
    (root / "picker-credential-result.json").write_text(json.dumps(body))
    check("catalogue credential validated", code == 200)
    code, options = stack.http_json(
        base
        + "/console/api/workspaces/current/plugin/parameters/dynamic-options?"
        + urlencode(
            {
                "plugin_id": "trendpower/consumer_knowledge",
                "provider": PROVIDER,
                "action": "consumer_knowledge",
                "parameter": "knowledge_base_ids",
                "provider_type": "tool",
            }
        ),
        headers=headers,
    )
    (root / "picker-options.json").write_text(json.dumps(options))
    check(
        "dynamic choices show two owned libraries",
        code == 200 and {o["value"] for o in options["options"]} == {"kb-own", "kb-b"},
    )
    draft_url = base + "/console/api/apps/" + app["app_id"] + "/workflows/draft"

    def select(libraries):
        code, draft = stack.http_json(draft_url, headers=headers)
        assert code == 200
        from upgrade_consumer_flow import select_knowledge

        draft = select_knowledge({"workflow": draft}, libraries, identifier)["workflow"]
        code, body = stack.http_json(
            draft_url,
            "POST",
            {k: draft[k] for k in ("graph", "features", "hash", "conversation_variables")},
            headers,
        )
        check("node selection saved", code == 200)

    pub_headers = {"Authorization": "Bearer " + stack.PUBLISH_KEY}

    def action(name, **fields):
        return stack.http_json(
            gateway + "/v1/dify/" + name, "POST", {"app_id": app["app_id"], **fields}, pub_headers
        )

    app_headers = {"Authorization": "Bearer " + stack.TOKEN, "X-Business-Entry": "consumer"}

    def ask():
        count = len(knowledge.requests)
        code, qa = stack.http_json(
            gateway + "/v1/assistant/questions", "POST", {"question": "怎么开始充电？"}, app_headers
        )
        assert code == 202, qa
        until = time.monotonic() + 90
        while time.monotonic() < until:
            code, answer = stack.http_json(
                gateway + "/v1/assistant/questions/" + qa["qa_id"], headers=app_headers
            )
            if answer.get("status") not in {"running", "queued"}:
                break
            time.sleep(0.2)
        assert code == 200 and answer.get("status") == "completed", answer
        return answer, knowledge.requests[count:]

    document = json.loads(content.read_text())
    document["entries"]["KB-C-008"] = {
        "state": "approved",
        "title": "B库指引",
        "answers": {"zh": "审核后的B库指导", "en": "Reviewed B guidance."},
    }
    content.write_text(json.dumps(document))
    select(["kb-own"])
    code, published = action("publish")
    check("picker version published", code == 200)
    first = published["version_no"]
    _, calls = ask()
    check("App searches selected A", len(calls) == 1 and "/kb-own/" in calls[0][0])
    # 只改变一次性栈的测试账号角色；真实会话仍由Dify登录签发。
    import uuid

    def role(value):
        assert value in {"normal", "dataset_operator", "editor", "owner"}
        sql = f"UPDATE tenant_account_joins SET role='{value}' WHERE tenant_id='{uuid.UUID(workspace)}'"
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
            "-c",
            sql,
            timeout=20,
        )
        assert result.returncode == 0

    options_url = (
        base
        + "/console/api/workspaces/current/plugin/parameters/dynamic-options?"
        + urlencode(
            {
                "plugin_id": "trendpower/consumer_knowledge",
                "provider": PROVIDER,
                "action": "consumer_knowledge",
                "parameter": "knowledge_base_ids",
                "provider_type": "tool",
            }
        )
    )
    for denied_role in ("normal", "dataset_operator"):
        role(denied_role)
        check(
            denied_role + " cannot list knowledge choices",
            stack.http_json(options_url, headers=headers)[0] == 403,
        )
    role("editor")
    code, choices = stack.http_json(options_url, headers=headers)
    check("editor can list owned knowledge choices", code == 200 and len(choices["options"]) == 2)
    check(
        "editor cannot use foreign credential",
        stack.http_json(options_url + "&credential_id=" + str(uuid.uuid4()), headers=headers)[0] == 403,
    )
    check(
        "editor cannot manage provider credentials",
        stack.http_json(
            base + "/console/api/workspaces/current/tool-provider/builtin/" + PROVIDER + "/add",
            "POST",
            {"credentials": credential, "name": "not-allowed", "type": "api-key"},
            headers,
        )[0]
        == 403,
    )
    started_web = stack._compose(env, "up", "-d", "web", timeout=120)
    check("Dify editor ready for browser", started_web.returncode == 0)
    (root / "picker-browser-ready.json").write_text(json.dumps({"headers": headers, "app_id": app["app_id"]}))
    until = time.monotonic() + 170
    while not (root / "picker-browser-result.json").exists() and time.monotonic() < until:
        time.sleep(0.2)
    browser = json.loads((root / "picker-browser-result.json").read_text())
    check("browser selects B in knowledge node", browser.get("passed") is True)
    code, draft = stack.http_json(draft_url, headers=headers)
    actual_node = next(n["data"] for n in draft["graph"]["nodes"] if n["id"] == "knowledge")
    (root / "picker-browser-node.json").write_text(json.dumps(actual_node))
    check(
        "browser selection persisted",
        actual_node["tool_configurations"]["knowledge_base_ids"]["value"] == ["kb-b"],
    )
    # 管理API需要该隔离工作区原有owner；浏览器editor已完成选库/保存，不改变这条前提。
    role("owner")
    _, calls = ask()
    check("draft B keeps published A", len(calls) == 1 and "/kb-own/" in calls[0][0])
    before = len(knowledge.requests)
    trial_headers = {"Authorization": "Bearer " + stack.TRIAL_KEY}
    code, trial = stack.http_json(
        gateway + "/v1/dify/trial",
        "POST",
        {"app_id": app["app_id"], "order_no": stack.ORDER_LATEST, "question": "怎么开始充电？"},
        trial_headers,
    )
    assert code == 202, trial
    until = time.monotonic() + 90
    while time.monotonic() < until:
        code, result = stack.http_json(gateway + "/v1/dify/trial/" + trial["trial_id"], headers=trial_headers)
        if result.get("status") not in {"queued", "running"}:
            break
        time.sleep(0.2)
    (root / "picker-trial-result.json").write_text(json.dumps(result, ensure_ascii=False))
    check(
        "trial searches B and delivers reviewed answer",
        result.get("status") == "completed"
        and "审核后的B库指导" in str(result)
        and len(knowledge.requests) == before + 1
        and "/kb-b/" in knowledge.requests[-1][0],
    )
    check("publish B", action("publish")[0] == 200)
    answer, calls = ask()
    check(
        "App reads B after publish",
        len(calls) == 1 and "/kb-b/" in calls[0][0] and "审核后的B库指导" in str(answer),
    )
    check("restore A", action("restore", version_no=first)[0] == 200)
    _, calls = ask()
    check("restore searches A", len(calls) == 1 and "/kb-own/" in calls[0][0])
