"""正常公司身份驱动受管草稿的提交、回读、停止，不以builder通过代替执行。"""

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from execution.test_app_runtime_dify_entry import dify_chat
from execution.test_app_runtime_node_entry import _Resolver

from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.gateway import gateway_api as api
from consumer.test_consumer_knowledge_flow import Workflow
from consumer.test_consumer_publication import APP, Console, prepare

HEADERS = {"Authorization": "Bearer trial-key", "X-AIOps-Console-Session": "company-session"}
PATH = "/v1/dify/management/trial"


@pytest.fixture
def managed_trial(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, content, routes = prepare(env, monkeypatch, tmp_path)
        context = env["app"].state.gateway
        settings = env["settings"]
        settings.dify_trial_api_key = "trial-key"
        subject = replace(
            _Resolver().resolve("token-a", required_scope="aiops:orders:read").subject, b_user_id="manager"
        )
        from datetime import UTC, datetime

        from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext

        caller = ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="TENANT-A",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset(),
            resolved_at=datetime.now(UTC),
        )
        current = [caller]
        monkeypatch.setattr(
            auth, "identity", lambda *a: auth.ManagerIdentity("manager", "TENANT-A", False, "token")
        )
        monkeypatch.setattr(api, "authenticated_operator_console", lambda *a: (current[0], "token"))
        context.self_service.store.register(
            app_id=APP,
            tenant_id="TENANT-A",
            workspace_id="workspace-a",
            agent_name="WF01",
            profile_id="",
            actor="manager",
            entry="consumer",
            environment="env-a",
            targets={
                "knowledge_base_ids": ["kb-own"],
                "live_order_status": True,
                "live_billing_record": True,
            },
        )
        deleted = []

        class Admin:
            def __init__(self, *a):
                pass

            def import_app_dsl(self, *a, **k):
                return "99999999-9999-9999-9999-999999999999"

            def app_service_key(self, *a):
                return "temporary-key"

            def delete_app(self, *a):
                deleted.append(a[-1])

        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyConsoleAdmin", Admin)
        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyDslClient", Console)
        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyChatflowClient", Workflow)
        yield SimpleNamespace(
            env=env, runtime=runtime, caller=caller, current=current, routes=routes, deleted=deleted, kb=kb
        )


def submit(f):
    result = f.env["client"].post(PATH, headers=HEADERS, json={"app_id": APP, "question": "怎么开始充电？"})
    assert result.status_code == 202, result.text
    return result.json()["trial_id"]


