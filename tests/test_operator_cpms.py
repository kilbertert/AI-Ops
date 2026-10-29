"""管家 API 工具：真 Gateway/存储/HTTP，合成身份、CPMS及派发节点。"""

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from execution.test_app_runtime_dify_entry import dify_chat
from execution.test_m2_close_contract import NODE_A_TOKEN, _write_config
from execution.test_m3_gateway_exec_config import _write_manifest

from aiops_diagnostics import operator_cpms
from aiops_diagnostics.dify_app_registry import DifyAppBinding
from aiops_diagnostics.execution import chat_execution, node_publication
from aiops_diagnostics.faq import PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig, AgentManager, AgentStore

HEADERS = {"Authorization": "Bearer operator-private", "X-Business-Entry": "operator"}
APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ORDER = "ORDER-1001"


def scope(site="SITE-1", user="OPERATOR-A", tenant="TENANT-A"):
    subject = SubjectRecord(b_user_id=user, c_user_id="C-" + user, tenant_id=tenant, user_type="5")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="organ", site_ids=(site,)),
        roles=frozenset({"ROLE_OPERATOR"}),
        permissions=frozenset({"aiops:diagnoses:write", "aiops:orders:read"}),
    )


class Resolver:
    current = None
    hook = None

    def resolve(self, token, **kwargs):
        if token != "operator-private":
            from aiops_diagnostics.identity.caller_auth import CallerAuthError

            raise CallerAuthError("invalid", code="caller_auth.invalid")
        if self.hook:
            self.hook()
        return self.current


class Directory:
    def roles_for_b_user(self, user, tenant):
        return (PlatformRoleRecord(user, "C-" + user, tenant, "admin"),)

    def roles_for_c_user(self, user, tenant):
        return (PlatformRoleRecord("OPERATOR-A", user, tenant, "admin"),)


