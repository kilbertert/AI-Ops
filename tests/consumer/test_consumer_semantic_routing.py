"""语义标签必须决定真实路由；分类器桩只用于隔离API测试。"""

import sqlite3

import pytest
from test_app_runtime_dify_entry import dify_chat
from test_consumer_chat_service import Client, done
from test_consumer_live_context import ask, wire


def semantic(runtime, monkeypatch, intent="order_status", reference="latest", dependent=False):
    calls = []

    def classify(question, **kwargs):
        calls.append((question, kwargs))
        return {"intent": intent, "order_reference": reference, "uses_context": dependent}

    monkeypatch.setattr(runtime, "classify_consumer", classify, raising=False)
    return calls


@pytest.mark.parametrize("question", ["我上一个订单现在是什么情况", "刚才那次充电结束了没"])
def test_semantics_reach_live_order_instead_of_faq(tmp_path, monkeypatch, question):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        calls = semantic(runtime, monkeypatch)
        response = ask(env, question)
        assert response.status_code == 202, response.text
        final = done(env, response.json()["qa_id"]).json()
        assert final["status"] == "completed", final
        assert len(calls) == 1 and env["upstream"].calls == 1 and not kb.calls
        assert "cpms.chargingInfo" in final["result"]["blocks"][0]["text"]


def test_fresh_query_after_expired_faq_keeps_conversation_but_no_stale_memory(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, "knowledge", "none")
        first = ask(env, "怎么查看历史订单？").json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        with sqlite3.connect(env["db"]) as db:
            db.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
        calls = semantic(runtime, monkeypatch)
        second = ask(env, "帮我查看最新的订单的情况", first["conversation_id"])
        assert second.status_code == 202, second.text
        assert second.json()["conversation_id"] == first["conversation_id"]
        assert done(env, second.json()["qa_id"]).json()["status"] == "completed"
        assert done(env, first["qa_id"]).status_code == 410
        assert len(calls) == 1 and not calls[0][1].get("previous_question")
        assert Client.calls[-1]["conversation_id"] == ""
        assert env["upstream"].calls == 1 and len(kb.calls) == 1


@pytest.mark.parametrize("intent,reference", [("write_request", "latest"), ("clarify", "none")])
def test_unsupported_or_ambiguous_intent_never_starts_data_or_faq(tmp_path, monkeypatch, intent, reference):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, intent, reference)
        response = ask(env, "帮我处理刚才那单")
        assert response.status_code == 200 and response.json()["type"] == "clarification"
        assert not kb.calls and not Client.calls and env["upstream"].calls == 0


def test_semantic_knowledge_overrides_keyword_false_positive(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, "knowledge", "none")
        response = ask(env, "订单状态是什么意思？")
        assert response.status_code == 202, response.text
        assert done(env, response.json()["qa_id"]).json()["status"] == "completed"
        assert len(kb.calls) == 1 and env["upstream"].calls == 0


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"intent": "order_status", "order_no": "123"},
        {"intent": "order_status", "order_reference": "latest", "uses_context": "false"},
    ],
)
def test_invalid_classifier_never_falls_back_to_keywords(tmp_path, monkeypatch, value):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        monkeypatch.setattr(runtime, "classify_consumer", lambda *a, **k: value)
        response = ask(env, "查询最新订单状态")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CONSUMER_INTENT_UNAVAILABLE"
        assert not Client.calls and not kb.calls and env["upstream"].calls == 0


def test_classifier_outage_is_visible_and_never_reads_business_data(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)

        def outage(*a, **k):
            raise TimeoutError("synthetic timeout")

        monkeypatch.setattr(runtime, "classify_consumer", outage)
        response = ask(env)
        assert response.status_code == 503
        assert "尚未查询" in response.json()["error"]["message"]
        assert not Client.calls and not kb.calls and env["upstream"].calls == 0


def test_classification_cannot_grant_access_to_other_users_order(tmp_path, monkeypatch):
    from test_app_runtime_node_entry import ORDER_A

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, reference="explicit")
        env["orders"].ownership[ORDER_A] = "USER-B"
        response = ask(env, f"查一下 {ORDER_A} 到哪了")
        assert response.status_code == 404
        assert not Client.calls and not kb.calls and env["upstream"].calls == 0


@pytest.mark.parametrize("dependent", [True, False])
def test_valid_pronoun_rechecks_order_and_never_reuses_snapshot(tmp_path, monkeypatch, dependent):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch)
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        semantic(runtime, monkeypatch, reference="previous", dependent=dependent)
        second = ask(env, "那笔现在结束没", first["conversation_id"])
        assert second.status_code == 202
        assert done(env, second.json()["qa_id"]).json()["status"] == "completed"
        assert env["upstream"].calls == 2 and not kb.calls
        assert Client.calls[-1]["conversation_id"] == ""


def test_expiry_during_classification_does_not_reuse_previous_order(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch)
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"

        def classify(*a, **k):
            with sqlite3.connect(env["db"]) as db:
                db.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
            return {"intent": "order_status", "order_reference": "previous", "uses_context": True}

        monkeypatch.setattr(runtime, "classify_consumer", classify)
        response = ask(env, "那笔呢", first["conversation_id"])
        assert response.status_code == 200 and response.json()["code"] == "CONSUMER_CONTEXT"
        assert env["upstream"].calls == 1 and len(Client.calls) == 1 and not kb.calls


@pytest.mark.parametrize(
    "reference,question",
    [
        ("unclear", "查我倒数第二单"),
        ("none", "看看订单"),
        ("explicit", "看看 2026102600000001 和 2026102600000002"),
        ("latest", "看看 2026102600000001"),
        ("previous", "那单呢"),
    ],
)
def test_ambiguous_or_inconsistent_order_reference_never_reads(tmp_path, monkeypatch, reference, question):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, reference=reference)
        response = ask(env, question)
        assert response.status_code == 200 and response.json()["type"] == "clarification"
        assert not Client.calls and not kb.calls and env["upstream"].calls == 0


def test_previous_reference_is_rechecked_at_submission_even_if_model_says_independent(tmp_path, monkeypatch):
    import aiops_diagnostics.gateway_api as api

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch)
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        semantic(runtime, monkeypatch, reference="previous", dependent=False)
        original = api._begin_conversation_turn

        def expire(*args, **kwargs):
            with sqlite3.connect(env["db"]) as db:
                db.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
            return original(*args, **kwargs)

        monkeypatch.setattr(api, "_begin_conversation_turn", expire)
        second = ask(env, "那笔现在结束没", first["conversation_id"])
        assert second.status_code == 409
        assert "之前的数据已失效" in second.json()["error"]["message"]
        assert env["upstream"].calls == 1 and len(Client.calls) == 1 and not kb.calls


def test_latest_reference_does_not_silently_use_page_selected_order(tmp_path, monkeypatch):
    from test_app_runtime_dify_entry import HEADERS
    from test_app_runtime_node_entry import ORDER_B

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch)
        response = env["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "看看最近那单", "order_no": ORDER_B}
        )
        assert response.status_code == 200 and response.json()["code"] == "CONSUMER_ORDER"
        assert not Client.calls and not kb.calls and env["upstream"].calls == 0
