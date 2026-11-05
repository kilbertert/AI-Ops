"""原生预览经真实 HTTP 桥走任务授权；上游使用隔离响应。"""

import http.client
import json
import subprocess
import threading
from pathlib import Path

import pytest
from dify.test_dify_console_publish import (
    APP_ID,
    ORIGIN,
    SESSION,
    TENANT_ID,
    TRIAL_KEY,
    _browser_headers,
    _serve,
    _Upstream,
)

from aiops_diagnostics.dify import dify_console_publish as preview
from aiops_diagnostics.dify.dify_console_publish import BridgeSettings, make_server


def test_native_fetch_and_stop_match_dify_request_shapes():
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(root / "tests/operator_native_preview.mjs"),
            str(root / "ops/dify-console-bridge/operator-native-preview.js"),
        ],
        check=True,
    )


def test_browser_disconnect_requests_cancellation(bridge, upstream, monkeypatch):
    from aiops_diagnostics.dify.dify_console_publish import ConsolePublishBridgeHandler

    calls = []
    original = preview._native_gateway

    def gateway(handler, session, path, **kwargs):
        calls.append(path)
        if path.endswith("/cancel"):
            return 200, {"status": "cancelled"}
        return original(handler, session, path, **kwargs)

    def disconnected(self, event):
        raise BrokenPipeError("browser disconnected")

    monkeypatch.setattr(preview, "_native_gateway", gateway)
    monkeypatch.setattr(ConsolePublishBridgeHandler, "_sse", disconnected)
    status, _, body = send(bridge)
    assert status == 200 and body == ""
    assert calls == ["", "/qa_test1/cancel"]


def test_stop_uses_same_company_session(bridge, monkeypatch):
    calls = []

    def gateway(handler, session, path, **kwargs):
        calls.append((session.access_token, path))
        return 200, {"status": "cancelled"}

    monkeypatch.setattr(preview, "_native_gateway", gateway)
    status, _, body = send(bridge, {"task_id": "aiops_qa_test1"}, path=preview.NATIVE_CANCEL_ROUTE)
    assert status == 200 and json.loads(body) == {"result": "success"}
    assert calls == [(SESSION, "/qa_test1/cancel")]


@pytest.mark.parametrize("fault", ["disabled", "forbidden-app", "bad-query", "bad-prompt", "bad-tools"])
def test_configuration_and_app_access_fail_closed(bridge, upstream, monkeypatch, fault):
    body = {"app_id": APP_ID, "query": "订单 2115330675982561281", "pre_prompt": "prompt", "tools": []}
    if fault == "disabled":
        from aiops_diagnostics.dify.dify_console_publish import ConsolePublishBridgeHandler

        monkeypatch.setattr(
            ConsolePublishBridgeHandler,
            "_check_browser_request",
            lambda self: (_ for _ in ()).throw(preview._Rejected(404, "disabled")),
        )
    if fault == "forbidden-app":
        upstream.app_status = 403
    if fault == "bad-query":
        body["query"] = 42
    if fault == "bad-prompt":
        body["pre_prompt"] = ""
    if fault == "bad-tools":
        body["tools"] = "tool"
    code, _, raw = send(bridge, body)
    assert code in {400, 403, 404} and json.loads(raw)["message"]
    assert not any(x["path"].startswith("/v1/") for x in upstream.calls)


@pytest.mark.parametrize("code", ["CPMS_SCOPE_MISMATCH", "CHAT_ROUTE_RETRACTED", "OPERATOR_PREVIEW_UNBOUND"])
def test_upstream_refusal_has_no_success_event(bridge, upstream, code):
    upstream.trial_status = 403
    upstream.trial_body = {"error": {"code": code}}
    status, _, body = send(bridge)
    assert status == 403 and json.loads(body)["code"] == code
    assert "message_end" not in body


def test_running_then_failed_does_not_deliver_report(bridge, upstream, monkeypatch):
    monkeypatch.setattr(preview, "NATIVE_POLL_SECONDS", 0)
    original = preview._native_gateway
    reads = []

    def gateway(handler, session, path, **kwargs):
        if not path:
            return original(handler, session, path, **kwargs)
        reads.append(path)
        return 200, {"status": "running" if len(reads) == 1 else "failed", "error_code": "NODE_FAILED"}

    monkeypatch.setattr(preview, "_native_gateway", gateway)
    code, _, body = send(bridge)
    assert code == 200 and ": waiting" in body and "NODE_FAILED" in body
    assert "message_end" not in body and len(reads) == 2


