from __future__ import annotations

import base64
import http.client
import http.server
import io
import json
import threading
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from aiops_diagnostics.bounded_http import (
    AUTH_REJECTED,
    AUTH_REJECTED_STATUSES,
    DECODE_EXCEPTIONS,
    DETAIL_KEYS,
    ENVELOPE_SUCCESS_CODES,
    FORM_CONTENT_TYPE,
    HTTP_ERROR,
    INVALID_BODY,
    INVALID_ENVELOPE,
    JSON_CONTENT_TYPE,
    NO_RETRY,
    TENANT_ID_HEADER,
    TEXT_PLAIN_CONTENT_TYPE,
    TRANSPORT_EXCEPTIONS,
    TRANSPORT_FAILURES,
    UNAVAILABLE,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    RetryPolicy,
    basic_auth_header,
    bearer_auth_header,
    build_internal_token_headers,
    build_request,
    classify_failure,
    classify_http_error,
    classify_transport_error,
    decode_json,
    domain_error,
    error_detail,
    form_body,
    internal_token_headers,
    join_url,
    json_body,
    open_response,
    parse_code_data_envelope,
    parse_data_key_envelope,
    parse_raw_envelope,
    quote_segment,
    read_body,
    request_json,
    retry_delay,
    should_retry,
    tenant_id_header,
)
from aiops_diagnostics.http_auth import build_internal_token

TOKEN_TIMESTAMP = 1700000000


class _DomainError(RuntimeError):
    """测试用领域错误：把语义分类带出来，供断言映射结果。"""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class _Response:
    """伪上游的一个响应：可作上下文管理器，``read`` 尊重传入的大小。"""

    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.headers = Message()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]


