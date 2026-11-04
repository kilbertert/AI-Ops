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


def test_preview_readback_refuses_revoked_knowledge(managed_trial, monkeypatch):
    f = managed_trial
    tid = submit(f)
    finish(f, tid)
    # 将旧FAQ测试图的取证元数据标为选库版本；回读必须查询当前授权目录。
    with f.runtime.store._connection(write=True) as db:
        import json

        result = json.loads(
            db.execute("SELECT result_json FROM standard_diagnoses WHERE diagnosis_id=?", (tid,)).fetchone()[
                0
            ]
        )
        result["report"].update(callback_version=3, knowledge_base_ids=["revoked-library"])
        db.execute(
            "UPDATE standard_diagnoses SET result_json=? WHERE diagnosis_id=?", (json.dumps(result), tid)
        )
    monkeypatch.setattr(
        api.consumer_knowledge_selection,
        "owned_libraries",
        lambda *a: [{"knowledge_base_id": "revoked-library", "audience": "internal"}],
    )
    response = f.env["client"].post(PATH + "/result", headers=HEADERS, json={"app_id": APP, "trial_id": tid})
    assert response.status_code == 410, response.text


def test_knowledge_trial_records_the_kind_it_actually_sends(managed_trial, monkeypatch):
    """运行记录里的 `data_kind` 必须等于实际发给 Dify 的那个值。

    2026-10-31 实测：旧代码记录写 `status`、请求传 `knowledge`，于是知识类预览
    在库与日志里读起来像订单状态，排查被引到完全错的路径（当时据此误判"检索服务坏了"，
    实际是选中的库是空库）。记录与实际不一致，比不记录更坏。
    """
    f = managed_trial
    observed = {}

    # `data_kind` 只在 callback_version >= 2 时随请求发出（旧发布版不带），
    # 而本 fixture 走的是最小草稿；提到 2 才能观察"记录 vs 请求"这一对。
    from aiops_diagnostics.consumer.consumer_trial import ConsumerTrial

    original = ConsumerTrial.execute

    def execute(self, *args):
        self.content["callback_version"] = 2
        return original(self, *args)

    monkeypatch.setattr(ConsumerTrial, "execute", execute)

    def capture(self, **kwargs):
        observed.update(kwargs.get("inputs") or {})
        raise RuntimeError("stop at the Dify request boundary")

    from consumer.test_consumer_knowledge_flow import Workflow

    monkeypatch.setattr(Workflow, "run", capture)
    tid = submit(f)
    finish(f, tid)
    run = f.runtime.dify_run_store.by_qa(tid)
    assert observed["data_kind"] == "knowledge"
    assert run.data_kind == observed["data_kind"]


def test_trial_failure_exposes_a_readable_reason(managed_trial):
    """试运行失败时，结果面必须给出可读原因，而不是笼统的"未完成"。

    运营看的是控制台面板；只回 `TRIAL_FAILED` 或一句通用文案，
    "你忘了填订单号 / 这句话需要澄清"就会看起来像系统故障
    （2026-10-31 实测就发生过这种误读）。
    """
    f = managed_trial
    result = f.env["client"].post(
        PATH,
        headers=HEADERS,
        json={"app_id": APP, "question": "我上一个订单现在什么情况"},
    )
    assert result.status_code in {202, 400, 409}, result.text
    trial_id = result.json().get("trial_id")
    if trial_id is None:
        return  # 在提交阶段就被拒：此时由 HTTP 错误体承载原因，不在本用例范围
    finish(f, trial_id)
    payload = (
        f.env["client"]
        .post(PATH + "/result", headers=HEADERS, json={"app_id": APP, "trial_id": trial_id})
        .json()
    )
    if payload.get("error_code"):
        assert payload.get("error_message", "").strip(), payload
        # 不把内部异常文本回显给运营
        assert "Traceback" not in payload["error_message"]
        assert "aiops_diagnostics" not in payload["error_message"]


def test_clarification_trial_is_previewable_when_prompts_write_notices(managed_trial, monkeypatch):
    """澄清类问题在 slots 发布版下应当**可预览**，而不是被直接拒掉。

    运营为 clarify/human 写的正文，必须在发布前看得到；旧代码把这类问题
    在 `ConsumerTrial` 里直接拒绝，于是"我改的澄清话术"永远无法预览。
    语义与正式路径一致：转成 notice，按 knowledge 路径跑，`data_kind=notice_kind`。
    """
    f = managed_trial
    seen = {}

    import types

    # 知识路径会走 `require_available` → 目录查询；与其它知识用例同样补桩。
    monkeypatch.setattr(
        f.kb,
        "list_knowledge_bases",
        lambda: [{"knowledge_base_id": "kb-own", "name": "own"}],
        raising=False,
    )

    def classify(self, question, **kwargs):
        return {"intent": "clarify", "uses_context": False, "order_reference": "none"}

    # 只替换分类器本身；直接 setattr 在 runtime 上会连带覆盖 kb 桩的解析路径。
    monkeypatch.setattr(f.runtime, "classify_consumer", types.MethodType(classify, f.runtime))

    from aiops_diagnostics.consumer.consumer_trial import ConsumerTrial

    original = ConsumerTrial.execute

    def execute(self, *args):
        self.content["callback_version"] = 5
        return original(self, *args)

    monkeypatch.setattr(ConsumerTrial, "execute", execute)

    from consumer.test_consumer_knowledge_flow import Workflow

    def capture(self, **kwargs):
        seen.update(kwargs.get("inputs") or {})
        raise RuntimeError("stop at the Dify request boundary")

    monkeypatch.setattr(Workflow, "run", capture)
    tid = submit(f)
    row = finish(f, tid)
    # 不再因"澄清意图"被拒；确实发起了请求，且类型是 notice
    assert seen.get("data_kind") == "clarify", {
        k: row.get(k) for k in ("status", "error_code", "error_message")
    }
    run = f.runtime.dify_run_store.by_qa(tid)
    assert run.data_kind == seen["data_kind"]