@pytest.mark.parametrize(
    "query,expected",
    [
        ("诊断订单 123456789012", "123456789012"),
        ("1234", "1234"),
        ("order ID: A-12", "A-12"),
        ("我上一个订单是什么情况", ""),
        ("hello", ""),
        ("帮我找一下广州天河附近的充电站", ""),
        ("帮我查询充电站 TEST2026-10-29 03:00:09", ""),
        # `.`/`:` 是合同承诺的形状（#606）：旧语法复查候选会判「不支持的
        # 字符」，收口到订单标识权威后完整抽出、不再截断或拒收。
        ("订单 123456789012.12", "123456789012.12"),
        ("订单 123456789012:12", "123456789012:12"),
        ("订单号: 1234.56，订单号: 9876", None),
        # 「订单」二字不是门槛：只写了单号的句子同样在指一张订单（2026-11-02 截图）。
        ("帮我分析这笔单的电费2116050516255272961", "2116050516255272961"),
        ("帮我分析这单的电费2116050516255272961", "2116050516255272961"),
        ("分析一下2116050516255272961这笔", "2116050516255272961"),
        ("帮我看看这笔充电的电费 2116050516255272961", "2116050516255272961"),
        # 门限与提取共用同一组候选：日期排在单号前面时，不能把日期当成订单。
        # （2026-11-02 评审实测：旧写法先抓 `TEST2026-10-29`。）
        ("帮我分析站点 TEST2026-10-29 这笔单2116050516255272961", "2116050516255272961"),
        # 英文单号紧贴中文时不能被拆成两个候选（`ORDER` 与 `-1001`）。
        ("帮我分析这笔单的电费ORDER-1001", "ORDER-1001"),
        ("order no: 2096164064667852801", "2096164064667852801"),
    ],
)
def test_explicit_order_syntax_never_truncates(query, expected):
    if expected is None:
        with pytest.raises(preview._Rejected):
            preview._native_order(query)
    else:
        assert preview._native_order(query) == expected


@pytest.fixture
def upstream():
    fixture = _Upstream()
    server = _serve(fixture)
    yield fixture
    server.shutdown()
    server.server_close()


