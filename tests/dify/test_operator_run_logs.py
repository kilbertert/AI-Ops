"""LOG-01：真实临时账本/HTTP，合成公司与 Dify 身份；不调用业务结果回读。"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from operations.test_operator_cpms import APP, operator, scope  # noqa: F401

from aiops_diagnostics.dify import self_service_identity as auth
from aiops_diagnostics.execution import operator_run_logs as logs
from aiops_diagnostics.execution.agent_runs import AgentRunConfig
from aiops_diagnostics.gateway import gateway_db
from aiops_diagnostics.identity.scope_context import DataScope

OTHER_APP = "11111111-2222-3333-4444-555555555555"
HEADERS = {"Authorization": "Bearer log-machine", "X-AIOps-Console-Session": "fixture-session"}
PATH = "/v1/dify/management/run-logs"


@pytest.fixture
def logged(operator, monkeypatch):  # noqa: F811
    f = operator
    ctx = f["ctx"]
    ctx.settings.operator_profiles_file = "fixture-enabled"
    ctx.settings.dify_publish_api_key = "log-machine"
    for app in (APP, OTHER_APP):
        ctx.self_service.store.register(
            app_id=app,
            tenant_id="TENANT-A",
            workspace_id="workspace",
            agent_name="operator-node",
            profile_id="default",
            actor="fixture",
            entry="operator",
        )
    monkeypatch.setattr(
        ctx.operator_cpms,
        "console_identity",
        lambda session: {
            "token": "operator-private",
            "user_id": f["resolver"].current.caller.b_user_id,
            "tenant_id": f["resolver"].current.effective_tenant_id,
        },
    )
    monkeypatch.setattr(
        auth,
        "company_get",
        lambda *_: {
            "sysUser": {
                "id": f["resolver"].current.caller.b_user_id,
                "tenantId": f["resolver"].current.effective_tenant_id,
                "type": "5",
            }
        },
    )

    def issue(*, caller=None, app=APP, state="succeeded", expired=False, qa=None, binding=None):
        run = ctx.agent_run_store.issue(
            scope=caller or scope(),
            order_no="ORDER-FIXTURE-1",
            config=AgentRunConfig.load(Path(f["route"].config_file)),
            gateway_version="fixture-v1",
            publication_app_id=app,
            operation="operator_agent",
            chat_binding=binding,
        )
        ctx.agent_run_store.bind_qa(run.run_id, qa or "qa_" + run.run_id)
        with ctx.agent_run_store._connection(write=True) as connection:
            connection.execute(
                "UPDATE agent_runs SET state=?, snapshot_json=?, reason=?, completed_at=?, expires_at=? "
                "WHERE run_id=?",
                (
                    state,
                    json.dumps(
                        {
                            "answer": "PRIVATE-ANSWER",
                            "data": "PRIVATE-TOOL-DATA",
                            "evidence": {
                                "cpms_order_detail": {"data": "PRIVATE-EVIDENCE"},
                                "PRIVATE-INJECTED-KEY": {"data": "PRIVATE-X"},
                            },
                            "trial": True,
                            "history_runs": ["run_hist_1", "run_hist_2"],
                        }
                    ),
                    "fixture_terminal_reason",
                    None if state in {"issued", "running"} else datetime.now(UTC).isoformat(),
                    (datetime.now(UTC) + timedelta(days=-1 if expired else 1)).isoformat(),
                    run.run_id,
                ),
            )
        return run.run_id

    f["issue_log"] = issue
    return f


def post(f, body=None, path=PATH, **kwargs):
    return f["client"].post(path, json=body or {"app_id": APP}, headers=HEADERS, **kwargs)


def test_list_is_bounded_scoped_and_metadata_only(logged):
    f = logged
    own = {f["issue_log"](state=s, expired=True) for s in ("succeeded", "failed", "running")}
    f["issue_log"](app=OTHER_APP)
    f["issue_log"](caller=scope(user="OTHER"))
    f["issue_log"](caller=scope(tenant="TENANT-B"))
    f["issue_log"](caller=scope(site="SITE-2"))
    first = post(f, {"app_id": APP, "limit": 2})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["has_more"] is True
    second = post(f, {"app_id": APP, "limit": 2, "page": 2})
    assert second.json()["has_more"] is False
    items = first.json()["items"] + second.json()["items"]
    assert {r["run_id"] for r in items} == own
    assert {r["state"] for r in items} == {"succeeded", "failed", "expired"}
    assert all(r["result_expired"] and r["trace"] == [] and r["source"] == "java" for r in items)
    assert "PRIVATE" not in first.text + second.text
    assert set(items[0]) == {
        "run_id",
        "qa_id",
        "state",
        "created_at",
        "elapsed_ms",
        "elapsed_source",
        "result_expired",
        "delivery_mode",
        "review_reason",
        "trace",
        "source",
        "gateway_version",
        "node_build",
        "agent_version",
        "reason",
        "question",
        "order_no",
        "language",
        "environment",
        "expected_node_id",
        "actual_node_id",
        "agent_version_observed",
        "agent_version_gap",
        "claimed_at",
        "completed_at",
        "expires_at",
        "result_sha256",
        "trial",
        "history_run_count",
        "evidence_tools",
        "tool_summaries",
    }


def test_evidence_delivery_and_review_reason_are_metadata_not_private_content(logged):
    run_id = logged["issue_log"]()
    store = logged["ctx"].agent_run_store
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE agent_runs SET snapshot_json=? WHERE run_id=?",
            (
                json.dumps(
                    {
                        "delivery_mode": "evidence_only",
                        "answer": "PRIVATE",
                        "answer_review": {"reason": "unsupported_currency", "currencies": ["PRIVATE"]},
                    }
                ),
                run_id,
            ),
        )
    response = post(logged, {"app_id": APP, "run_id": run_id}, PATH + "/detail")
    assert response.json()["delivery_mode"] == "evidence_only"
    assert response.json()["review_reason"] == "unsupported_currency"
    assert "PRIVATE" not in response.text
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE agent_runs SET snapshot_json=? WHERE run_id=?",
            (json.dumps({"delivery_mode": "PRIVATE", "answer_review": {"reason": "PRIVATE"}}), run_id),
        )
    response = post(logged, {"app_id": APP, "run_id": run_id}, PATH + "/detail")
    assert response.json()["delivery_mode"] is None and response.json()["review_reason"] is None
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize(
    "state,expired",
    [
        ("succeeded", True),
        ("failed", True),
        ("issued", True),
        ("running", True),
        ("running", False),
    ],
)
def test_detail_keeps_terminal_state_expires_inflight_and_does_no_writes(logged, monkeypatch, state, expired):
    f = logged
    run = f["issue_log"](state=state, expired=expired)
    store = f["ctx"].agent_run_store
    monkeypatch.setattr(f["ctx"].operator_cpms, "authorize_readback", lambda *_: pytest.fail("业务回读"))
    before = store.peek(run)
    original_connection = gateway_db.connection
    statements, forbidden = [], []

    def read_only(action, arg1, _arg2, _database, _trigger):
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_TRANSACTION,
        }
        if action in allowed:
            return sqlite3.SQLITE_OK
        forbidden.append((action, arg1))
        return sqlite3.SQLITE_DENY

    @contextmanager
    def observed_connection(*args, **kwargs):
        with original_connection(*args, **kwargs) as connection:
            connection.set_authorizer(read_only)
            connection.set_trace_callback(statements.append)
            changes = connection.total_changes
            yield connection
            assert connection.total_changes == changes

    monkeypatch.setattr(gateway_db, "connection", observed_connection)
    result = post(f, {"app_id": APP, "run_id": run}, PATH + "/detail")
    assert result.status_code == 200
    assert result.json()["state"] == ("expired" if expired and state in {"issued", "running"} else state)
    assert result.json()["result_expired"] is expired
    assert "PRIVATE" not in result.text
    assert store.peek(run) == before
    assert any("FROM agent_runs" in sql for sql in statements)
    assert forbidden == []


def test_logs_refresh_directory_scope_even_when_resolved_identity_is_unchanged(logged, monkeypatch):
    from aiops_diagnostics.identity.query_scope import resolve_query_scope

    f = logged
    run = f["issue_log"]()
    monkeypatch.setattr(
        f["ctx"].runtime._trial_operator_scope,
        "site_scope_by_b_user_id",
        lambda *_: resolve_query_scope(scope(site="SITE-2")),
    )
    assert post(f).json()["items"] == []
    assert post(f, {"app_id": APP, "run_id": run}, PATH + "/detail").status_code == 403


@pytest.mark.parametrize("change", ["app", "user", "tenant", "sites", "roles", "company"])
def test_detail_rechecks_current_identity_and_scope(logged, monkeypatch, change):
    f = logged
    run = f["issue_log"]()
    app_id = APP
    if change == "app":
        app_id = OTHER_APP
    elif change == "user":
        f["resolver"].current = scope(user="OTHER")
    elif change == "tenant":
        f["resolver"].current = scope(tenant="TENANT-B")
    elif change == "sites":
        f["resolver"].current = replace(
            scope(), data_scope=DataScope(type="organ", site_ids=()), scope_fingerprint=""
        )
    elif change == "roles":
        f["resolver"].current = replace(scope(), roles=frozenset(), scope_fingerprint="")
    else:
        monkeypatch.setattr(
            auth, "company_get", lambda *_: {"sysUser": {"id": "OTHER", "tenantId": "TENANT-A"}}
        )
    result = post(f, {"app_id": app_id, "run_id": run}, PATH + "/detail")
    assert result.status_code == 403
    assert result.headers["cache-control"] == "no-store"
    assert run not in result.text and "PRIVATE" not in result.text


@pytest.mark.parametrize(
    "body",
    [{"page": 0}, {"page": 1001}, {"limit": 51}, {"limit": True}, {"tenant_id": "TENANT-B"}, {"page": "2"}],
)
def test_invalid_request_is_not_cached(logged, body):
    result = post(logged, {"app_id": APP, **body})
    assert result.status_code == 422
    assert result.headers["cache-control"] == "no-store"


def test_requires_json_and_machine_session(logged):
    client = logged["client"]
    assert (
        client.post(PATH, content="{}", headers={**HEADERS, "Content-Type": "text/plain"}).status_code == 415
    )
    result = client.post(PATH, json={"app_id": APP})
    assert result.status_code == 403
    assert result.headers["cache-control"] == "no-store"


def test_unknown_and_consumer_apps_are_refused(logged):
    result = post(logged, {"app_id": "00000000-0000-0000-0000-000000000000"})
    assert result.status_code == 403
    logged["ctx"].self_service.store.repair_entry(APP, tenant_id="TENANT-A", entry="consumer")
    assert post(logged).status_code == 403


@pytest.mark.parametrize("state", ["succeeded", "failed", "running"])
def test_trace_contract_keeps_actual_durations_and_prefers_last_event(logged, state):
    run = logged["ctx"].agent_run_store.peek(logged["issue_log"](state=state, expired=True))
    event = {
        "phase": "tool",
        "elapsed_ms": 75,
        "at": "2026-11-10T12:00:00Z",
        "tool": "cpms_order_detail",
        "status": "succeeded",
        "data": "PRIVATE",
        "duration_ms": 25,
        "detail": "OPERATOR_SCOPE_CHANGED",
    }
    filler = {key: value for key, value in event.items() if key != "detail"}
    projected = logs.metadata(replace(run, trace=tuple([{**filler, "elapsed_ms": 1}] * 149 + [event])))
    assert projected["elapsed_ms"] == 75
    assert projected["elapsed_source"] == "trace"
    assert projected["trace"][-1]["duration_ms"] == 25
    assert projected["trace"][-1]["detail"] == "OPERATOR_SCOPE_CHANGED"
    assert len(projected["trace"]) == 128
    assert set(projected["trace"][0]) == {"phase", "elapsed_ms", "duration_ms", "at", "tool", "status"}
    assert "PRIVATE" not in json.dumps(projected)


def test_detail_projects_question_order_terminal_code_and_scope_isolation(logged):
    f = logged
    qa = f["ctx"].store.create_assistant_question(
        scope().scope_fingerprint, "这张订单怎么还没退费", tenant_id="TENANT-A"
    )
    foreign_qa = f["ctx"].store.create_assistant_question(
        scope(site="SITE-2").scope_fingerprint, "PRIVATE-QUESTION"
    )
    own = f["issue_log"](qa=qa["qa_id"], state="failed")
    foreign = f["issue_log"](qa=foreign_qa["qa_id"], state="failed")
    detail = post(f, {"app_id": APP, "run_id": own}, PATH + "/detail").json()
    assert detail["question"] == "这张订单怎么还没退费"
    assert detail["order_no"] == "ORDER-FIXTURE-1"
    assert detail["reason"] == "fixture_terminal_reason"
    assert detail["evidence_tools"] == ["cpms_order_detail"]
    assert detail["trial"] is True
    assert detail["history_run_count"] == 2
    # 死 run 取证可注入任意 evidence 键名；未声明的键不得进日志。
    assert "PRIVATE-INJECTED-KEY" not in json.dumps(detail)
    # 别的指纹下的问题就算被错绑到本 run，也不能经这条投影漏出来。
    foreign_detail = post(f, {"app_id": APP, "run_id": foreign}, PATH + "/detail")
    assert foreign_detail.json()["question"] is None
    assert "PRIVATE" not in foreign_detail.text


def test_trial_is_derived_from_chat_binding_when_snapshot_lacks_the_flag(logged):
    f = logged
    run = f["issue_log"](binding="operator-trial:app-trial", state="failed")
    with f["ctx"].agent_run_store._connection(write=True) as connection:
        connection.execute("UPDATE agent_runs SET snapshot_json=? WHERE run_id=?", ("{}", run))
    detail = post(f, {"app_id": APP, "run_id": run}, PATH + "/detail").json()
    # 绑定是内部标识（operator-trial:<app uuid>）——只用于派生 trial，不投影。
    assert "chat_binding" not in detail
    assert detail["trial"] is True
    assert detail["history_run_count"] == 0 and detail["evidence_tools"] == []


@pytest.mark.parametrize("state", ["succeeded", "failed", "running"])
def test_persisted_run_trace_reaches_list_and_detail(logged, state):
    f = logged
    run_id = f["issue_log"](state=state, expired=True)
    store = f["ctx"].agent_run_store
    events = [
        {"phase": "dispatch", "status": "running", "at": "2026-11-10T00:00:00Z", "elapsed_ms": 0},
        {
            "phase": "tool",
            "tool": "cpms_order_detail",
            "status": "refused",
            "at": "2026-11-10T00:00:01Z",
            "elapsed_ms": 1000,
            "duration_ms": 750,
            "detail": "OPERATOR_SCOPE_CHANGED" + "X" * 64,
        },
    ]
    for event in events:
        store.record_trace(run_id, event)
    # detail 是机器码字段：store 层截断兜底，不接受超长写入。
    events[1]["detail"] = events[1]["detail"][:64]
    assert store.peek(run_id).trace == tuple(events)
    listing = post(f)
    detail = post(f, {"app_id": APP, "run_id": run_id}, PATH + "/detail")
    assert listing.status_code == detail.status_code == 200
    assert listing.json()["items"] == [detail.json()]
    assert detail.json()["trace"] == events
    assert detail.json()["elapsed_ms"] == 1000
    assert detail.json()["state"] == ("expired" if state == "running" else state)
    assert detail.json()["result_expired"] is True
    assert "PRIVATE" not in detail.text


def test_detail_projects_tool_summaries_and_keeps_them_scope_bounded(logged):
    """ADR-0251：行级摘要投影到 detail，仍走同一 scope 闸。"""
    f = logged
    run_id = f["issue_log"](state="succeeded")
    foreign = f["issue_log"](caller=scope(user="OTHER"), state="succeeded")
    f["ctx"].agent_run_store.record_tool_summary(
        run_id,
        "cpms_order_detail",
        {"入参": "order_no=ORDER-FIXTURE-1", "状态": "已支付", "应付": "8.00"},
    )
    f["ctx"].agent_run_store.record_tool_summary(foreign, "cpms_order_detail", {"状态": "PRIVATE-AMOUNT"})
    detail = post(f, {"app_id": APP, "run_id": run_id}, PATH + "/detail").json()
    assert detail["tool_summaries"] == {
        "cpms_order_detail": {"入参": "order_no=ORDER-FIXTURE-1", "状态": "已支付", "应付": "8.00"}
    }
    assert "PRIVATE" not in json.dumps(detail)
    # 无摘要的 run 不给空对象冒充有内容。
    empty = f["issue_log"](state="succeeded")
    assert post(f, {"app_id": APP, "run_id": empty}, PATH + "/detail").json()["tool_summaries"] is None
