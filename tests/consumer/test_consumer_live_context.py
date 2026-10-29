"""Chatflow本人状态读取：同轮一次业务快照与权限生命周期。"""

import json
import sqlite3
import uuid

import pytest
from execution.test_app_runtime_dify_entry import HEADERS, dify_chat
from execution.test_app_runtime_node_entry import ORDER_A, ORDER_B

from aiops_diagnostics.dify.dify_chatflow import ChatflowReply
from consumer.test_consumer_chat_service import Client, done
from consumer.test_consumer_knowledge_flow import setup


class Flow(Client):
    env = None
    after = None
    skip = False
    token = ""

    def run(self, **kwargs):
        self.calls.append(kwargs)
        ids = [str(uuid.uuid4()) for _ in range(4)]
        kwargs["on_started"](*ids)
        Flow.token = kwargs["inputs"]["data_grant"]
        path = (
            "/v1/internal/consumer-data/" + kwargs["inputs"]["data_kind"]
            if "data_kind" in kwargs["inputs"]
            else "/v1/internal/consumer-knowledge"
        )
        if not Flow.skip:
            r = self.env["client"].post(path, headers={"Authorization": "Bearer " + self.token})
            assert r.status_code == 200, r.text
            context = r.json()
            again = self.env["client"].post(path, headers={"Authorization": "Bearer " + self.token})
            assert again.json() == context
        else:
            context = {}
        if Flow.after:
            Flow.after()
        selection = (
            {"kind": "realtime", "faq_id": ""}
            if context.get("status") == "live" or Flow.skip
            else {"kind": "knowledge", "faq_id": "KB-C-007"}
        )
        return ChatflowReply(*ids, json.dumps(selection), {})


def wire(env, monkeypatch, tmp_path):
    runtime, kb, _ = setup(env, monkeypatch, tmp_path)
    doc = json.loads(env["bindings"].read_text())
    doc["bindings"][0]["live_order_status"] = True
    doc["bindings"][0]["callback_version"] = 2
    env["bindings"].write_text(json.dumps(doc))
    Flow.env, Flow.after, Flow.skip = env, None, False
    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", Flow)
    return runtime, kb


def ask(env, question="check my latest order status", conversation=None, headers=HEADERS):
    return env["client"].post(
        "/v1/assistant/questions",
        headers=headers,
        json={"question": question, **({"conversation_id": conversation} if conversation else {})},
    )


def test_live_order_has_one_snapshot_and_never_carries_it_to_next_model_turn(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        intro = ask(env, "怎么开始充电？").json()
        assert done(env, intro["qa_id"]).json()["status"] == "completed"
        first = ask(env, conversation=intro["conversation_id"])
        assert first.status_code == 202, first.text
        first = first.json()
        final = done(env, first["qa_id"]).json()
        assert final["status"] == "completed", final
        text = final["result"]["blocks"][0]["text"]
        assert ORDER_A in text and "cpms.chargingInfo" in text and "UTC" in text
        assert env["upstream"].calls == 1 and len(kb.calls) == 1
        assert Client.calls[-1]["conversation_id"] == ""
        after = ask(env, "怎么查看费用？", first["conversation_id"])
        assert after.status_code == 202
        assert done(env, after.json()["qa_id"]).json()["status"] == "completed"
        assert Client.calls[-1]["conversation_id"] == ""
        explicit = ask(env, f"check order {ORDER_B} status").json()
        final = done(env, explicit["qa_id"]).json()
        assert final["status"] == "completed" and ORDER_B in final["result"]["blocks"][0]["text"]
        assert env["upstream"].calls == 2
        from execution.test_app_runtime_node_entry import TOKENS

        monkeypatch.setitem(TOKENS, "token-b", ("USER-B", "TENANT-A"))
        other = {**HEADERS, "Authorization": "Bearer token-b"}
        assert ask(env, f"check order {ORDER_A} status", headers=other).status_code == 404
        env["orders"].ownership[ORDER_A] = "USER-B"
        assert done(env, first["qa_id"]).status_code == 404
        assert env["upstream"].calls == 2


@pytest.mark.parametrize("mode", ["revoke", "cancel", "expire", "binding"])
def test_no_live_data_escapes_changes_during_business_read(tmp_path, monkeypatch, mode):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        import aiops_diagnostics.gateway_api as api

        original = api._authorized_charging_snapshot

        def read(settings, caller, order):
            result = original(settings, caller, order)
            if mode == "revoke":
                env["orders"].ownership[order] = "USER-B"
            elif mode == "binding":
                doc = json.loads(env["bindings"].read_text())
                doc["bindings"][0]["agent_version_no"] += 1
                env["bindings"].write_text(json.dumps(doc))
            else:
                with sqlite3.connect(env["db"]) as con:
                    if mode == "cancel":
                        con.execute("UPDATE chat_dify_runs SET state='cancelled'")
                    else:
                        con.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")
            return result

        monkeypatch.setattr(api, "_authorized_charging_snapshot", read)
        first = ask(env).json()
        final = done(env, first["qa_id"]).json()
        assert final["status"] == "failed" and final.get("result") is None
        run = runtime.dify_run_store.by_qa(first["qa_id"])
        assert runtime.dify_run_store.context(run.run_id) is None
        assert env["upstream"].calls == 1 and not kb.calls


def test_worker_cannot_deliver_after_authorization_is_lost(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _ = wire(env, monkeypatch, tmp_path)
        Flow.after = lambda: env["orders"].ownership.clear()
        first = ask(env).json()
        final = done(env, first["qa_id"]).json()
        assert final["status"] == "failed" and final["error"]["code"] == "ORDER_NOT_VISIBLE"
        assert final.get("result") is None


def test_model_realtime_claim_without_business_read_cannot_complete(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        wire(env, monkeypatch, tmp_path)
        Flow.skip = True
        final = done(env, ask(env).json()["qa_id"]).json()
        assert final["status"] == "failed" and final.get("result") is None
        assert env["upstream"].calls == 0


def test_order_read_scope_failure_prevents_any_workflow_or_business_read(tmp_path, monkeypatch):
    from aiops_diagnostics.identity.caller_auth import CallerAuthError

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb = wire(env, monkeypatch, tmp_path)
        resolver = env["app"].state.gateway.caller_resolver
        original = resolver.resolve

        def resolve(token, *, required_scope, **kwargs):
            if required_scope == "aiops:orders:read":
                raise CallerAuthError("expired", code="caller_auth.invalid")
            return original(token, required_scope=required_scope, **kwargs)

        monkeypatch.setattr(resolver, "resolve", resolve)
        result = ask(env)
        assert result.status_code == 401
        assert Client.calls == [] and env["upstream"].calls == 0 and not kb.calls


def test_worker_rechecks_expiry_after_final_authorization(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)

        def after():
            def expire():
                with sqlite3.connect(env["db"]) as con:
                    con.execute("UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00'")

            env["orders"].mutations_at[env["orders"].allow_count + 1] = expire

        Flow.after = after
        first = ask(env).json()
        result = done(env, first["qa_id"]).json()
        assert result["status"] == "failed" and result.get("result") is None
        assert runtime.dify_run_store.snapshot(runtime.dify_run_store.by_qa(first["qa_id"]).run_id) is None
        assert env["orders"].mutations
