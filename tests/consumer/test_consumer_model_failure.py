"""从真实模型HTTP边界进入自由问答，不能用分类器替身掩盖丢失的原因。"""

import json
from dataclasses import replace

import pytest
from execution.test_app_runtime_dify_entry import dify_chat

from aiops_diagnostics.common.config import AgentSettings, ProviderConfig, Settings
from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime
from consumer.test_consumer_chat_service import Client, done
from consumer.test_consumer_intent_client import response, server
from consumer.test_consumer_live_context import ask, wire

REAL_CLASSIFY = GatewayRuntime.classify_consumer


def real_classifier(runtime, monkeypatch, base):
    settings = replace(
        Settings(),
        agent=AgentSettings(
            providers=(ProviderConfig("test", base, model="test", api_key_env="TEST_MODEL_KEY"),),
            default_provider="test",
        ),
    )
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-never-log")
    monkeypatch.setattr(Settings, "from_config", lambda *_: settings)
    monkeypatch.setattr(runtime, "classify_consumer", REAL_CLASSIFY.__get__(runtime))


@pytest.mark.parametrize(
    "status,error,code,retryable,reason",
    [
        (
            403,
            {"code": "insufficient_user_quota"},
            "CONSUMER_MODEL_QUOTA_EXHAUSTED",
            False,
            "quota_exhausted",
        ),
        (
            403,
            {"message": "用户额度不足 secret-never-log"},
            "CONSUMER_MODEL_QUOTA_EXHAUSTED",
            False,
            "quota_exhausted",
        ),
        (429, {"code": "insufficient_quota"}, "CONSUMER_MODEL_QUOTA_EXHAUSTED", False, "quota_exhausted"),
        (429, {"type": "insufficient_quota"}, "CONSUMER_MODEL_QUOTA_EXHAUSTED", False, "quota_exhausted"),
        (
            403,
            {"message": "access denied secret-never-log"},
            "CONSUMER_INTENT_UNAVAILABLE",
            False,
            "auth_rejected",
        ),
        (429, {"code": "rate_limit_exceeded"}, "CONSUMER_INTENT_UNAVAILABLE", True, "rate_limited"),
        (500, {"message": "secret-never-log"}, "CONSUMER_INTENT_UNAVAILABLE", True, "upstream_error"),
    ],
)
def test_free_question_reports_model_failure_without_reading_orders(
    tmp_path, monkeypatch, caplog, status, error, code, retryable, reason
):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        with server({"error": error}, status) as (base, calls):
            real_classifier(runtime, monkeypatch, base)
            result = ask(env, "How to charge safely")
        assert result.status_code == 503
        body = result.json()["error"]
        assert body["code"] == code
        assert body["retryable"] is retryable
        assert "无法理解" not in body["message"]
        assert "reason=" + reason in caplog.text
        assert "secret-never-log" not in result.text + caplog.text
        assert len(calls) == 1
        assert not Client.calls and not kb.calls and env["upstream"].calls == 0


def test_same_free_question_recovers_with_real_classifier(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        with server({"error": {"code": "insufficient_quota"}}, 429) as (base, _):
            real_classifier(runtime, monkeypatch, base)
            assert ask(env, "How to charge safely").status_code == 503
        decision = {"intent": "knowledge", "order_reference": "none", "uses_context": False}
        with server(response(decision)) as (base, calls):
            real_classifier(runtime, monkeypatch, base)
            result = ask(env, "How to charge safely")
        assert result.status_code == 202, result.text
        final = done(env, result.json()["qa_id"]).json()
        assert final["status"] == "completed" and final["result"]["blocks"]
        assert len(calls) == 1 and len(kb.calls) == 1 and len(Client.calls) == 1
        assert env["upstream"].calls == 0
        assert "secret-never-log" not in json.dumps(final)
