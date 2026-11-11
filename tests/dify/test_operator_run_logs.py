"""LOG-01：真实临时账本/HTTP，合成公司与 Dify 身份；不调用业务结果回读。"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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

    def issue(*, caller=None, app=APP, state="succeeded", expired=False):
        run = ctx.agent_run_store.issue(
            scope=caller or scope(),
            order_no="PRIVATE-ORDER",
            config=AgentRunConfig.load(Path(f["route"].config_file)),
            gateway_version="fixture-v1",
            publication_app_id=app,
            operation="operator_agent",
        )
        ctx.agent_run_store.bind_qa(run.run_id, "qa_" + run.run_id)
        with ctx.agent_run_store._connection(write=True) as connection:
            connection.execute(
                "UPDATE agent_runs SET state=?, snapshot_json=?, reason=?, completed_at=?, expires_at=? "
                "WHERE run_id=?",
                (
                    state,
                    json.dumps({"answer": "PRIVATE-ANSWER", "data": "PRIVATE-TOOL-DATA"}),
                    "PRIVATE-FAILURE",
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
        "trace",
        "source",
        "gateway_version",
        "node_build",
        "agent_version",
    }


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
    }
    projected = logs.metadata(
        SimpleNamespace(
            **{**asdict(run), "trace": tuple([{**event, "elapsed_ms": 1}] * 149 + [event])},
            exec_config_confirmed=False,
        )
    )
    assert projected["elapsed_ms"] == 75
    assert projected["elapsed_source"] == "trace"
    assert projected["trace"][-1]["duration_ms"] == 25
    assert len(projected["trace"]) == 128
    assert set(projected["trace"][0]) == {"phase", "elapsed_ms", "duration_ms", "at", "tool", "status"}
    assert "PRIVATE" not in json.dumps(projected)


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
            "status": "succeeded",
            "at": "2026-11-10T00:00:01Z",
            "elapsed_ms": 1000,
            "duration_ms": 750,
        },
    ]
    for event in events:
        store.record_trace(run_id, event)
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
