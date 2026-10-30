"""真实HTTP分类请求：格式、故障、无工具与拒绝重定向。"""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from aiops_diagnostics.common.config import ProviderConfig
from aiops_diagnostics.consumer.consumer_intent import classify

DECISION = {"intent": "order_status", "order_reference": "latest", "uses_context": False}


@contextmanager
def server(body, status=200):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", self.server.base + "/redirected")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http.base = f"http://127.0.0.1:{http.server_port}"
    Thread(target=http.serve_forever, daemon=True).start()
    try:
        yield http.base, calls
    finally:
        http.shutdown()
        http.server_close()


def response(value=DECISION, status="completed"):
    return {
        "status": status,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}],
    }


@pytest.mark.parametrize("wire", ["responses", "chat"])
def test_one_typed_model_request_without_tools_or_provider_session(wire):
    body = (
        response()
        if wire == "responses"
        else {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(DECISION)}}]}
    )
    with server(body) as (base, calls):
        result = classify(
            "我上一个订单现在是什么情况",
            provider=ProviderConfig("test", base, wire, model="test-model"),
            api_key="synthetic",
            language="zh",
        )
        assert result == DECISION and len(calls) == 1
        path, payload = calls[0]
        assert path == ("/responses" if wire == "responses" else "/chat/completions")
        assert "tools" not in payload and "previous_response_id" not in payload
        messages = payload.get("input", payload.get("messages"))
        assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
        assert json.loads(messages[1]["content"])["question"] == "我上一个订单现在是什么情况"
        assert "synthetic" not in json.dumps(payload)


@pytest.mark.parametrize(
    "body",
    [
        response(status="incomplete"),
        response({**DECISION, "order_no": "123"}),
        {},
        {"status": "completed", "output": []},
        {"status": "completed", "output": response()["output"] + [{"type": "function_call"}]},
        {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
    ],
)
def test_invalid_or_incomplete_answer_is_not_used(body):
    with server(body) as (base, calls):
        with pytest.raises(ValueError):
            classify(
                "query",
                provider=ProviderConfig("test", base, model="test"),
                api_key="synthetic",
                language="en",
            )
        assert len(calls) == 1


@pytest.mark.parametrize("extra", [{"refusal": "no"}, {"tool_calls": [{"id": "unexpected"}]}])
def test_chat_refusal_or_tool_call_cannot_accompany_a_valid_decision(extra):
    body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(DECISION), **extra}}]}
    with server(body) as (base, _), pytest.raises(ValueError):
        classify(
            "query",
            provider=ProviderConfig("test", base, "chat", model="test"),
            api_key="synthetic",
            language="en",
        )


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_no_redirect_or_retry_on_upstream_failure(status):
    with server({}, status) as (base, calls):
        with pytest.raises(RuntimeError):
            classify(
                "query",
                provider=ProviderConfig("test", base, model="test"),
                api_key="synthetic",
                language="en",
            )
        assert len(calls) == 1


def test_evaluation_runner_scores_fixed_expectations_and_retains_no_key(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "tools/verify_consumer_intent.py"
    spec = importlib.util.spec_from_file_location("verify_intent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with server(response()) as (base, calls):
        config = tmp_path / "test.env"
        config.write_text(
            f"AIOPS_PROVIDERS=test\nAIOPS_PROVIDER_TEST_BASE_URL={base}\n"
            "AIOPS_PROVIDER_TEST_MODEL=test\nAIOPS_PROVIDER_TEST_KEY_ENV=TEST_MODEL_KEY\n"
        )
        config.chmod(0o600)
        monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")
        report = tmp_path / "report.json"
        monkeypatch.setattr(sys, "argv", [str(path), "--config", str(config), "--out", str(report)])
        assert module.main() == 1
        result = json.loads(report.read_text())
        assert len(calls) == len(result["results"]) == 17
        assert result["results"][0]["passed"] and not result["results"][2]["passed"]
        assert "test-secret" not in report.read_text() and report.stat().st_mode & 0o077 == 0
