"""真实 API 路径上的可归因观察：成功、各类拒绝、异常、并发。

这个夹具是**本文件自己的**，不共享 `test_consumer_order_chat.py` 的夹具 —— 那边是
既有回归基线，本片不修改它（不删断言、不加 skip、不降标准）。这里需要的额外能力
（强制某个分支拒绝、制造未预期异常、改配置后重建 app）都只在本文件实现。

判据全部走真实 API：`TestClient` 覆盖应用内部路径，最后一个用例再起真实 uvicorn
经 TCP 驱动，断言的是**实际写出的日志行**。没有任何一处重算被测逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
from aiops_diagnostics import request_observation as ro
from aiops_diagnostics.caller_auth import CallerAuthError
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

ORDER = "2096164064667852801"
HEADERS = {"Authorization": "Bearer verified", "X-Business-Entry": "consumer"}
QUESTION = "查询我最新一单的状态"

LINE = re.compile(
    r"^assistant_question_result "
    r"request_id=(?P<request_id>cor_[0-9a-f]{12}) "
    r"route=consumer_order_chat "
    r"stage=(?P<stage>[a-z_]+) "
    r"outcome=(?P<outcome>[a-z]+) "
    r"http_status=(?P<status>\d+) "
    r"error_code=(?P<code>[A-Z_\-]+) "
    r"tenant_hash=(?P<tenant>[\-0-9a-f]+) "
    r"qa_id=(?P<qa_id>(?:qa_[0-9a-f]{32})|-) "
    r"duration_ms=(?P<duration>\d+)$"
)


class Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def lines():
    handler = Recorder()
    logger = logging.getLogger("aiops.assistant_question")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def parsed(messages: list[str]) -> list[dict[str, str]]:
    out = []
    for message in messages:
        match = LINE.match(message)
        assert match, f"unexpected line shape: {message!r}"
        out.append(match.groupdict())
    return out


def only(messages: list[str]) -> dict[str, str]:
    records = parsed(messages)
    assert len(records) == 1, f"expected exactly one result line, got {records}"
    return records[0]


@pytest.fixture
def provision(tmp_path, monkeypatch):
    state = SimpleNamespace(
        calls=[],
        auth_calls=[],
        status="1",
        wrong_order=False,
        refused_scopes=set(),
        expired_on_recheck=False,
        auth_down=False,
        list_orders_sleep=0.0,
        cpms_sleep=0.0,
        unexpected=None,
        fail_evidence_write=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if state.cpms_sleep:
                time.sleep(state.cpms_sleep)
            state.calls.append(self.path)
            body = json.dumps(
                {
                    "code": 0,
                    "data": {
                        "orderNo": "2096164064667852999" if state.wrong_order else ORDER,
                        "status": state.status,
                        "plateNo": "PRIVATE-PLATE",
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Resolver:
        def resolve(self, token, *, required_scope, **_kwargs):
            if token != "verified":
                raise CallerAuthError("invalid", code="caller_auth.invalid")
            state.auth_calls.append(required_scope)
            if required_scope in state.refused_scopes:
                raise CallerAuthError("scope refused", code="caller_auth.forbidden")
            if state.expired_on_recheck and required_scope == "aiops:orders:read":
                # The real production shape: the first resolution
                # (aiops:diagnoses:write) succeeds and only the second one,
                # inside authenticated_chat_order_reader, is rejected.
                raise CallerAuthError("token no longer valid", code="caller_auth.invalid")
            subject = SubjectRecord(b_user_id="b-1", c_user_id="C-1", tenant_id="tenant-a")
            return ScopeContext.build(
                caller=subject,
                subject=subject,
                delegated=False,
                effective_tenant_id="tenant-a",
                data_scope=DataScope(type="self"),
                roles=frozenset(),
                permissions=frozenset({required_scope}),
            )

    class Directory:
        def roles_for_c_user(self, c_user_id, tenant_id):
            return (PlatformRoleRecord("b-1", "C-1", "tenant-a", "admin"),)

        def roles_for_b_user(self, b_user_id, tenant_id):
            return ()

    class Authorizer:
        def can_access(self, caller, order_no):
            if state.auth_down:
                raise CallerAuthError("down", code="caller_auth.unavailable")
            return order_no == ORDER

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
            if state.unexpected is not None:
                raise state.unexpected
            raise AssertionError("status query must not diagnose")

        def start_assistant_qa(self, *_args, **_kwargs):
            # Reachable only on the "feature disabled" path, where the request is
            # ordinary QA. This module's job there is to emit NOTHING; the job
            # itself is out of scope for this test.
            return {
                "qa_id": "qa_" + "0" * 32,
                "status": "queued",
                "question": QUESTION,
                "result": None,
                "error_code": None,
            }

        def classify_lightweight(self, *_args, **_kwargs):
            # None means "no routing decision", which the handler already treats
            # as a normal case. These tests never assert on the ordinary-QA
            # answer; they only assert that it is NOT counted as a personal
            # status query.
            return None

    @contextmanager
    def mysql(_settings, _scope):
        if state.list_orders_sleep:
            time.sleep(state.list_orders_sleep)
        if state.unexpected is not None:
            raise state.unexpected
        yield SimpleNamespace(list_orders=lambda **_kwargs: [{"order_no": ORDER}])

    monkeypatch.setattr(gateway_api, "scoped_mysql_source", mysql)
    # Read at call time: the test body flips the switch after the fixture ran.
    real_write = gateway_api.write_private_text

    def maybe_failing_write(*args, **kwargs):
        if state.fail_evidence_write:
            raise OSError("synthetic evidence write failure")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(gateway_api, "write_private_text", maybe_failing_write)

    config = tmp_path / "production.env"
    config.write_text("# synthetic only\n")
    config.chmod(0o600)
    state.tmp = tmp_path
    state.config = config
    state.store = GatewayStore(tmp_path / "gateway.db")

    def build(*, enabled: bool = True, endpoint: str | None = None) -> None:
        state.settings = GatewayServerSettings(
            data_home=tmp_path,
            database_file=tmp_path / "gateway.db",
            server_config_file=config,
            consumer_order_chat_enabled=enabled,
            execution_environment="test",
            charging_status_base_url=endpoint or f"http://127.0.0.1:{server.server_port}",
        )
        state.app = gateway_api.create_gateway_app(
            settings=state.settings,
            store=state.store,
            runtime=Runtime(),
            caller_resolver=Resolver(),
            order_authorizer=Authorizer(),
            platform_resolver=PlatformIdentityResolver(Directory()),
            faq_catalog=FAQCatalog.bundled(),
        )

    build()
    state.rebuild = build
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(client: httpx.Client | TestClient, question: str = QUESTION, **body):
    return client.post("/v1/assistant/questions", headers=HEADERS, json={"question": question, **body})


# ---------------------------------------------------------------- success


def test_success_line_carries_qa_id_status_and_duration(provision, lines):
    with TestClient(provision.app) as client:
        response = post(client)
        assert response.status_code == 202
        body = response.json()

    record = only(lines)
    assert record["outcome"] == "completed"
    assert record["status"] == "202"
    assert record["code"] == "-"
    assert record["stage"] == ro.STAGE_ORDER_CHAT_EVIDENCE
    assert record["qa_id"] == body["qa_id"]
    assert record["tenant"] == ro.tenant_hash("tenant-a")
    assert int(record["duration"]) >= 0


def test_duration_covers_work_that_happens_before_the_qa_row_exists(provision, lines):
    """计时起点必须在 SQL/CPMS **之前**。

    让 SQL 与 CPMS 各慢 120 ms：从 QA 落库起算会读到接近 0，真实实现必须 >= 240。
    """
    provision.list_orders_sleep = 0.12
    provision.cpms_sleep = 0.12
    with TestClient(provision.app) as client:
        assert post(client).status_code == 202

    record = only(lines)
    assert int(record["duration"]) >= 240, record


# ---------------------------------------------------------------- refusals


def test_missing_order_read_scope_is_refused_and_recorded(provision, lines):
    provision.refused_scopes.add("aiops:orders:read")
    with TestClient(provision.app) as client:
        response = post(client)

    assert response.status_code == 403
    record = only(lines)
    assert record["outcome"] == "refused"
    assert record["status"] == "403"
    assert record["code"] == "INSUFFICIENT_SCOPE"
    assert record["qa_id"] == "-"


def test_unowned_order_records_the_refusal_and_creates_no_qa(provision, lines):
    provision.wrong_order = False
    directory = provision.tmp / "consumer-order-chat"

    class Refuse(SimpleNamespace):
        pass

    def deny(_caller, _order_no):
        return False

    provision.app.state.gateway.order_authorizer.can_access = deny  # type: ignore[union-attr]
    with TestClient(provision.app) as client:
        response = post(client)

    assert response.status_code == 404
    record = only(lines)
    assert record["outcome"] == "refused"
    assert record["code"] == "ORDER_NOT_FOUND"
    assert record["qa_id"] == "-"
    assert not directory.exists() or not list(directory.glob("qa_*.json"))


def test_unregistered_tenant_is_distinguishable_from_an_unowned_order(provision, lines):
    provision.rebuild(endpoint="other-tenant:http://127.0.0.1:1")
    with TestClient(provision.app) as client:
        response = post(client)

    assert response.status_code == 404
    record = only(lines)
    assert record["code"] == "TENANT_ENDPOINT_UNRESOLVED"
    assert record["stage"] == ro.STAGE_ORDER_CHAT_CPMS


def test_revoked_credential_on_re_authentication_is_a_refusal_not_a_failure(provision, lines):
    """首次身份的解析成功，`orders:read` 再次解析时凭据无效 ⇒ 401，必须算拒绝。

    这是生产可达的形状（`authenticated_chat_order_reader` 里的第二次解析），
    不是构造出来的边界。它一度被记成 `failed`，于是"凭据过期"会抬高服务 5xx 率 ——
    而那是运维用来判断"网关坏了"的那个数。
    """
    provision.expired_on_recheck = True
    with TestClient(provision.app) as client:
        response = post(client)

    assert response.status_code == 401
    record = only(lines)
    assert record["outcome"] == "refused", record
    assert record["status"] == "401"
    assert record["code"] == "INVALID_ACCESS_TOKEN"
    # 不在 5xx 分子里：那正是这条 bug 的危害
    assert int(record["status"]) < 500
    assert provision.auth_calls[:2] == ["aiops:diagnoses:write", "aiops:orders:read"]


def test_every_4xx_is_a_refusal_and_every_5xx_is_a_failure(provision, lines):
    """契约是整个 4xx/5xx 区间，不是一张要人记住去补的清单。

    第一版把拒绝写成了枚举 `{400,403,404,409,422}` 并漏掉 401；枚举正是错因，
    所以这里钉边界（4xx 含 401/429；5xx 不是拒绝），而不是钉某个集合的成员。
    """
    for status in (400, 401, 403, 404, 409, 422, 429, 499):
        assert ro._outcome(status, "") == ro.OUTCOME_REFUSED, status
    for status in (500, 502, 503):
        assert ro._outcome(status, "") == ro.OUTCOME_FAILED, status


def test_outcome_is_exactly_one_of_three_values(provision, lines):
    """只有三类结果。手册上一版写了四类，多的那类是我想象出来的。"""
    assert {ro.OUTCOME_COMPLETED, ro.OUTCOME_REFUSED, ro.OUTCOME_FAILED} == {
        ro._outcome(202, ""),
        ro._outcome(403, ""),
        ro._outcome(500, ""),
    }


def test_evidence_failure_keeps_the_real_qa_id(provision, lines):
    """证据落盘失败 503 时 QA 行已经建出来 —— 保留那个关联，别为了旧文案删掉它。

    Codex 复核指出原手册写"qa_id 只在成功时有值"，与实现不符：实现早就把
    `mark_qa` 放在建行之后、写盘之前。这里把那条事实钉住。
    """
    provision.fail_evidence_write = True
    with TestClient(provision.app) as client:
        response = post(client)

    assert response.status_code == 503
    record = only(lines)
    assert record["outcome"] == "failed"
    assert record["code"] == "CHARGING_STATUS_EVIDENCE_UNAVAILABLE"
    assert record["qa_id"].startswith("qa_"), "503 时已有真实 QA，关联必须保留"
    assert record["stage"] == ro.STAGE_ORDER_CHAT_EVIDENCE


def test_a_clarification_is_a_200_without_an_order(provision, lines):
    """候选也可能以 200 clarification 结束且 qa_id=-：HTTP 成功 ≠ 查到了订单。"""
    provision.rebuild(enabled=True)
    with TestClient(provision.app) as client:
        # 命中状态问法但没有"最新"，也没有订单号 ⇒ 分支内要求补订单号
        response = post(client, "查询这个订单的状态")

    assert response.status_code == 200
    record = only(lines)
    assert record["outcome"] == "completed"
    assert record["qa_id"] == "-", "clarification 没有 QA，不能记成已查到订单"


def test_cpms_failure_is_a_failure_not_a_refusal(provision, lines):
    provision.wrong_order = True
    with TestClient(provision.app) as client:
        response = post(client)

    assert response.status_code == 502
    record = only(lines)
    assert record["outcome"] == "failed"
    assert record["code"] == "CHARGING_STATUS_UNAVAILABLE"


def test_explicit_order_refused_before_the_branch_is_still_attributed(provision, lines):
    """显式 order_no 在进入 `_consumer_order_chat` 前就被拒，也必须有行。"""
    stranger = "2096164064667852802"
    with TestClient(provision.app) as client:
        response = post(client, f"查询订单 {stranger} 的状态", order_no=stranger)

    assert response.status_code == 404
    record = only(lines)
    assert record["code"] == "ORDER_NOT_FOUND"
    assert record["stage"] == ro.STAGE_ORDER_CHAT_AUTH


# ---------------------------------------------------------------- exclusions


def test_unauthenticated_request_is_never_counted(provision, lines):
    """身份没建立 ⇒ 不记录；租户不可能来自客户端自报的头。"""
    with TestClient(provision.app) as client:
        response = client.post(
            "/v1/assistant/questions",
            headers={"X-Business-Entry": "consumer", "X-Tenant-Id": "client-claimed"},
            json={"question": QUESTION},
        )

    assert response.status_code == 401
    assert lines == []


def test_ordinary_question_is_not_mislabelled(provision, lines):
    with TestClient(provision.app) as client:
        post(client, "充电桩怎么用")
    assert lines == []


def test_feature_disabled_never_claims_the_branch(provision, lines):
    provision.rebuild(enabled=False)
    with TestClient(provision.app) as client:
        response = post(client)
    assert response.status_code == 409
    assert lines == []


# ---------------------------------------------------------------- shape


def test_line_carries_no_question_answer_order_or_credential(provision, lines):
    with TestClient(provision.app) as client:
        body = post(client).json()
    line = lines[0]
    # qa_id is the join key and is SUPPOSED to be there; everything that
    # identifies the person, the order or the content must not be.
    assert body["qa_id"] in line
    for forbidden in (QUESTION, "PRIVATE-PLATE", ORDER, "tenant-a", "verified"):
        assert forbidden not in line, forbidden


# ---------------------------------------------------------------- failures


def test_unexpected_exception_is_recorded_and_the_response_is_unchanged(provision, lines):
    provision.unexpected = RuntimeError("upstream exploded: /v1/orders/2096164064667852801")

    with TestClient(provision.app, raise_server_exceptions=False) as client:
        response = post(client)

    assert response.status_code == 500
    record = only(lines)
    assert record["outcome"] == "failed"
    assert record["code"] == "UNHANDLED_EXCEPTION"
    assert "upstream exploded" not in lines[0]
    assert "2096164064667852801" not in lines[0]


def test_a_broken_log_handler_does_not_change_the_response(provision):
    logger = logging.getLogger("aiops.assistant_question")

    class Exploding(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("log sink down")

    handler = Exploding()
    logger.addHandler(handler)
    try:
        with TestClient(provision.app, raise_server_exceptions=False) as client:
            response = post(client)
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 202
    assert response.json()["status"] == "completed"


def test_same_second_concurrency_keeps_one_id_and_one_line_per_request(provision, lines):
    with TestClient(provision.app) as client:

        def one() -> None:
            assert post(client).status_code == 202

        threads = [threading.Thread(target=one) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

    records = parsed(lines)
    assert len(records) == 4, records
    ids = [record["request_id"] for record in records]
    assert len(set(ids)) == 4, ids


def test_real_uvicorn_http_writes_one_line_per_request(provision, lines):
    """真实 uvicorn + TCP，断言真实写出的日志行，而不是 TestClient 语义。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(provision.app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started

            with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
                ok = post(client)
                assert ok.status_code == 202
                provision.app.state.gateway.order_authorizer.can_access = lambda _c, _o: False  # type: ignore[union-attr]
                refused = post(client)
                assert refused.status_code == 404
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()

    records = parsed(lines)
    assert [record["outcome"] for record in records] == ["completed", "refused"]
    assert records[0]["qa_id"].startswith("qa_")
    assert records[1]["qa_id"] == "-"


# ---------------------------------------------------------------- unit


def test_non_http_scopes_pass_straight_through():
    seen: list[str] = []

    async def downstream(scope, _receive, _send):
        seen.append(scope["type"])

    asyncio.run(ro.AssistantQuestionObservation(downstream)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_format_line_rejects_free_text_in_the_error_code():
    record = ro.Observation(request_id="cor_0123456789ab", candidate=True)
    record.error_code = "not a code: /v1/orders/123"
    line = ro.format_line(record, http_status=404)
    assert "error_code=UNKNOWN" in line
    assert "/v1/orders/123" not in line


def test_observation_of_returns_none_when_not_installed():
    request = SimpleNamespace(scope={"type": "http"})
    assert ro.observation_of(request) is None
