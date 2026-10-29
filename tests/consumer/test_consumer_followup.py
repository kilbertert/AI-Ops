"""未解决反馈使用本人已验证主题；两次即给人工入口。"""

import sqlite3

import pytest
from test_app_runtime_dify_entry import HEADERS, dify_chat

from consumer.test_consumer_chat_service import Client, done
from consumer.test_consumer_knowledge_flow import setup, wire


def ask(env, question, conversation=None, headers=HEADERS):
    return env["client"].post(
        "/v1/assistant/questions",
        headers=headers,
        json={"question": question, **({"conversation_id": conversation} if conversation else {})},
    )


@pytest.mark.parametrize("feedback", ["还是不行", "Still not working!"])
def test_two_feedback_turns_keep_topic_and_exit_without_more_calls(tmp_path, monkeypatch, feedback):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)
        first = ask(env, "怎么开始充电？").json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        conversation = first["conversation_id"]
        second = ask(env, feedback, conversation)
        assert second.status_code == 202, second.text
        final = done(env, second.json()["qa_id"]).json()
        assert final["status"] == "completed", final
        assert "哪一步" in final["result"]["blocks"][0]["text"]
        assert kb.calls[-1][1] == "怎么开始充电？\n" + feedback
        # 重新构造存储客户端，证明未使用进程内计数。
        runtime.dify_run_store = type(runtime.dify_run_store)(runtime.store.path)
        third = ask(env, feedback, conversation)
        assert third.status_code == 200, third.text
        assert "https://support.example.test/" in third.json()["message"]
        assert "已转接" not in third.json()["message"]
        assert len(Client.calls) == 2 and len(kb.calls) == 2
        other = ask(env, feedback, conversation, {**HEADERS, "Authorization": "Bearer token-b"})
        assert other.status_code == 404
        assert len(Client.calls) == 2 and len(kb.calls) == 2
        changed = ask(env, "怎样查看费用？", conversation)
        assert changed.status_code == 202
        assert done(env, changed.json()["qa_id"]).json()["status"] == "completed"
        next_turn = ask(env, feedback, conversation)
        assert next_turn.status_code == 202
        assert done(env, next_turn.json()["qa_id"]).json()["status"] == "completed"
        assert kb.calls[-1][1] == "怎样查看费用？\n" + feedback


@pytest.mark.parametrize("damage", ["expiry", "answer"])
def test_invalid_previous_answer_never_enters_followup(tmp_path, monkeypatch, damage):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, _ = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)
        first = ask(env, "怎么开始充电？").json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        with sqlite3.connect(env["db"]) as con:
            if damage == "expiry":
                con.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
            else:
                con.execute("UPDATE assistant_questions SET result_json='{}'")
        response = ask(env, "还是不行", first["conversation_id"])
        if damage == "expiry":
            assert response.status_code == 200 and response.json()["code"] == "CONSUMER_CONTEXT"
            assert "之前的数据已失效" in response.json()["message"]
            assert done(env, first["qa_id"]).status_code == 410
        else:
            assert response.status_code == 503
        assert len(Client.calls) == 1 and len(kb.calls) == 1


def test_previous_expires_during_retrieval_and_new_context_is_not_disclosed(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)
        first = ask(env, "怎么开始充电？").json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"

        def expire():
            with sqlite3.connect(env["db"]) as con:
                con.execute(
                    "UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE qa_id=?",
                    (first["qa_id"],),
                )

        kb.after = expire
        response = ask(env, "还是不行", first["conversation_id"])
        final = done(env, response.json()["qa_id"]).json()
        assert final["status"] == "failed" and final.get("result") is None
        run = runtime.dify_run_store.by_qa(response.json()["qa_id"])
        assert runtime.dify_run_store.context(run.run_id) is None
        assert len(kb.calls) == 2


def test_feedback_without_history_stays_a_normal_question(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, _ = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)
        first = ask(env, "还是不行")
        assert first.status_code == 202
        assert done(env, first.json()["qa_id"]).json()["status"] == "completed"
        assert kb.calls[0][1] == "还是不行"