class _FakeUpstream:
    """一个伪上游：按脚本依次给出响应或故障，并记录收到的每个请求。

    四组能力（请求构造、异常映射、信封、退避）的骨架级测试都由它驱动。
    """

    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []
        self.outcomes: list[Any] = []

    def queue(self, *outcomes: Any) -> _FakeUpstream:
        self.outcomes.extend(outcomes)
        return self

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> _Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.outcomes:
            raise AssertionError("fake upstream ran out of scripted outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def last_request(self) -> urllib.request.Request:
        return self.requests[-1]

    def sent_headers(self, index: int = -1) -> dict[str, str]:
        # urllib 把头名 normalize 成 ``Name.capitalize()``（Content-Type → Content-type），
        # 断言前统一小写。
        return {name.lower(): value for name, value in self.requests[index].header_items()}


def _json_response(payload: object, *, status: int = 200) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"), status=status)


def _mapping(**overrides: Any) -> ErrorMapping:
    """一个测试用映射：四个语义分类各构造一个带 kind 的领域错误。"""
    values: dict[str, Any] = {
        "auth_rejected": lambda failure: _DomainError(
            f"auth:{failure.status}:{failure.detail}", kind=AUTH_REJECTED
        ),
        "http_error": lambda failure: _DomainError(
            f"http:{failure.status}:{failure.detail}", kind=HTTP_ERROR
        ),
        "unavailable": lambda failure: _DomainError(f"unavailable:{failure.detail}", kind=UNAVAILABLE),
        "invalid_body": lambda failure: _DomainError(f"invalid_body:{failure.detail}", kind=INVALID_BODY),
        "invalid_envelope": lambda failure: _DomainError(
            f"invalid_envelope:{failure.detail}", kind=INVALID_ENVELOPE
        ),
    }
    values.update(overrides)
    return ErrorMapping(**values)


def _spec(**overrides: Any) -> RequestSpec:
    values: dict[str, Any] = {"url": "https://upstream.example.test/v1/thing"}
    values.update(overrides)
    return RequestSpec(**values)


def _http_error(
    status: int, body: bytes = b"", url: str = "https://upstream.example.test/v1/thing"
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(body))


def _install(monkeypatch: pytest.MonkeyPatch, upstream: _FakeUpstream) -> _FakeUpstream:
    monkeypatch.setattr(urllib.request, "urlopen", upstream)
    return upstream


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------


def test_join_url_uses_one_joining_rule() -> None:
    assert join_url("https://upstream.example.test/", "/v1/thing") == "https://upstream.example.test/v1/thing"
    assert (
        join_url("https://upstream.example.test///", "v1/thing") == "https://upstream.example.test/v1/thing"
    )
    assert join_url("https://upstream.example.test/") == "https://upstream.example.test"
    joined = join_url(
        "https://upstream.example.test", "/diag/order", query={"order_no": "A 1", "tenant_id": "T/2"}
    )
    assert joined == "https://upstream.example.test/diag/order?order_no=A+1&tenant_id=T%2F2"


def test_quote_segment_escapes_path_metacharacters() -> None:
    assert quote_segment("kb-01") == "kb-01"
    assert quote_segment("a/b") == "a%2Fb"
    assert quote_segment("../user/info") == "..%2Fuser%2Finfo"
    assert quote_segment("空间") == "%E7%A9%BA%E9%97%B4"


def test_request_spec_is_sent_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _install(monkeypatch, _FakeUpstream().queue(_json_response({"ok": True})))
    spec = _spec(
        method="POST",
        headers={"Content-Type": JSON_CONTENT_TYPE, "X-Custom": "1"},
        body=json_body({"question": "充电异常"}),
        timeout=7.5,
    )

    assert request_json(spec, mapping=_mapping()) == {"ok": True}

    request = upstream.last_request
    assert request.get_method() == "POST"
    assert request.full_url == spec.url
    assert request.data == json_body({"question": "充电异常"})
    assert upstream.timeouts == [7.5]
    assert upstream.sent_headers()["content-type"] == JSON_CONTENT_TYPE
    assert upstream.sent_headers()["x-custom"] == "1"


def test_body_builders_encode_utf8_json_and_form() -> None:
    assert json_body({"q": "充电"}) == '{"q": "充电"}'.encode()
    assert json_body(None) == b"null"
    assert form_body({"token": "a b/c"}) == b"token=a+b%2Fc"


@pytest.mark.parametrize(
    ("name", "spec", "expected"),
    [
        (
            "basic+form",
            _spec(
                method="POST",
                headers={
                    "Authorization": basic_auth_header("aiops", "secret"),
                    "Content-Type": FORM_CONTENT_TYPE,
                },
                body=form_body({"token": "opaque"}),
            ),
            {"authorization": "Basic YWlvcHM6c2VjcmV0", "content-type": FORM_CONTENT_TYPE},
        ),
        (
            "bearer",
            _spec(headers={"Authorization": bearer_auth_header("platform-token")}),
            {"authorization": "Bearer platform-token"},
        ),
        (
            "internal-token",
            _spec(headers=internal_token_headers("secret", 300, TOKEN_TIMESTAMP)),
            {
                "x-internal-token": build_internal_token("secret", TOKEN_TIMESTAMP, 300),
                "x-request-timestamp": str(TOKEN_TIMESTAMP),
            },
        ),
        (
            "tenant-id",
            _spec(headers=tenant_id_header("TENANT-A")),
            {TENANT_ID_HEADER: "TENANT-A"},
        ),
        (
            "basic+text/plain",
            _spec(
                method="POST",
                headers={
                    "Authorization": basic_auth_header("proxy-backend", "upstream-secret"),
                    "Content-Type": TEXT_PLAIN_CONTENT_TYPE,
                },
                body=b"SHOW STABLES",
            ),
            {
                "authorization": f"Basic {base64.b64encode(b'proxy-backend:upstream-secret').decode()}",
                "content-type": TEXT_PLAIN_CONTENT_TYPE,
            },
        ),
    ],
)
def test_every_auth_variant_assembles_its_declared_headers(
    monkeypatch: pytest.MonkeyPatch, name: str, spec: RequestSpec, expected: dict[str, str]
) -> None:
    del name
    upstream = _install(monkeypatch, _FakeUpstream().queue(_json_response({"ok": True})))

    request_json(spec, mapping=_mapping())

    sent = upstream.sent_headers()
    for header, value in expected.items():
        assert sent[header] == value


def test_basic_auth_header_quotes_credentials() -> None:
    assert basic_auth_header("client id", "secret:value") == "Basic Y2xpZW50JTIwaWQ6c2VjcmV0JTNBdmFsdWU="


def test_internal_token_headers_reuse_the_single_existing_implementation() -> None:
    assert internal_token_headers is build_internal_token_headers


def test_read_body_reads_one_byte_past_the_cap_so_truncation_is_detectable() -> None:
    response = _Response(b"0123456789")
    assert read_body(response) == b"0123456789"
    assert read_body(response, max_read_bytes=4) == b"01234"
    assert read_body(response, max_read_bytes=10) == b"0123456789"


class _RedirectingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
            return
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", JSON_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def redirecting_upstream() -> Any:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_follow_redirects_is_an_explicit_endpoint_parameter(redirecting_upstream: str) -> None:
    """禁重定向是端点策略（重定向会绕过它），因此必须是显式参数。"""
    followed = request_json(_spec(url=f"{redirecting_upstream}/redirect"), mapping=_mapping())
    assert followed == {"ok": True}

    no_follow = _spec(url=f"{redirecting_upstream}/redirect", follow_redirects=False)
    with pytest.raises(_DomainError) as excinfo:
        request_json(no_follow, mapping=_mapping())
    assert excinfo.value.kind == HTTP_ERROR
    assert str(excinfo.value) == "http:302:"


def test_open_response_reads_a_real_socket_response(redirecting_upstream: str) -> None:
    with open_response(_spec(url=f"{redirecting_upstream}/target", timeout=5.0)) as response:
        assert response.status == 200
        assert json.loads(read_body(response)) == {"ok": True}


def test_build_request_normalises_headers_through_urllib() -> None:
    request = build_request(_spec(method="PUT", headers={"X-Custom": "1"}, body=b"x"))
    assert request.get_method() == "PUT"
    assert request.data == b"x"
    assert request.get_header("X-custom") == "1"


# --------------------------------------------------------------------------
# 异常映射
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_kind"),
    [
        (401, AUTH_REJECTED),
        (403, AUTH_REJECTED),
        (400, HTTP_ERROR),
        (404, HTTP_ERROR),
        (429, HTTP_ERROR),
        (500, HTTP_ERROR),
        (502, HTTP_ERROR),
        (503, HTTP_ERROR),
    ],
)
def test_http_status_maps_onto_the_single_classification_table(
    monkeypatch: pytest.MonkeyPatch, status: int, expected_kind: str
) -> None:
    _install(monkeypatch, _FakeUpstream().queue(_http_error(status, json.dumps({"msg": "拒绝"}).encode())))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping())

    assert excinfo.value.kind == expected_kind