def finish(f, trial_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = f.runtime.get_trial(f.caller, trial_id)
        if row["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    future = f.runtime._futures.get(trial_id)
    if future:
        future.result(timeout=5)
    return row


def test_unpublished_managed_trial_executes_and_reads_without_formal_route(managed_trial):
    f = managed_trial
    before = f.routes.all()
    tid = submit(f)
    row = finish(f, tid)
    assert row["status"] == "completed", row
    response = f.env["client"].post(PATH + "/result", headers=HEADERS, json={"app_id": APP, "trial_id": tid})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["report"]["summary"]
    assert f.deleted == ["99999999-9999-9999-9999-999999999999"]
    assert f.routes.all() == before == ()
    assert row["trial_source_app_id"] == APP


def test_cancel_revokes_grant_and_late_reply_cannot_win(managed_trial):
    from aiops_diagnostics.execution.chat_data_grant import DataGrantError, DataGrantStore, usable

    f = managed_trial
    outcome = []
    grant_states = []

    def cancel():
        tid = next(iter(f.runtime._trial_contexts))
        response = f.env["client"].post(
            PATH + "/cancel", headers=HEADERS, json={"app_id": APP, "trial_id": tid}
        )
        outcome.append(response)
        run = f.runtime.dify_run_store.by_qa(tid)
        grant_states.append(DataGrantStore(f.runtime.store.path).by_id(run.grant_id).state)
        with pytest.raises(DataGrantError):
            usable(DataGrantStore(f.runtime.store.path).by_id(run.grant_id))

    Workflow.after = cancel
    tid = submit(f)
    row = finish(f, tid)
    assert outcome[0].status_code == 200, outcome[0].text
    assert grant_states == ["revoked"]
    assert row["status"] == "cancelled" and row["result"] is None
    run = f.runtime.dify_run_store.by_qa(tid)
    assert run.state == "cancelled"
    with pytest.raises(DataGrantError):
        usable(DataGrantStore(f.runtime.store.path).by_id(run.grant_id))
    assert f.deleted


def test_other_account_and_application_cannot_read_or_cancel(managed_trial):
    f = managed_trial
    tid = submit(f)
    finish(f, tid)
    other = "bbbbbbbb-2222-3333-4444-555555555555"
    f.env["app"].state.gateway.self_service.store.register(
        app_id=other,
        tenant_id="TENANT-A",
        workspace_id="workspace-a",
        agent_name="other",
        profile_id="",
        actor="manager",
        entry="consumer",
        environment="env-a",
    )
    for suffix in ("/result", "/cancel"):
        response = f.env["client"].post(
            PATH + suffix, headers=HEADERS, json={"app_id": other, "trial_id": tid}
        )
        assert response.status_code == 404, response.text
    from datetime import UTC, datetime

    from aiops_diagnostics.identity.scope_context import ScopeContext

    subject = replace(f.caller.subject, b_user_id="another-manager")
    f.current[0] = ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="TENANT-A",
        data_scope=f.caller.data_scope,
        roles=frozenset(),
        permissions=frozenset(),
        resolved_at=datetime.now(UTC),
    )
    response = f.env["client"].post(PATH + "/result", headers=HEADERS, json={"app_id": APP, "trial_id": tid})
    assert response.status_code == 404, response.text


def test_trial_rechecks_company_scope_before_delivering_reply(managed_trial):
    f = managed_trial
    Workflow.after = lambda: f.current.__setitem__(
        0, _Resolver().resolve("token-b", required_scope="aiops:orders:read")
    )
    tid = submit(f)
    row = finish(f, tid)
    assert row["status"] == "failed" and row["result"] is None
    assert f.deleted


def test_management_publish_key_cannot_run_trial(managed_trial):
    f = managed_trial
    response = f.env["client"].post(
        PATH,
        headers={**HEADERS, "Authorization": "Bearer publication-key"},
        json={"app_id": APP, "question": "怎么开始充电？"},
    )
    assert response.status_code == 403
    assert not f.runtime._trial_contexts


def test_trial_bridge_preserves_session_uses_trial_key_and_rejects_injected_scope(monkeypatch):
    import threading

    import httpx

    from aiops_diagnostics.dify import dify_console_publish as bridge

    seen = []

    def upstream(url, **kwargs):
        seen.append((url, kwargs))
        if url.endswith("/summary"):
            return 200, {"role": "editor"}
        if "/apps/" in url:
            return 200, {"id": APP}
        return 200, {"trial_id": "tr_" + "a" * 32, "status": "queued"}

    monkeypatch.setattr(bridge, "_request", upstream)
    settings = bridge.BridgeSettings(
        "127.0.0.1",
        0,
        "http://dify",
        "http://gateway",
        "publish-key",
        "http://console.example",
        "workspace",
        trial_key="trial-key",
    )
    server = bridge.make_server(settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {
        "Origin": "http://console.example",
        "Referer": f"http://console.example/app/{APP}/workflow",
        "Cookie": "access_token=session; csrf_token=csrf",
        "X-CSRF-Token": "csrf",
    }
    body = {"action": "trial", "app_id": APP, "question": "怎么充电？"}
    try:
        url = f"http://127.0.0.1:{server.server_port}/__aiops/manage"
        response = httpx.post(url, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert seen[-1][0].endswith("/v1/dify/management/trial")
        assert seen[-1][1]["headers"]["Authorization"] == "Bearer trial-key"
        assert seen[-1][1]["headers"]["X-AIOps-Console-Session"] == "session"
        forwarded = len([x for x in seen if "/management/trial" in x[0]])
        for extra in ({"tenant_id": "other"}, {"question": []}, {"order_no": "/"}, {"trial_id": "wrong"}):
            assert httpx.post(url, headers=headers, json={**body, **extra}).status_code == 400
        assert len([x for x in seen if "/management/trial" in x[0]]) == forwarded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