@contextmanager
def environment(tmp_path, monkeypatch):
    state = {"calls": [], "site": "SITE-1", "tenant": "TENANT-A", "hook": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            state["calls"].append((self.path, self.headers.get("Authorization")))
            row = {
                "orderNo": ORDER,
                "tenantId": state["tenant"],
                "siteId": state["site"],
                "status": 2,
                "stoppedReasonContent": "Communication interrupted",
                "phone": "PRIVATE-PHONE",
            }
            if "/getChargingMonitor/" in self.path:
                data = {"records": [row], "total": 100, "size": 50, "current": 1}
            else:
                data = {"orderNo": ORDER, "siteId": state["site"], "chOrderInfoModel": row}
            if state["hook"]:
                state["hook"]()
            body = json.dumps({"code": 0, "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as f:
            settings = f["settings"]
            settings.chat_execution_bindings_file = ""
            settings.chat_route_bindings_file = str(f["db"])
            config = _write_config(tmp_path)
            manifest = _write_manifest(tmp_path)
            raw = json.loads(manifest.read_text())
            raw["schema"] = "operator-exec-config/1"
            raw["tools"] = [
                {
                    "name": name,
                    "description": "d",
                    "input_schema": json.dumps(
                        {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
                    ),
                }
                for name in operator_cpms.TOOL_NAMES
            ]
            manifest.write_text(json.dumps(raw))
            target = node_publication.NodeTarget(
                APP,
                "TENANT-A",
                "env-a",
                str(config),
                str(manifest),
                entry="operator",
                cpms_base_url=f"http://127.0.0.1:{server.server_port}",
                proxy_base_url=f["gateway_base"],
            )
            target_file = tmp_path / "node-targets.json"
            target_file.write_text(
                json.dumps({"schema": "aiops-node-targets/1", "targets": [target.__dict__]})
            )
            target_file.chmod(0o600)
            settings.chat_node_targets_file = str(target_file)
            ctx = f["app"].state.gateway
            resolver = Resolver()
            resolver.current = scope()
            ctx.caller_resolver = resolver
            from aiops_diagnostics.identity.query_scope import resolve_query_scope

            ctx.runtime._trial_operator_scope = SimpleNamespace(
                site_scope_by_b_user_id=lambda *_: resolve_query_scope(resolver.current)
            )
            ctx.platform_resolver = PlatformIdentityResolver(Directory())
            manager = AgentManager(AgentStore(f["db"]))
            admin = SimpleNamespace(effective_tenant_id="TENANT-A", roles={"ROLE_PLATFORM_ADMIN"})
            agent = manager.create(
                admin,
                name="operator-node",
                description="test",
                config=AgentConfig(
                    agent_type="operations",
                    prompt="baseline",
                    knowledge_base_ids=(),
                    model="aiops-api",
                    output_contract="diagnosis-v1",
                ),
            )
            manager.publish(admin, agent.agent_id, expected_revision=agent.revision)
            ctx.agent_manager = manager
            binding = DifyAppBinding("TENANT-A", "operator", APP, "operator-node")
            ctx.runtime.dify_app_registry.put(binding)
            candidate = {"prompt": "SOP_ONE", "enabled": list(operator_cpms.TOOL_NAMES)}
            monkeypatch.setattr(
                node_publication,
                "DifyDslClient",
                lambda _: SimpleNamespace(
                    tool_provider_ids=lambda: {"cpms-node": "provider-id"},
                    export_dsl=lambda: json.dumps(
                        {
                            "app": {"mode": "agent-chat"},
                            "model_config": {
                                "pre_prompt": candidate["prompt"],
                                "agent_mode": {
                                    "tools": [
                                        {
                                            "tool_name": name,
                                            "provider_id": "provider-id",
                                            "provider_type": "api",
                                            "enabled": name in candidate["enabled"],
                                        }
                                        for name in operator_cpms.TOOL_NAMES
                                    ]
                                },
                            },
                        }
                    ),
                ),
            )
            source = SimpleNamespace(workspace_id="workspace")

            def publish(revision):
                outcome = node_publication.publish(manager, admin, binding, source, target)
                node_publication.advance(ctx, outcome, binding, target, revision)
                return chat_execution.select_binding(
                    settings, resolver.current, scenario=operator_cpms.SCENARIO, entry="operator"
                )

            route = publish(0)
            yield {
                **f,
                "ctx": ctx,
                "state": state,
                "resolver": resolver,
                "target": target,
                "candidate": candidate,
                "route": route,
                "publish": publish,
                "manifest": manifest,
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def operator(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        yield f


def request(client):
    return client.post(
        "/v1/assistant/questions",
        headers=HEADERS,
        json={"question": "请检查这张订单为什么中断", "order_no": ORDER},
    )


def node(f, run_id, *, tools=operator_cpms.TOOL_NAMES, before_complete=None):
    client = f["client"]
    head = {"Authorization": "Bearer " + NODE_A_TOKEN}
    publication = client.post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
    assert publication.status_code == 200, publication.text
    head["X-AI-Ops-Exec-Config"] = publication.json()["sha256"]
    for tool in tools:
        path = f"/v1/internal/operator-runs/{run_id}/tools/{tool}"
        response = client.post(path + "/authorization", headers=head, json={})
        assert response.status_code == 200, response.text
        grant = response.json()
        assert grant["site_ids"] == ["SITE-1"]
        url = grant["base_url"] + operator_cpms.PATHS[tool] + ORDER
        if tool == operator_cpms.TOOL_NAMES[1]:
            url += "?current=1&size=50"
        response = client.get(url, headers={"Authorization": "Bearer " + grant["bearer"]})
        assert response.status_code == 200, response.text
        assert "PRIVATE-PHONE" not in response.text
        evidence = operator_cpms.project_evidence(response.json(), ORDER, tool)
        response = client.post(
            path + "/evidence",
            headers=head,
            json={"authorization_id": grant["authorization_id"], "evidence": evidence},
        )
        assert response.status_code == 200, response.text
    if before_complete:
        before_complete()
    answer = {
        "summary": "订单异常结束。",
        "probable_cause": "通信中断，仍需核查现场。",
        "next_steps": ["核对现场网络。"],
        "limitations": ["监测仅有限样本。"],
        "evidence_tools": list(tools),
    }
    return client.post(
        f"/v1/internal/operator-runs/{run_id}/completions",
        headers=head,
        json={"status": "completed", "answer": json.dumps(answer, ensure_ascii=False)},
    )


def terminal(f, qa):
    for _ in range(200):
        response = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
        if response.status_code != 200 or response.json()["status"] not in {"queued", "running"}:
            return response
        time.sleep(0.01)
    raise AssertionError("no terminal")


def test_operator_runs_two_cpms_tools_and_original_readback(operator, monkeypatch):
    f = operator
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    assert response.status_code == 202, response.text
    final = terminal(f, response.json()["qa_id"])
    assert final.status_code == 200 and final.json()["status"] == "completed", final.text
    assert "cpms.orderDetail" in final.text and "cpms.chargingMonitor" in final.text
    assert [p for p, _ in f["state"]["calls"]].count("/chOrderInfo/getOrderInfoDetail/" + ORDER) == 4
    assert len(f["state"]["calls"]) == 5  # 节点取证2次，签发/完成/回读授权各1次。
    assert all(token == "Bearer operator-private" for _, token in f["state"]["calls"])
    f["candidate"]["enabled"] = []
    f["publish"](f["route"].route_revision)
    denied = request(f["client"])
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "OPERATOR_DETAIL_TOOL_DISABLED"
    assert (
        f["client"].get("/v1/assistant/questions/" + response.json()["qa_id"], headers=HEADERS).status_code
        == 410
    )
    assert all(
        q.get("result") is None
        for q in f["client"].get("/v1/assistant/questions", headers=HEADERS).json()["questions"]
    )


@pytest.mark.parametrize("fault", ["wrong-site", "revoked-during-read", "cancel", "missing-evidence"])
def test_refused_data_or_completion_cannot_succeed(operator, monkeypatch, fault):
    f = operator
    if fault == "wrong-site":
        f["state"]["site"] = "SITE-OTHER"
    if fault == "revoked-during-read":
        f["state"]["hook"] = lambda: setattr(f["resolver"], "current", scope("SITE-OTHER"))

    def dispatch(_, cfg, run):
        if fault == "missing-evidence":
            return node(f, run, tools=())
        before = None
        if fault == "cancel":
            qa = f["ctx"].agent_run_store.get(run).qa_id

            def before():
                return f["client"].post(f"/v1/assistant/questions/{qa}/cancel", headers=HEADERS)

        return node(f, run, before_complete=before)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = request(f["client"])
    if fault in {"wrong-site", "revoked-during-read"}:
        assert response.status_code in {403, 404}, response.text
        assert not f["ctx"].operator_cpms.tasks
        return
    assert response.status_code == 202, response.text
    final = terminal(f, response.json()["qa_id"])
    assert final.status_code != 200 or final.json()["status"] != "completed", final.text
    assert not final.json().get("result")


def test_draft_toggle_does_not_publish_and_disabled_tool_cannot_be_called(operator, monkeypatch):
    f = operator
    f["candidate"]["enabled"] = [operator_cpms.TOOL_NAMES[0]]
    assert len(json.loads(f["route"].manifest_text)["tools"]) == 2
    current = f["publish"](f["route"].route_revision)
    assert [v["name"] for v in json.loads(current.manifest_text)["tools"]] == [operator_cpms.TOOL_NAMES[0]]
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    final = terminal(f, response.json()["qa_id"])
    assert final.json()["status"] == "failed", final.text
    assert len(f["state"]["calls"]) == 2


@pytest.mark.parametrize(
    "fault",
    ["wrong-node", "wrong-manifest", "unknown-tool", "expired", "retracted", "old-completion", "payload"],
)
def test_refusals_happen_before_business_reads(operator, monkeypatch, fault):
    f = operator
    entered, release = threading.Event(), threading.Event()
    runs = []

    def dispatch(_, cfg, run):
        runs.append(run)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    response = request(f["client"])
    assert response.status_code == 202, response.text
    assert entered.wait(5)
    try:
        run = f["ctx"].agent_run_store.get(runs[0])
        head = {"Authorization": "Bearer " + NODE_A_TOKEN, "X-AI-Ops-Exec-Config": run.expected_agent_version}
        path = f"/v1/internal/operator-runs/{run.run_id}/tools/{operator_cpms.TOOL_NAMES[0]}/authorization"
        body = {}
        if fault == "wrong-node":
            head["Authorization"] = "Bearer wrong-node"
        elif fault == "wrong-manifest":
            head["X-AI-Ops-Exec-Config"] = "0" * 64
        elif fault == "unknown-tool":
            path = path.replace(operator_cpms.TOOL_NAMES[0], "shell")
        elif fault == "expired":
            with f["ctx"].agent_run_store._connection(write=True) as c:
                c.execute(
                    "UPDATE agent_runs SET expires_at=? WHERE run_id=?",
                    ("2000-01-01T00:00:00+00:00", run.run_id),
                )
        elif fault == "retracted":
            f["candidate"]["enabled"] = []
            f["publish"](f["route"].route_revision)
        elif fault == "old-completion":
            path = "/v1/internal/charging-status/completions/" + run.run_id
            body = {"status": "completed"}
        elif fault == "payload":
            body = {"order_no": "OTHER"}
        denied = f["client"].post(path, headers=head, json=body)
        assert denied.status_code in {403, 422}, denied.text
        assert len(f["state"]["calls"]) == 1  # 签发授权已读一次，节点没有取数。
        assert not f["ctx"].operator_cpms.tasks[run.run_id].evidence
    finally:
        release.set()


def test_readback_checks_current_order_scope_without_replacing_evidence(operator, monkeypatch):
    f = operator
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    qa = response.json()["qa_id"]
    assert terminal(f, qa).json()["status"] == "completed"
    before = f["ctx"].agent_run_store.by_qa(qa).snapshot
    f["state"]["site"] = "SITE-OTHER"
    denied = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "CPMS_SCOPE_MISMATCH"
    assert f["ctx"].agent_run_store.by_qa(qa).snapshot == before


def test_scope_revoke_during_node_read_is_not_only_checked_at_start(operator, monkeypatch):
    f = operator

    def hook():
        if len(f["state"]["calls"]) == 2:
            f["resolver"].current = scope("SITE-OTHER")

    f["state"]["hook"] = hook
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    assert response.status_code == 202
    final = terminal(f, response.json()["qa_id"])
    assert final.status_code != 200 or final.json()["status"] == "failed"
    future = f["ctx"].runtime._futures.get(response.json()["qa_id"])
    if future is not None:
        future.result(timeout=5)
    assert f["ctx"].agent_run_store.by_qa(response.json()["qa_id"]).state == "failed"


def test_runtime_tools_are_not_used_for_operator_trial(operator):
    from aiops_diagnostics.gateway_api import StandardAPIError, _workflow_trial_draft

    f = operator
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    f["settings"].environment = "env-a"
    with DifyWorkspaces(f["db"])._connection(write=True) as c:
        c.execute(
            "INSERT INTO dify_workspaces "
            "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
            "VALUES (?,?,?,'ready',?,?)",
            ("TENANT-A", "env-a", "workspace", "2026-10-26", "2026-10-26"),
        )
    with pytest.raises(StandardAPIError) as failure:
        _workflow_trial_draft(f["ctx"], APP, scope())
    assert failure.value.code == "OPERATOR_TRIAL_IDENTITY_REQUIRED"
    assert f["state"]["calls"] == []


def test_publish_retract_restore_use_operator_route_via_http(operator, monkeypatch):
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    f = operator
    f["settings"].environment = "env-a"
    f["settings"].dify_publish_api_key = "publish-only-key"
    f["settings"].dify_console_base_url = "http://127.0.0.1:1"
    f["settings"].dify_console_api_key = "console-only-key"
    with DifyWorkspaces(f["db"])._connection(write=True) as c:
        c.execute(
            "INSERT INTO dify_workspaces "
            "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
            "VALUES (?,?,?,'ready',?,?)",
            ("TENANT-A", "env-a", "workspace", "2026-10-26", "2026-10-26"),
        )
    head = {"Authorization": "Bearer publish-only-key"}
    f["candidate"]["prompt"] = "SOP_TWO"
    published = f["client"].post("/v1/dify/publish", headers=head, json={"app_id": APP})
    assert published.status_code == 200, published.text
    current = chat_execution.select_binding(
        f["settings"], scope(), entry="operator", scenario=operator_cpms.SCENARIO
    )
    assert "SOP_TWO" in current.manifest_text
    retracted = f["client"].post("/v1/dify/retract", headers=head, json={"app_id": APP})
    assert retracted.status_code == 200, retracted.text
    assert request(f["client"]).status_code == 410
    restored = f["client"].post(
        "/v1/dify/restore", headers=head, json={"app_id": APP, "version_no": f["route"].agent_version_no}
    )
    assert restored.status_code == 200, restored.text
    current = chat_execution.select_binding(
        f["settings"], scope(), entry="operator", scenario=operator_cpms.SCENARIO
    )
    assert "SOP_ONE" in current.manifest_text and "SOP_TWO" not in current.manifest_text
    assert f["state"]["calls"] == []


@pytest.mark.parametrize("native", [False, True])
def test_operator_trial_uses_draft_without_changing_publication(operator, monkeypatch, native):
    from aiops_diagnostics.dify_workspaces import DifyWorkspaces

    f = operator
    f["settings"].environment = "env-a"
    f["settings"].dify_trial_api_key = "trial-only"
    with DifyWorkspaces(f["db"])._connection(write=True) as c:
        c.execute(
            "INSERT INTO dify_workspaces "
            "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
            "VALUES (?,?,?,'ready',?,?)",
            ("TENANT-A", "env-a", "workspace", "2026-10-26", "2026-10-26"),
        )
    seen = []
    monkeypatch.setattr(
        f["ctx"].operator_cpms,
        "console_identity",
        lambda session: (
            {"token": "operator-private", "user_id": "OPERATOR-A", "tenant_id": "TENANT-A"}
            if session == "valid-session"
            else {}
        ),
    )

    def dispatch(_, cfg, run):
        seen.append(f["ctx"].agent_run_store.get(run).execution_manifest)
        return node(f, run)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    f["candidate"]["prompt"] = "TRIAL_ONLY"
    head = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "valid-session"}
    payload = {"app_id": APP, "order_no": ORDER}
    if native:
        payload["operator_preview"] = {
            "pre_prompt": "UNSAVED_NATIVE_DRAFT",
            "tools": [
                {"tool_name": name, "provider_id": "provider-id", "provider_type": "api", "enabled": True}
                for name in operator_cpms.TOOL_NAMES
            ],
        }
    response = f["client"].post("/v1/dify/trial", headers=head, json=payload)
    assert response.status_code == 202, response.text
    trial = response.json()["trial_id"]
    for _ in range(200):
        result = f["client"].get("/v1/dify/trial/" + trial, headers=head)
        assert result.status_code == 200, result.text
        if result.json()["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert result.json()["status"] == "completed", result.text
    from aiops_diagnostics.dify_console_publish import _trial_report_text

    panel = _trial_report_text(result.json(), "completed")
    answer = f["ctx"].agent_run_store.by_qa(trial).snapshot["answer"]
    for text in (answer["summary"], answer["probable_cause"], *answer["next_steps"], *answer["limitations"]):
        assert text in panel
    assert "cpms.orderDetail" in panel and "cpms.chargingMonitor" in panel
    assert ("UNSAVED_NATIVE_DRAFT" if native else "TRIAL_ONLY") in seen[0]
    current = chat_execution.select_binding(
        f["settings"], scope(), entry="operator", scenario=operator_cpms.SCENARIO
    )
    assert current == f["route"] and "TRIAL_ONLY" not in current.manifest_text


def test_node_provider_has_real_paths_and_no_shared_credentials():
    from aiops_diagnostics.ops_provider import ensure_node_provider

    calls = []

    class Console:
        def _call(self, workspace, path, **kwargs):
            calls.append((workspace, path, kwargs))
            return [] if kwargs.get("method", "GET") == "GET" else {"result": "success"}

    tools = [
        {"name": name, "description": name, "input_schema": '{"type":"object","properties":{}}'}
        for name in operator_cpms.TOOL_NAMES
    ]
    result = ensure_node_provider(Console(), "workspace", "https://gateway.example", {"tools": tools})
    assert result["action"] == "created"
    payload = calls[-1][2]["body"]
    assert payload["credentials"] == {"auth_type": "none"}
    assert set(json.loads(payload["schema"])["paths"]) == {
        "/v1/operator-tools/" + name for name in operator_cpms.TOOL_NAMES
    }


@pytest.mark.parametrize("fault", ["no-login", "foreign-tool", "injected-token"])
def test_native_preview_rejects_before_business(operator, monkeypatch, fault):
    f = operator
    f["settings"].dify_trial_api_key = "trial-only"
    draft = {
        "pre_prompt": "preview",
        "tools": [
            {
                "provider_id": "provider-id",
                "provider_type": "api",
                "tool_name": "cpms_order_detail",
                "enabled": True,
            }
        ],
    }
    if fault == "foreign-tool":
        draft["tools"][0]["tool_name"] = "refund"
    if fault == "injected-token":
        draft["tools"][0]["bearer"] = "supplied"
    before = len(f["state"]["calls"])
    response = f["client"].post(
        "/v1/dify/trial",
        headers={"Authorization": "Bearer trial-only"},
        json={"app_id": APP, "order_no": ORDER, "operator_preview": draft},
    )
    assert response.status_code in {403, 422}
    assert len(f["state"]["calls"]) == before


def test_native_cancel_only_owns_trial_and_revokes_grants(operator, monkeypatch):
    f = operator
    f["settings"].dify_trial_api_key = "trial-only"
    pending = threading.Event()
    entered = threading.Event()

    def dispatch(*args):
        entered.set()
        pending.wait(4)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(
        "aiops_diagnostics.gateway_api.authenticated_operator_console",
        lambda ctx, session: (f["resolver"].current, "operator-private"),
    )
    try:
        response = request(f["client"])
        qa = response.json()["qa_id"]
        assert entered.wait(2)
        run = f["ctx"].agent_run_store.by_qa(qa)
        head = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "valid"}
        cancel = "/v1/dify/trial/" + qa + "/cancel"
        body = {"app_id": APP}
        assert f["client"].post(cancel, headers=head, json=body).status_code == 404  # 正式任务不是试用。
        f["ctx"].operator_cpms.tasks[run.run_id].trial = True
        original = f["resolver"].current
        f["resolver"].current = scope(user="OTHER")
        assert f["client"].post(cancel, headers=head, json=body).status_code == 404
        f["resolver"].current = original
        assert f["client"].post(cancel, headers=head, json={"app_id": "foreign-app"}).status_code == 404
        response = f["client"].post(cancel, headers=head, json=body)
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert f["ctx"].agent_run_store.get(run.run_id).state == "revoked"
    finally:
        pending.set()


def test_native_draft_cannot_borrow_another_provider(operator):
    f = operator
    source = SimpleNamespace(workspace_id="workspace")
    draft = {
        "pre_prompt": "native",
        "tools": [
            {
                "provider_id": "foreign-provider",
                "provider_type": "api",
                "tool_name": "cpms_order_detail",
                "enabled": True,
            }
        ],
    }
    before = len(f["state"]["calls"])
    with pytest.raises(ValueError, match="unimplemented tool"):
        node_publication.candidate(source, f["target"], operator_preview=draft)
    assert len(f["state"]["calls"]) == before


def test_node_provider_without_task_cannot_read_any_business(operator):
    for name in operator_cpms.TOOL_NAMES:
        response = operator["client"].post("/v1/operator-tools/" + name, json={})
        assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    assert operator["state"]["calls"] == []


@pytest.mark.parametrize("header", ["X-Third-Session", "third-session"])
def test_operator_session_mode_is_refused_before_cpms_or_run(operator, header):
    f = operator
    response = f["client"].post(
        "/v1/assistant/questions",
        headers={**HEADERS, header: "legacy-session"},
        json={"question": "诊断订单", "order_no": ORDER},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OPERATOR_COMPANY_TOKEN_REQUIRED"
    assert f["state"]["calls"] == [] and f["ctx"].operator_cpms.tasks == {}


def test_readback_reauthenticates_diagnosis_scope(operator, monkeypatch):
    from aiops_diagnostics.identity.caller_auth import CALLER_AUTH_FORBIDDEN, CallerAuthError

    f = operator
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    response = request(f["client"])
    qa = response.json()["qa_id"]
    assert terminal(f, qa).json()["status"] == "completed"
    normal = f["resolver"].resolve
    checked = []

    def revoked(token, *, required_scope, **kwargs):
        checked.append(required_scope)
        if required_scope == "aiops:diagnoses:write":
            raise CallerAuthError("revoked", code=CALLER_AUTH_FORBIDDEN)
        return normal(token, required_scope=required_scope, **kwargs)

    monkeypatch.setattr(f["resolver"], "resolve", revoked)
    before = len(f["state"]["calls"])
    response = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
    assert response.status_code == 403 and response.json()["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert "aiops:diagnoses:write" in checked and len(f["state"]["calls"]) == before


@pytest.mark.parametrize("header", ["X-Third-Session", "third-session"])
def test_readback_refuses_shared_session_before_cpms(operator, monkeypatch, header):
    f = operator
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    qa = request(f["client"]).json()["qa_id"]
    assert terminal(f, qa).json()["status"] == "completed"
    before = len(f["state"]["calls"])
    response = f["client"].get("/v1/assistant/questions/" + qa, headers={**HEADERS, header: "legacy-session"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OPERATOR_COMPANY_TOKEN_REQUIRED"
    assert len(f["state"]["calls"]) == before


def tenant_owner():
    original = scope()
    owner = replace(original.subject, user_type="1")
    return replace(
        original, caller=owner, subject=owner, data_scope=DataScope(type="all"), scope_fingerprint=""
    )


def test_tenant_owner_uses_only_selected_orders_site(operator, monkeypatch):
    f = operator
    f["resolver"].current = tenant_owner()
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    submitted = request(f["client"])
    assert submitted.status_code == 202, submitted.text
    final = terminal(f, submitted.json()["qa_id"])
    assert final.status_code == 200 and final.json()["status"] == "completed", final.text
    assert len(f["state"]["calls"]) == 5
    f["resolver"].current = replace(
        tenant_owner(), subject=scope().subject, caller=scope().caller, scope_fingerprint=""
    )
    refused = f["client"].get("/v1/assistant/questions/" + submitted.json()["qa_id"], headers=HEADERS)
    assert refused.status_code == 404


def test_owner_readback_refuses_changed_order_site(operator, monkeypatch):
    f = operator
    f["resolver"].current = tenant_owner()
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    qa = request(f["client"]).json()["qa_id"]
    assert terminal(f, qa).json()["status"] == "completed"
    snapshot = f["ctx"].agent_run_store.by_qa(qa).snapshot
    f["state"]["site"] = "SITE-OTHER"
    refused = f["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "OPERATOR_SCOPE_CHANGED"
    assert f["ctx"].agent_run_store.by_qa(qa).snapshot == snapshot


def test_employee_with_several_sites_gets_one_site_in_task_grant(operator, monkeypatch):
    f = operator
    f["resolver"].current = replace(
        scope(), data_scope=DataScope(type="organ", site_ids=("SITE-1", "SITE-OTHER")), scope_fingerprint=""
    )
    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda _, cfg, run: node(f, run))
    submitted = request(f["client"])
    assert submitted.status_code == 202
    assert terminal(f, submitted.json()["qa_id"]).json()["status"] == "completed"


@pytest.mark.parametrize("fault", ["other-tenant", "site-changed", "owner-demoted"])
def test_tenant_owner_does_not_relax_task_scope(operator, monkeypatch, fault):
    f = operator
    f["resolver"].current = tenant_owner()
    if fault == "other-tenant":
        f["state"]["tenant"] = "TENANT-B"

    def dispatch(_, cfg, run):
        if fault == "site-changed":
            f["state"]["site"] = "SITE-OTHER"
        if fault == "owner-demoted":
            f["resolver"].current = scope("SITE-OTHER")
        return node(f, run)

    monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
    submitted = request(f["client"])
    if fault == "other-tenant":
        assert submitted.status_code == 403 and submitted.json()["error"]["code"] == "CPMS_SCOPE_MISMATCH"
        assert f["ctx"].operator_cpms.tasks == {}
    else:
        assert submitted.status_code == 202, submitted.text
        qa = submitted.json()["qa_id"]
        future = f["ctx"].runtime._futures.get(qa)
        if future:
            future.result(timeout=5)
        row = f["ctx"].store.get_assistant_question(qa, tenant_owner().scope_fingerprint)
        assert row["status"] == "failed" and row["result"] is None


@pytest.mark.parametrize(
    "data_scope", [DataScope(type="all"), DataScope(type="organ"), DataScope(type="self")]
)
def test_non_owner_cannot_use_empty_or_all_scope(operator, data_scope):
    f = operator
    f["resolver"].current = replace(scope(), data_scope=data_scope, scope_fingerprint="")
    submitted = request(f["client"])
    assert submitted.status_code == 403, submitted.text
    assert not f["state"]["calls"] and not f["ctx"].operator_cpms.tasks


def test_answer_rejection_logs_only_fixed_reason(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="aiops.operator")
    answer = {
        "summary": "PRIVATE-ORDER-123 已退款",
        "probable_cause": "PRIVATE-TOKEN-456",
        "next_steps": [],
        "limitations": [],
        "evidence_tools": [operator_cpms.TOOL_NAMES[0]],
    }
    with pytest.raises(operator_cpms.OperatorRefused, match="OPERATOR_INVALID_ANSWER"):
        operator_cpms.validate_answer(answer, {operator_cpms.TOOL_NAMES[0]: {}})
    assert "operator_answer_rejected reason=mutation_claim" in caplog.text
    assert "PRIVATE-ORDER" not in caplog.text
    assert "PRIVATE-TOKEN" not in caplog.text