def test_a_client_declares_its_own_auth_rejected_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """introspection 把 400 也算凭证被拒——差异是显式参数，不是另一张表。"""
    _install(monkeypatch, _FakeUpstream().queue(_http_error(400)))
    mapping = _mapping(auth_rejected_statuses=frozenset({400, 401, 403}))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=mapping)

    assert excinfo.value.kind == AUTH_REJECTED
    assert frozenset({401, 403}) == AUTH_REJECTED_STATUSES


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        TimeoutError("timed out"),
        ConnectionResetError("reset by peer"),
        OSError("connection refused"),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_every_transport_failure_becomes_a_coded_domain_error(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    _install(monkeypatch, _FakeUpstream().queue(failure))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping())

    assert excinfo.value.kind == UNAVAILABLE
    assert str(excinfo.value) == f"unavailable:{failure.__class__.__name__}"
    assert excinfo.value.__cause__ is failure


def test_transport_failure_capture_set_is_defined_once() -> None:
    """捕获集合在骨架内单点定义：调用点不再各自手写异常元组。"""
    assert TRANSPORT_FAILURES == TRANSPORT_EXCEPTIONS + DECODE_EXCEPTIONS
    for expected in (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
        OSError,
        http.client.IncompleteRead,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        assert expected in TRANSPORT_FAILURES


@pytest.mark.parametrize("body", [b"\xff\xfe", b'{"broken"', b""])
def test_non_utf8_or_invalid_json_body_is_an_invalid_response(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    _install(monkeypatch, _FakeUpstream().queue(_Response(body)))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping())

    assert excinfo.value.kind == INVALID_BODY
    assert str(excinfo.value) in {"invalid_body:UnicodeDecodeError", "invalid_body:JSONDecodeError"}


def test_decode_json_reports_the_failing_decoder() -> None:
    assert decode_json(b'{"ok": true}', mapping=_mapping()) == {"ok": True}
    with pytest.raises(_DomainError) as excinfo:
        decode_json(b"\xff", mapping=_mapping())
    assert str(excinfo.value) == "invalid_body:UnicodeDecodeError"
    with pytest.raises(_DomainError) as excinfo:
        decode_json(b'{"broken"', mapping=_mapping())
    assert str(excinfo.value) == "invalid_body:JSONDecodeError"


def test_error_detail_reads_the_declared_keys_and_falls_back_to_empty() -> None:
    detail = _http_error(401, b'{"detail": "token expired"}')
    assert error_detail(detail) == "token expired"
    assert error_detail(detail, keys=("msg",)) == ""
    assert error_detail(_http_error(500, b"not json")) == ""
    assert error_detail(_http_error(500, b"")) == ""
    assert DETAIL_KEYS == ("detail", "msg", "message")


def test_http_error_detail_reaches_the_domain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeUpstream().queue(_http_error(401, b'{"msg": "token expired"}')))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping())

    assert str(excinfo.value) == "auth:401:token expired"
    error = _http_error(503, b'{"msg": "upstream down"}')
    assert classify_http_error(error, mapping=_mapping()) == HttpFailure(
        kind=HTTP_ERROR, status=503, detail="upstream down", cause=error
    )


