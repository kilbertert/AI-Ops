"""原 API 驱动 Chatflow 作业：用户/会话隔离、完成与取消、版本和正文守卫。"""

import json
import time
import uuid

from execution.test_app_runtime_dify_entry import HEADERS, dify_chat

from aiops_diagnostics.dify.dify_chatflow import ChatflowReply


class Client:
    calls = []
    stops = []
    before = None

    def __init__(self, *args, **kwargs):
        pass

    def run(self, **kwargs):
        self.calls.append(kwargs)
        conversation = kwargs["conversation_id"] or str(uuid.uuid4())
        run, task, message = (str(uuid.uuid4()) for _ in range(3))
        kwargs["on_started"](run, task, conversation, message)
        if type(self).before:
            type(self).before()
        return ChatflowReply(run, task, conversation, message, "请先核对设备提示。", {})

    def stop(self, **kwargs):
        self.stops.append(kwargs)
        return True


def configure(env, monkeypatch):
    # 既有生命周期测试使用确定性分类替身；语义质量由独立真实模型语料验收。
    from aiops_diagnostics.consumer.consumer_billing import is_billing_question
    from aiops_diagnostics.consumer.consumer_order_chat import asks_latest_order, is_order_status_question

    def classify(_runtime, question, **kwargs):
        intent = (
            "billing_record"
            if is_billing_question(question)
            else "order_status"
            if is_order_status_question(question)
            else "knowledge"
        )
        return {
            "intent": intent,
            "order_reference": "latest" if asks_latest_order(question) else "explicit",
            "uses_context": bool(kwargs.get("previous_question")),
        }

    monkeypatch.setattr(
        "aiops_diagnostics.gateway.gateway_runtime.GatewayRuntime.classify_consumer", classify
    )
    Client.calls = []
    Client.stops = []
    Client.before = None
    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", Client)
    env["settings"].consumer_chatflow_enabled = True
    document = json.loads(env["bindings"].read_text())
    row = document["bindings"][0]
    row.update(
        scenario="consumer_service",
        agent_id="agt_12345678",
        agent_version_no=1,
        app_id="11111111-2222-3333-4444-555555555555",
    )
    env["bindings"].write_text(json.dumps(document))


def ask(env, *, conversation=None, headers=HEADERS):
    payload = {"question": "怎么开始充电？"}
    if conversation:
        payload["conversation_id"] = conversation
    return env["client"].post("/v1/assistant/questions", headers=headers, json=payload)


def done(env, qa):
    for _ in range(100):
        response = env["client"].get("/v1/assistant/questions/" + qa, headers=HEADERS)
        if response.status_code != 200 or response.json()["status"] not in ["queued", "running"]:
            return response
        time.sleep(0.01)
    raise AssertionError("job did not settle")


def test_original_api_creates_and_reuses_a_scope_bound_conversation(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        first = ask(env)
        assert first.status_code == 202, first.text
        a = first.json()
        final = done(env, a["qa_id"])
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "completed"
        second = ask(env, conversation=a["conversation_id"])
        assert second.status_code == 202, second.text
        assert done(env, second.json()["qa_id"]).json()["status"] == "completed"
        assert Client.calls[0]["conversation_id"] == ""
        assert Client.calls[1]["conversation_id"]
        assert Client.calls[0]["user"] == Client.calls[1]["user"]
        assert not any(
            "token-a" in json.dumps({k: v for k, v in c.items() if k != "on_started"}) for c in Client.calls
        )
        other = {**HEADERS, "Authorization": "Bearer token-b"}
        response = ask(env, conversation=a["conversation_id"], headers=other)
        assert response.status_code == 404
        assert len(Client.calls) == 2
        history = env["client"].get("/v1/conversations/" + a["conversation_id"], headers=HEADERS)
        assert history.status_code == 200 and len(history.json()["turns"]) == 2
        assert env["upstream"].calls == 0


def test_removed_binding_hides_qa_and_history_and_does_not_fall_back(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        a = ask(env).json()
        assert done(env, a["qa_id"]).json()["status"] == "completed"
        env["settings"].consumer_chatflow_enabled = False
        assert done(env, a["qa_id"]).status_code == 404
        history = env["client"].get("/v1/conversations/" + a["conversation_id"], headers=HEADERS)
        assert history.status_code == 404


def test_binding_changes_during_the_run_fail_without_an_answer(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)

        def change():
            document = json.loads(env["bindings"].read_text())
            document["bindings"][0]["agent_version_no"] = 2
            env["bindings"].write_text(json.dumps(document))

        Client.before = change
        a = ask(env).json()
        final = done(env, a["qa_id"])
        assert final.json()["status"] == "failed"
        assert final.json().get("result") is None


def test_cancel_during_generation_stays_terminal_and_stops_its_task(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        import threading

        entered = threading.Event()
        release = threading.Event()
        Client.before = lambda: (entered.set(), release.wait(5))
        a = ask(env).json()
        assert entered.wait(5)
        result = env["client"].post("/v1/assistant/questions/" + a["qa_id"] + "/cancel", headers=HEADERS)
        release.set()
        assert result.status_code == 200 and result.json()["status"] == "cancelled"
        assert len(Client.stops) == 1
        assert done(env, a["qa_id"]).json()["status"] == "cancelled"


def test_corrupted_answer_is_not_returned(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        a = ask(env).json()
        assert done(env, a["qa_id"]).json()["status"] == "completed"
        import sqlite3

        with sqlite3.connect(env["db"]) as connection:
            connection.execute("update assistant_questions set result_json='{}' where qa_id=?", (a["qa_id"],))
        assert done(env, a["qa_id"]).status_code == 503


def test_same_version_changed_credential_reference_refuses_old_results(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        another = tmp_path / "replacement.key"
        another.write_text("replacement-app-key")
        another.chmod(0o600)
        data = json.loads(env["bindings"].read_text())
        data["bindings"][0]["workflow_key_file"] = str(another)
        env["bindings"].write_text(json.dumps(data))
        assert done(env, first["qa_id"]).status_code == 410
        assert ask(env, conversation=first["conversation_id"]).status_code == 409


def test_expired_result_is_not_delivered_from_qa_or_conversation(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        import sqlite3

        with sqlite3.connect(env["db"]) as connection:
            connection.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
        assert done(env, first["qa_id"]).status_code == 410
        # 快照过期仍拒绝回读；独立新问题在同一会话重新执行，不传递失效记忆。
        next_turn = ask(env, conversation=first["conversation_id"])
        assert next_turn.status_code == 202
        assert done(env, next_turn.json()["qa_id"]).json()["status"] == "completed"
        assert len(Client.calls) == 2 and Client.calls[-1]["conversation_id"] == ""
        assert (
            env["client"].get("/v1/conversations/" + first["conversation_id"], headers=HEADERS).status_code
            == 410
        )


def test_failed_upstream_never_falls_back_or_reuses_failed_conversation(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)

        def failure():
            raise RuntimeError("unavailable")

        Client.before = failure
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "failed"
        assert ask(env, conversation=first["conversation_id"]).status_code == 409
        assert len(Client.calls) == 1 and env["upstream"].calls == 0


def test_thai_and_khmer_start_a_chatflow(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        configure(env, monkeypatch)
        for language in ["th", "km"]:
            response = ask(env, headers={**HEADERS, "Accept-Language": language})
            assert response.status_code == 202
            assert done(env, response.json()["qa_id"]).json()["status"] == "completed"
        assert len(Client.calls) == 2
        assert env["app"].state.gateway.store.count_assistant_questions() == 2
