from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from aiops_diagnostics import gateway_api
from aiops_diagnostics.consumer.consumer_order_chat import is_order_status_question
from aiops_diagnostics.data.sources import MySQLSource
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.identity.caller_auth import CallerAuthError
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

ORDER = "2096164064667852801"
OTHER = "2096164064667852802"
HEADERS = {"Authorization": "Bearer verified", "X-Business-Entry": "consumer"}


@pytest.fixture
def provision(tmp_path, monkeypatch):
    state = SimpleNamespace(
        calls=[],
        auth_calls=[],
        user="C-1",
        allowed={ORDER},
        rows=[{"order_no": ORDER}],
        status="1",
        scopes_requested=[],
        wrong_order=False,
        auth_down=False,
        revoke_after=False,
        scopes=[],
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.calls.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            body = json.dumps(
                {
                    "code": 0,
                    "data": {
                        "orderNo": OTHER if state.wrong_order else ORDER,
                        "status": state.status,
                        "plateNo": "PRIVATE-PLATE",
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if state.revoke_after:
                state.allowed.clear()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Resolver:
        def resolve(self, token, *, required_scope, **kwargs):
            if token != "verified":
                raise CallerAuthError("invalid", code="caller_auth.invalid")
            state.auth_calls.append(required_scope)
            if required_scope in state.scopes_requested:
                raise CallerAuthError("scope refused", code="caller_auth.forbidden")
            subject = SubjectRecord(b_user_id="b-1", c_user_id=state.user, tenant_id="tenant-a")
            scope = ScopeContext.build(
                caller=subject,
                subject=subject,
                delegated=False,
                effective_tenant_id="tenant-a",
                data_scope=DataScope(type="self"),
                roles=frozenset(),
                permissions=frozenset({required_scope}),
            )
            if getattr(state, "identity_changes", False) and required_scope == "aiops:orders:read":
                subject = SubjectRecord(b_user_id="other", c_user_id="other", tenant_id="tenant-a")
                scope = ScopeContext.build(
                    caller=subject,
                    subject=subject,
                    delegated=False,
                    effective_tenant_id="tenant-a",
                    data_scope=DataScope(type="self"),
                    roles=frozenset(),
                    permissions=frozenset({required_scope}),
                )
            return state.override if hasattr(state, "override") else scope

    class Directory:
        def roles_for_c_user(self, c_user_id, tenant_id):
            return (PlatformRoleRecord("b-1", state.user, "tenant-a", "admin"),)

        def roles_for_b_user(self, b_user_id, tenant_id):
            return ()

    class Authorizer:
        def can_access(self, caller, order_no):
            if state.auth_down:
                raise CallerAuthError("down", code="caller_auth.unavailable")
            return order_no in state.allowed

    class Runtime:
        def shutdown(self):
            pass

        def get_assistant_qa(self, context, qa_id):
            return state.store.get_assistant_question(qa_id, context.scope_fingerprint)

        def list_assistant_qa(self, context, *, limit=50):
            return state.store.list_assistant_questions(context.scope_fingerprint, limit=limit)

        def cancel_assistant_qa(self, context, qa_id):
            return self.get_assistant_qa(context, qa_id)

        def start_standard_diagnosis(self, *_args, **_kwargs):
            raise AssertionError("status query must not diagnose")

        def classify_lightweight(self, *_args, **_kwargs):
            raise AssertionError("status query must not invoke model routing")

    @contextmanager
    def mysql(settings, scope):
        state.scopes.append(scope)
        yield SimpleNamespace(list_orders=lambda **kwargs: state.rows)

    monkeypatch.setattr(gateway_api, "scoped_mysql_source", mysql)
    config = tmp_path / "production.env"
    config.write_text("# synthetic only\n")
    config.chmod(0o600)
    state.settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
        consumer_order_chat_enabled=True,
        execution_environment="test",
        charging_status_base_url=f"http://127.0.0.1:{server.server_port}",
    )
    state.store = GatewayStore(state.settings.database_file)
    state.app = gateway_api.create_gateway_app(
        settings=state.settings,
        store=state.store,
        runtime=Runtime(),
        caller_resolver=Resolver(),
        order_authorizer=Authorizer(),
        platform_resolver=PlatformIdentityResolver(Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def query(client, **body):
    return client.post(
        "/v1/assistant/questions", headers=HEADERS, json={"question": "查询我最新一单的状态", **body}
    )


def test_latest_and_explicit_answers_are_bound_to_private_evidence(provision):
    p = provision
    with TestClient(p.app) as client:
        response = query(client)
        assert response.status_code == 202, response.json()
        assert response.headers["Cache-Control"] == "no-store"
        body = response.json()
        assert body["type"] == "qa" and body["status"] == "completed"
        text = body["result"]["blocks"][0]["text"]
        assert ORDER in text and "充电结束" in text and "cpms.chargingInfo" in text
        assert "PRIVATE-PLATE" not in text
        assert p.auth_calls[:2] == ["aiops:diagnoses:write", "aiops:orders:read"]
        assert p.scopes[0].tenant_id == "tenant-a" and p.scopes[0].user_id == "C-1"
        path = p.settings.data_home / "consumer-order-chat" / (body["qa_id"] + ".json")
        evidence = json.loads(path.read_text())
        assert (
            evidence["result_sha256"]
            == hashlib.sha256(json.dumps(evidence["snapshot"], sort_keys=True).encode()).hexdigest()
        )
        assert (
            evidence["answer_sha256"]
            == hashlib.sha256(json.dumps(body["result"], sort_keys=True).encode()).hexdigest()
        )
        assert path.stat().st_mode & 0o777 == 0o600
        poll = client.get("/v1/assistant/questions/" + body["qa_id"], headers=HEADERS)
        assert poll.json()["result"] == body["result"]
        assert poll.headers["Cache-Control"] == "no-store"
        assert p.calls[0][1]["tenant-id"] == "tenant-a" and p.calls[0][1]["user-id"] == "C-1"
        p.scopes.clear()
        assert query(client, question="查询这个订单的状态", order_no=ORDER).status_code == 202
        assert p.scopes == [] and len(p.calls) == 2


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("empty", 404),
        ("other", 404),
        ("down", 503),
        ("wrong_order", 502),
        ("revoked", 404),
        ("evidence", 503),
    ],
)
def test_failures_do_not_deliver_status(provision, monkeypatch, failure, expected):
    p = provision
    if failure == "empty":
        p.rows = []
    if failure == "other":
        p.allowed.clear()
    if failure == "down":
        p.auth_down = True
    if failure == "wrong_order":
        p.wrong_order = True
    if failure == "revoked":
        p.revoke_after = True
    if failure == "evidence":

        def refuse(*args):
            raise OSError("private content must not leak")

        monkeypatch.setattr(gateway_api, "write_private_text", refuse)
    with TestClient(p.app) as client:
        response = query(client)
    assert response.status_code == expected
    assert response.headers["Cache-Control"] == "no-store"
    assert ORDER not in response.text and "private content" not in response.text
    if failure in {"empty", "other", "down"}:
        assert not p.calls


@pytest.mark.parametrize(
    "scope_type,delegated,user",
    [("all", False, "C-1"), ("organ", False, "C-1"), ("self", True, "C-1"), ("self", False, None)],
)
def test_non_consumer_identity_cannot_read(provision, scope_type, delegated, user):
    subject = SubjectRecord(b_user_id="b-1", c_user_id=user, tenant_id="tenant-a")
    provision.override = ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=delegated,
        effective_tenant_id="tenant-a",
        data_scope=DataScope(type=scope_type),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )
    with TestClient(provision.app) as client:
        response = query(client)
    assert response.status_code == 403 and not provision.calls and not provision.scopes


def test_unknown_status_and_history_recheck(provision):
    p = provision
    p.status = "999"
    with TestClient(p.app) as client:
        response = query(client).json()
        assert "status_role=unknown" in response["result"]["blocks"][0]["text"]
        p.allowed.clear()
        assert client.get("/v1/assistant/questions/" + response["qa_id"], headers=HEADERS).status_code == 404
        assert client.get("/v1/assistant/questions", headers=HEADERS).status_code == 200
        assert (
            client.post(
                "/v1/assistant/questions/" + response["qa_id"] + "/cancel", headers=HEADERS
            ).status_code
            == 404
        )


def test_conversation_keeps_bound_answer_but_excludes_it_from_model_context(provision):
    from aiops_diagnostics.conversation_store import ConversationStore

    p = provision
    with TestClient(p.app) as client:
        created = client.post(
            "/v1/conversations",
            headers=HEADERS,
            json={"agent_version_key": "agt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa#v1"},
        )
        assert created.status_code == 201, created.json()
        cid = created.json()["conversation_id"]
        body = query(client, conversation_id=cid).json()
        detail = client.get("/v1/conversations/" + cid, headers=HEADERS)
        assert detail.status_code == 200 and not detail.json()["is_generating"]
        assert detail.json()["turns"][0]["answer"] == body["result"]
        store = ConversationStore(p.settings.database_file)
        scope = json.loads(
            (p.settings.data_home / "consumer-order-chat" / (body["qa_id"] + ".json")).read_text()
        )["scope_fingerprint"]
        assert store.context_turns(cid, scope) == []
        p.allowed.clear()
        assert client.get("/v1/conversations/" + cid, headers=HEADERS).status_code == 404


def test_scope_permission_missing_and_cross_user_read_refused(provision):
    p = provision
    with TestClient(p.app) as client:
        p.scopes_requested = ["aiops:orders:read"]
        assert query(client).status_code == 403 and p.calls == []
        p.scopes_requested = []
        body = query(client).json()
        p.user = "C-2"
        assert client.get("/v1/assistant/questions/" + body["qa_id"], headers=HEADERS).status_code == 404


@pytest.mark.parametrize("corruption", ["missing", "snapshot", "answer"])
def test_corrupted_evidence_cannot_be_read(provision, corruption):
    p = provision
    with TestClient(p.app) as client:
        body = query(client).json()
        path = p.settings.data_home / "consumer-order-chat" / (body["qa_id"] + ".json")
        evidence = json.loads(path.read_text())
        if corruption == "missing":
            path.unlink()
        else:
            if corruption == "snapshot":
                evidence["snapshot"]["status_code"] = "999"
            else:
                evidence["answer_sha256"] = "0" * 64
            path.write_text(json.dumps(evidence))
        assert client.get("/v1/assistant/questions/" + body["qa_id"], headers=HEADERS).status_code == 503


def test_missing_order_without_latest_requests_clarification(provision):
    with TestClient(provision.app) as client:
        response = query(client, question="查询我的订单状态")
        assert response.json()["type"] == "clarification" and provision.calls == []


def test_disabled_path_refuses_without_python_fallback(provision):
    p = provision
    p.settings.consumer_order_chat_enabled = False
    with TestClient(p.app) as client:
        response = query(client, order_no=ORDER)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHAT_ROUTE_UNAVAILABLE"
    assert not p.calls and not p.scopes


def test_chat_toggle_uses_private_config_and_requires_personal_endpoint(provision, monkeypatch):
    p = provision
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(p.settings.server_config_file))
    monkeypatch.delenv("AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED", raising=False)
    p.settings.server_config_file.write_text("AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED=true\n")
    assert GatewayServerSettings.from_env().consumer_order_chat_enabled
    monkeypatch.setenv("AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED", "false")
    assert not GatewayServerSettings.from_env().consumer_order_chat_enabled
    p.settings.execution_environment = ""
    with pytest.raises(ValueError, match="logical execution environment"):
        p.settings.validate()
    p.settings.execution_environment = "test"
    p.settings.charging_status_base_url = ""
    with pytest.raises(ValueError, match="charging status endpoint"):
        p.settings.validate()


def test_evidence_write_cannot_bypass_final_authorization(provision, monkeypatch):
    p = provision
    write = gateway_api.write_private_text

    def revoke(*args):
        write(*args)
        p.allowed.clear()

    monkeypatch.setattr(gateway_api, "write_private_text", revoke)
    with TestClient(p.app) as client:
        assert query(client).status_code == 404
    with p.store._connection() as db:
        row = db.execute("SELECT status,result_json FROM assistant_questions").fetchone()
    assert row["status"] == "failed" and row["result_json"] is None


def test_new_turn_column_migrates_without_losing_existing_history(provision):
    import sqlite3

    from aiops_diagnostics.conversation_store import ConversationStore

    p = provision
    with sqlite3.connect(p.settings.database_file) as db:
        db.execute("ALTER TABLE conversation_turns DROP COLUMN status_qa_id")
    store = ConversationStore(p.settings.database_file)
    scope = "test-scope"
    conv = store.create(
        scope_fingerprint=scope, business_entry="consumer", agent_version_key="agt_aaaaaaaa#v1"
    )
    turn = store.begin_turn(conv["conversation_id"], scope, kind="qa", question="你好")
    answer = {"blocks": [{"kind": "text", "text": "你好"}], "retrieval_status": "not_found"}
    store.complete_turn(conv["conversation_id"], scope, turn, answer=answer)
    again = ConversationStore(p.settings.database_file)
    assert again.context_turns(conv["conversation_id"], scope)[0]["answer"] == answer


def test_latest_sql_filters_regular_and_work_order_owners_before_sort(monkeypatch):
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.identity.query_scope import QueryScope

    calls = []

    class Cursor:
        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchall(self):
            return []

    @contextmanager
    def cursor():
        yield Cursor()

    source = MySQLSource(Settings(), QueryScope(tenant_id="tenant-a", site_ids=None, user_id="C-1"))
    monkeypatch.setattr(source, "_cursor", cursor)
    source.list_orders(page=1, page_size=1)
    sql, params = calls[0]
    assert "tenant_id=%s" in sql and "CASE WHEN user_id=%s THEN consumer_id ELSE user_id END=%s" in sql
    assert "ORDER BY created_time DESC, id DESC" in sql and params == ["tenant-a", "-1", "C-1", 2, 0]


@pytest.mark.parametrize(
    "question",
    [
        "为什么最新订单异常结束？",
        "最新订单扣费原因",
        "诊断订单状态",
        "你好",
        "取消最新订单",
        "最新订单支付政策",
        "delete my latest order",
        "latest order fees",
    ],
)
def test_status_route_does_not_claim_diagnosis_or_generic_questions(question):
    assert not is_order_status_question(question)


def test_latest_selects_only_owned_regular_or_claimed_orders_in_sql(monkeypatch):
    import sqlite3

    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.identity.query_scope import QueryScope

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE ch_order_info(id INT,order_no TEXT,status TEXT,site_id TEXT,"
        "created_time INT,tenant_id TEXT,user_id TEXT,consumer_id TEXT)"
    )
    db.executemany(
        "INSERT INTO ch_order_info VALUES(?,?,?,?,?,?,?,?)",
        [
            (1, ORDER, "1", "site", 1, "tenant-a", "C-1", None),
            (2, OTHER, "1", "site", 2, "tenant-a", "-1", "C-1"),
            (3, "2096164064667852803", "1", "site", 3, "tenant-a", "C-2", None),
            (4, "2096164064667852804", "1", "site", 4, "tenant-b", "C-1", None),
        ],
    )

    class Cursor:
        def execute(self, sql, params):
            self.cursor = db.execute(sql.replace("`cloud_charging_pile`.", "").replace("%s", "?"), params)

        def fetchall(self):
            return [dict(r) for r in self.cursor.fetchall()]

    @contextmanager
    def cursor(self):
        yield Cursor()

    monkeypatch.setattr(MySQLSource, "_cursor", cursor)
    source = MySQLSource(Settings(), QueryScope(tenant_id="tenant-a", site_ids=None, user_id="C-1"))
    try:
        assert source.list_orders(page=1, page_size=1)[0]["order_no"] == OTHER
        db.execute("DELETE FROM ch_order_info WHERE order_no=?", (OTHER,))
        assert source.list_orders(page=1, page_size=1)[0]["order_no"] == ORDER
    finally:
        db.close()


def test_real_http_status_chat_and_missing_auth(provision):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(provision.app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            with httpx.Client(base_url=base) as client:
                result = query(client)
                assert result.status_code == 202 and result.json()["status"] == "completed"
                assert (
                    client.post("/v1/assistant/questions", json={"question": "我最新订单状态"}).status_code
                    == 401
                )
                assert len(provision.calls) == 1
                provision.allowed.clear()
                refused = query(client, order_no=ORDER)
                assert refused.status_code == 404 and len(provision.calls) == 1
                assert refused.headers["Cache-Control"] == "no-store"
                provision.allowed.add(ORDER)
                provision.wrong_order = True
                assert query(client).status_code == 502
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_failed_result_write_releases_conversation_without_answer(provision, monkeypatch):
    p = provision
    update = p.store.update_assistant_question
    monkeypatch.setattr(
        p.store,
        "update_assistant_question",
        lambda *a, **kw: False if kw["status"] == "completed" else update(*a, **kw),
    )
    with TestClient(p.app) as client:
        cid = client.post(
            "/v1/conversations", headers=HEADERS, json={"agent_version_key": "agt_aaaaaaaa#v1"}
        ).json()["conversation_id"]
        response = query(client, conversation_id=cid)
        assert response.status_code == 503
        detail = client.get("/v1/conversations/" + cid, headers=HEADERS).json()
        assert not detail["is_generating"] and detail["turns"] == []


@pytest.mark.parametrize("failure", ["identity", "source", "record"])
def test_independent_identity_and_persistence_failures(provision, monkeypatch, failure):
    from aiops_diagnostics.data.sources import SourceError

    p = provision
    if failure == "identity":
        p.identity_changes = True
    elif failure == "source":

        @contextmanager
        def unavailable(*args):
            raise SourceError("private upstream unavailable")
            yield

        monkeypatch.setattr(gateway_api, "scoped_mysql_source", unavailable)
    else:

        def unavailable(*args, **kwargs):
            raise OSError("private database unavailable")

        monkeypatch.setattr(p.store, "create_assistant_question", unavailable)
    with TestClient(p.app) as client:
        response = query(client)
        assert response.status_code == (403 if failure == "identity" else 503)
        assert "private" not in response.text
        if failure != "record":
            assert not p.calls


@pytest.mark.parametrize("language", ["zh-Hant", "en"])
def test_supported_languages_and_history_disable(provision, language):
    with TestClient(provision.app) as client:
        response = client.post(
            "/v1/assistant/questions",
            json={"question": "check my latest order status"},
            headers={**HEADERS, "Accept-Language": language},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["language"] == language and ORDER in body["result"]["blocks"][0]["text"]
        provision.settings.consumer_order_chat_enabled = False
        assert client.get("/v1/assistant/questions/" + body["qa_id"], headers=HEADERS).status_code == 404