def test_classify_transport_error_names_the_failing_layer() -> None:
    failure = classify_transport_error(http.client.IncompleteRead(b"partial"))
    assert failure.kind == UNAVAILABLE
    assert failure.detail == "IncompleteRead"
    assert isinstance(failure.cause, http.client.IncompleteRead)


def test_classify_failure_routes_every_captured_exception_to_its_own_bucket() -> None:
    """解码故障不并入"上游不可用"：它与传输层的文案在部分客户端不同。"""
    mapping = _mapping()

    decode = classify_failure(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), mapping=mapping)
    assert decode.kind == INVALID_BODY
    assert decode.detail == "UnicodeDecodeError"

    assert classify_failure(TimeoutError("slow"), mapping=mapping).kind == UNAVAILABLE
    assert classify_failure(_http_error(403), mapping=mapping).kind == AUTH_REJECTED
    assert classify_failure(_http_error(500), mapping=mapping).kind == HTTP_ERROR


def test_domain_error_dispatches_on_the_classified_kind() -> None:
    mapping = _mapping()
    assert domain_error(HttpFailure(AUTH_REJECTED), mapping).kind == AUTH_REJECTED
    assert domain_error(HttpFailure(HTTP_ERROR), mapping).kind == HTTP_ERROR
    assert domain_error(HttpFailure(UNAVAILABLE), mapping).kind == UNAVAILABLE
    assert domain_error(HttpFailure(INVALID_BODY), mapping).kind == INVALID_BODY
    assert domain_error(HttpFailure(INVALID_ENVELOPE), mapping).kind == INVALID_ENVELOPE


def test_http_errors_are_never_retried_even_with_a_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _install(monkeypatch, _FakeUpstream().queue(_http_error(503)))
    monkeypatch.setattr("aiops_diagnostics.bounded_http.time.sleep", lambda _seconds: None)

    with pytest.raises(_DomainError):
        request_json(_spec(), mapping=_mapping(), retry=RetryPolicy(max_retries=4))

    assert len(upstream.requests) == 1