@pytest.fixture
def bridge(upstream):
    upstream.trial_body = {"trial_id": "qa_test1", "status": "queued", "conversation_id": "conv_" + "a" * 32}
    upstream.trial_result_body = {
        "trial_id": "qa_test1",
        "status": "completed",
        "result": {"report": {"summary": "本次订单事实\ncpms.orderDetail"}},
    }
    server = make_server(
        BridgeSettings(
            "127.0.0.1",
            0,
            upstream.base,
            upstream.base,
            "publish-key",
            ORIGIN,
            TENANT_ID,
            trial_key=TRIAL_KEY,
            login_proxy_url=upstream.base,
        )
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


def send(bridge, payload=None, headers=None, path=preview.NATIVE_PREVIEW_ROUTE):
    payload = (
        payload
        if payload is not None
        else {
            "app_id": APP_ID,
            "query": "检查订单 2115330675982561281",
            "pre_prompt": "CURRENT_DRAFT",
            "tools": [],
        }
    )
    connection = http.client.HTTPConnection(*bridge, timeout=5)
    connection.request("POST", path, json.dumps(payload), dict(headers or _browser_headers()))
    response = connection.getresponse()
    body = response.read().decode()
    connection.close()
    return response.status, dict(response.headers), body


def test_original_preview_delivers_sse_without_credentials(bridge, upstream):
    status, headers, body = send(bridge)
    assert status == 200 and headers["Content-Type"].startswith("text/event-stream")
    events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
    assert events[-1]["event"] == "message_end"
    assert "本次订单事实" in events[-2]["answer"]
    assert events[0]["task_id"] == "aiops_qa_test1"
    assert events[1]["event"] == "agent_thought" and events[1]["thought"] == ""
    assert SESSION not in body and TRIAL_KEY not in body
    call = next(x for x in upstream.calls if x["path"] == "/v1/dify/trial")
    assert call["body"]["operator_preview"]["pre_prompt"] == "CURRENT_DRAFT"
    assert call["body"]["order_no"] == ""  # 由同一模型循环选择，桥不先做语义分类。
    assert call["headers"]["x-aiops-console-session"] == SESSION


def test_dify_use_chat_envelope_is_accepted_without_forwarding_context(bridge, upstream):
    http_status, _, body = send(
        bridge,
        {
            "app_id": APP_ID,
            "query": "检查订单 2115330675982561281",
            "pre_prompt": "CURRENT_DRAFT",
            "tools": [],
            "response_mode": "streaming",
            "conversation_id": "",
            "files": [],
            "inputs": {},
        },
    )
    assert http_status == 200 and "本次订单事实" in body
    call = next(x for x in upstream.calls if x["path"] == "/v1/dify/trial")
    assert not {"files", "inputs", "conversation_id", "response_mode"} & call["body"].keys()


@pytest.mark.parametrize(
    "field,value",
    [
        ("files", [{}]),
        ("inputs", {"tenant_id": "OTHER"}),
        ("conversation_id", "other-chat"),
        ("response_mode", "blocking"),
    ],
)
def test_native_preview_rejects_nonempty_unimplemented_context(bridge, upstream, field, value):
    http_status, _, _ = send(
        bridge,
        {
            "app_id": APP_ID,
            "query": "检查订单 2115330675982561281",
            "pre_prompt": "CURRENT_DRAFT",
            "tools": [],
            field: value,
        },
    )
    assert http_status == 400
    assert not any(x["path"] == "/v1/dify/trial" for x in upstream.calls)


@pytest.mark.parametrize("fault", ["csrf", "origin", "session", "app", "tenant"])
def test_rejects_untrusted_input_before_gateway(bridge, upstream, fault):
    headers = dict(_browser_headers())
    body = {"app_id": APP_ID, "query": "订单 2115330675982561281", "pre_prompt": "p", "tools": []}
    if fault == "csrf":
        headers["X-CSRF-Token"] = "wrong"
    if fault == "origin":
        headers["Origin"] = "https://evil.example"
    if fault == "session":
        headers.pop("Cookie")
    if fault == "app":
        body["app_id"] = "another"
    if fault == "multiple":
        body["query"] += " 2115330675982561282"
    if fault == "tenant":
        body["tenant_id"] = "foreign"
    status, _, response = send(bridge, body, list(headers.items()))
    assert 400 <= status < 500 and json.loads(response)["message"]
    assert not any(x["path"].startswith("/v1/") for x in upstream.calls)


def test_non_order_preview_reaches_gateway_without_fake_order(bridge, upstream):
    status, _, body = send(
        bridge,
        {
            "app_id": APP_ID,
            "query": "帮我找一下广州天河附近的充电站",
            "pre_prompt": "CURRENT_DRAFT",
            "tools": [],
        },
    )
    assert status == 200 and "message_end" in body
    call = next(x for x in upstream.calls if x["path"] == "/v1/dify/trial")
    assert call["body"]["order_no"] == ""
    assert call["body"]["question"] == "帮我找一下广州天河附近的充电站"


def test_missing_company_session_is_clear_and_not_retried(bridge, upstream):
    upstream.trial_status = 403
    upstream.trial_body = {"error": {"code": "OPERATOR_TRIAL_LOGIN_REQUIRED"}}
    status, _, body = send(bridge)
    assert status == 403 and "重新登录" in json.loads(body)["message"]
    assert sum(x["path"] == "/v1/dify/trial" for x in upstream.calls) == 1


def test_timeout_requests_cancellation(bridge, upstream, monkeypatch):
    monkeypatch.setattr(preview, "NATIVE_TIMEOUT_SECONDS", 0)
    calls = []
    original = preview._native_gateway

    def gateway(handler, session, path, **kwargs):
        calls.append(path)
        return (
            (200, {"status": "cancelled"})
            if path.endswith("/cancel")
            else original(handler, session, path, **kwargs)
        )

    monkeypatch.setattr(preview, "_native_gateway", gateway)
    _, _, body = send(bridge)
    assert "OPERATOR_PREVIEW_TIMEOUT" in body
    assert calls == ["", "/qa_test1/cancel"]


def test_read_failure_cancels_and_does_not_send_success(bridge, upstream, monkeypatch):
    upstream.trial_result_status = 403
    upstream.trial_result_body = {"error": {"code": "OPERATOR_TRIAL_LOGIN_REQUIRED"}}
    calls = []
    original = preview._native_gateway

    def gateway(handler, session, path, **kwargs):
        calls.append(path)
        return (
            (200, {"status": "cancelled"})
            if path.endswith("/cancel")
            else original(handler, session, path, **kwargs)
        )

    monkeypatch.setattr(preview, "_native_gateway", gateway)
    _, _, body = send(bridge)
    assert "重新登录" in body and "message_end" not in body
    assert calls[-1] == "/qa_test1/cancel"


def test_preview_conversation_reaches_gateway_and_returns_in_sse(bridge, monkeypatch):
    calls = []
    conversation = "conv_" + "a" * 32
    original = preview._native_gateway

    def gateway(handler, session, path, **kwargs):
        calls.append((path, kwargs.get("body")))
        if path == "":
            return 200, {"trial_id": "qa_test1", "status": "queued", "conversation_id": conversation}
        return original(handler, session, path, **kwargs)

    monkeypatch.setattr(preview, "_native_gateway", gateway)
    status, _, body = send(
        bridge,
        {
            "app_id": APP_ID,
            "query": "那这笔单的电压呢",
            "pre_prompt": "p",
            "tools": [],
            "conversation_id": conversation,
        },
    )
    assert status == 200
    assert calls[0][1]["conversation_id"] == conversation
    assert calls[0][1]["order_no"] == ""
    events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]
    assert all(event["conversation_id"] == conversation for event in events if event.get("event") != "error")


def test_missing_conversation_is_not_a_successful_preview(bridge, upstream):
    upstream.trial_body = {"trial_id": "qa_test1", "status": "queued"}
    status, _, body = send(bridge)
    assert status == 502 and "OPERATOR_PREVIEW_VERSION_MISMATCH" in body
    assert "data:" not in body
    assert any(x["path"].endswith("/cancel") for x in upstream.calls)


def test_native_preview_displays_quota_failure(bridge, upstream):
    upstream.trial_result_body = {
        "status": "failed",
        "error_code": "OPERATOR_MODEL_QUOTA_EXHAUSTED",
        "error_message": "模型服务额度不足，请联系管理员补充额度后重试。",
        "result": None,
    }
    status, _, body = send(bridge)
    assert status == 200
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    error = next(event for event in events if event.get("event") == "error")
    assert "额度不足" in error["message"]
    assert "补充额度" in error["message"]
    assert "message_end" not in body