# --------------------------------------------------------------------------
# 信封解析
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [0, 200])
def test_code_data_envelope_takes_data_on_success(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    payload = {"code": code, "msg": "ok", "data": {"id": "B-1"}}
    _install(monkeypatch, _FakeUpstream().queue(_json_response(payload)))

    assert request_json(_spec(), mapping=_mapping(), envelope=parse_code_data_envelope) == {"id": "B-1"}


@pytest.mark.parametrize(
    ("payload", "expected_kind", "expected_message"),
    [
        ({"code": 401, "msg": "令牌无效"}, AUTH_REJECTED, "auth:None:令牌无效"),
        ({"code": 403, "msg": "令牌无效"}, AUTH_REJECTED, "auth:None:令牌无效"),
        ({"code": 500, "msg": "查询失败"}, UNAVAILABLE, "unavailable:查询失败"),
        ({"code": 500}, UNAVAILABLE, "unavailable:invalid response"),
        (["not", "an", "envelope"], INVALID_ENVELOPE, "invalid_envelope:invalid response"),
        (None, INVALID_ENVELOPE, "invalid_envelope:invalid response"),
    ],
)
def test_code_data_envelope_classifies_rejections(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected_kind: str, expected_message: str
) -> None:
    _install(monkeypatch, _FakeUpstream().queue(_json_response(payload)))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping(), envelope=parse_code_data_envelope)

    assert excinfo.value.kind == expected_kind
    assert str(excinfo.value) == expected_message


def test_code_data_envelope_uses_the_declared_auth_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """信封层的 401/403 与 HTTP 层同一张表，不另写一套。"""
    _install(monkeypatch, _FakeUpstream().queue(_json_response({"code": 403, "msg": "拒绝"})))
    mapping = _mapping(auth_rejected_statuses=frozenset({401, 403}))
    assert frozenset({0, 200}) == ENVELOPE_SUCCESS_CODES

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=mapping, envelope=parse_code_data_envelope)

    assert excinfo.value.kind == AUTH_REJECTED


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": {"chunks": [1]}}, {"chunks": [1]}),
        ({"chunks": [1]}, {"chunks": [1]}),
        ([{"chunk_id": "c-1"}], [{"chunk_id": "c-1"}]),
    ],
)
def test_data_key_envelope_unwraps_only_when_present(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected: object
) -> None:
    _install(monkeypatch, _FakeUpstream().queue(_json_response(payload)))

    assert request_json(_spec(), mapping=_mapping(), envelope=parse_data_key_envelope) == expected


@pytest.mark.parametrize("payload", [None, 7, "text"])
def test_data_key_envelope_rejects_non_object_payloads(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    _install(monkeypatch, _FakeUpstream().queue(_json_response(payload)))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping(), envelope=parse_data_key_envelope)

    assert excinfo.value.kind == INVALID_ENVELOPE


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": 0, "data": [[1, 2]]}, {"code": 0, "data": [[1, 2]]}),
        ([{"dsType": 0, "roleCode": "ROLE_1"}], [{"dsType": 0, "roleCode": "ROLE_1"}]),
    ],
)
def test_raw_envelope_returns_the_whole_payload(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected: object
) -> None:
    """TDengine REST 与 UPMS ds 兜底分别是对象和数组，raw 模式两者都原样返回。"""
    _install(monkeypatch, _FakeUpstream().queue(_json_response(payload)))

    assert request_json(_spec(), mapping=_mapping(), envelope=parse_raw_envelope) == expected


@pytest.mark.parametrize("payload", [None, 7, "text"])
def test_raw_envelope_rejects_scalar_payloads(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    _install(monkeypatch, _FakeUpstream().queue(_json_response(payload)))

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping(), envelope=parse_raw_envelope)

    assert excinfo.value.kind == INVALID_ENVELOPE


# --------------------------------------------------------------------------
# 退避
# --------------------------------------------------------------------------


def test_retry_delay_is_the_single_jittered_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiops_diagnostics.bounded_http.random.uniform", lambda _low, _high: 0.0)
    policy = RetryPolicy()

    assert [retry_delay(attempt, policy=policy) for attempt in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_retry_delay_adds_jitter_within_its_bounds() -> None:
    """抖动是必需的：共享上游重启后并发客户端不得步调一致地重试。"""
    policy = RetryPolicy()
    for attempt, base in enumerate((1.0, 2.0, 4.0, 8.0)):
        delays = [retry_delay(attempt, policy=policy) for _ in range(200)]
        assert all(base <= delay <= base + policy.jitter_seconds for delay in delays)
        assert max(delays) - min(delays) > 0.2


def test_the_single_backoff_also_covers_a_smaller_base_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二份退避（媒体 404 重试的 0.25*2^n）由同一实现以参数表达。"""
    monkeypatch.setattr("aiops_diagnostics.bounded_http.random.uniform", lambda _low, _high: 0.0)
    policy = RetryPolicy(base_delay_seconds=0.25, max_delay_seconds=2.0)

    assert [retry_delay(attempt, policy=policy) for attempt in range(6)] == [0.25, 0.5, 1.0, 2.0, 2.0, 2.0]


def test_should_retry_respects_budget_method_and_deadline() -> None:
    policy = RetryPolicy(max_retries=2)

    assert should_retry(0, policy=policy) is True
    assert should_retry(1, policy=policy) is True
    assert should_retry(2, policy=policy) is False
    assert should_retry(0, policy=policy, method="POST") is False
    assert should_retry(0, policy=policy, deadline=10.0, now=10.0) is False
    assert should_retry(0, policy=policy, deadline=10.0, now=9.5) is True
    assert should_retry(0, policy=policy, deadline=None) is True
    assert RetryPolicy(max_retries=-1).attempts == 1
    assert NO_RETRY.max_retries == 0
    assert NO_RETRY.attempts == 1


def test_request_json_retries_transient_get_failures_with_jittered_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [urllib.error.URLError("blip"), TimeoutError("slow"), _json_response({"ok": True})]
    upstream = _install(monkeypatch, _FakeUpstream().queue(*outcomes))
    slept: list[float] = []
    monkeypatch.setattr("aiops_diagnostics.bounded_http.time.sleep", slept.append)

    assert request_json(_spec(), mapping=_mapping(), retry=RetryPolicy(max_retries=3)) == {"ok": True}

    assert len(upstream.requests) == 3
    assert len(slept) == 2
    assert 1.0 <= slept[0] <= 2.0
    assert 2.0 <= slept[1] <= 3.0


def test_request_json_never_retries_non_idempotent_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _install(monkeypatch, _FakeUpstream().queue(urllib.error.URLError("blip")))
    monkeypatch.setattr("aiops_diagnostics.bounded_http.time.sleep", lambda _seconds: None)

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(method="POST"), mapping=_mapping(), retry=RetryPolicy(max_retries=4))

    assert excinfo.value.kind == UNAVAILABLE
    assert len(upstream.requests) == 1


def test_request_json_stops_retrying_past_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮询类调用方把自己的超时传下来，慢连接不会重试到远超超时。"""
    upstream = _install(monkeypatch, _FakeUpstream().queue(*[urllib.error.URLError("down")] * 6))
    clock = {"now": 0.0}
    monkeypatch.setattr("aiops_diagnostics.bounded_http.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "aiops_diagnostics.bounded_http.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    with pytest.raises(_DomainError):
        request_json(_spec(), mapping=_mapping(), retry=RetryPolicy(max_retries=5), deadline=3.0)

    # 1 次首发 + 约 1s + 约 2s，越过 3s deadline 后停止
    assert len(upstream.requests) == 3


def test_request_json_reports_the_last_failure_after_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [
        urllib.error.URLError("blip"),
        http.client.IncompleteRead(b"partial"),
        ConnectionResetError("reset"),
    ]
    upstream = _install(monkeypatch, _FakeUpstream().queue(*outcomes))
    monkeypatch.setattr("aiops_diagnostics.bounded_http.time.sleep", lambda _seconds: None)

    with pytest.raises(_DomainError) as excinfo:
        request_json(_spec(), mapping=_mapping(), retry=RetryPolicy(max_retries=2))

    assert len(upstream.requests) == 3
    assert str(excinfo.value) == "unavailable:ConnectionResetError"


def test_request_json_without_a_retry_policy_makes_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = _install(monkeypatch, _FakeUpstream().queue(urllib.error.URLError("blip")))

    with pytest.raises(_DomainError):
        request_json(_spec(), mapping=_mapping())

    assert len(upstream.requests) == 1
