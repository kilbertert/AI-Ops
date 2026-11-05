"""已确认的会话/多工具/本人权限合同；原HTTP入口驱动，无最终JSON协议。"""

import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

from execution.test_app_runtime_dify_entry import HEADERS, dify_chat
from execution.test_app_runtime_node_entry import ORDER_A

from aiops_diagnostics.dify.dify_chatflow import ChatflowReply
from aiops_diagnostics.dify.dify_run_store import DifyRunError
from aiops_diagnostics.execution.chat_data_grant import DataGrantStore
from consumer.test_consumer_chat_service import Client, done
from consumer.test_consumer_knowledge_flow import setup


def wire(env, monkeypatch, tmp_path):
    runtime, kb, content = setup(env, monkeypatch, tmp_path)
    doc = json.loads(env["bindings"].read_text())
    doc["bindings"][0].update(callback_version=6, live_order_status=True, live_billing_record=True)
    env["bindings"].write_text(json.dumps(doc))
    body = json.loads(content.read_text())
    body.update(schema="aiops-consumer-live/1", entries={})
    content.write_text(json.dumps(body))
    monkeypatch.setattr(kb, "list_knowledge_bases", lambda: [{"knowledge_base_id": "kb-own"}], raising=False)

    def must_not_classify(*args, **kwargs):
        raise AssertionError("Agent入口不得先做互斥能力分类")

    monkeypatch.setattr(runtime, "classify_consumer", must_not_classify)
    return runtime, kb


def ask(env, question="这单为什么结束、是否扣款、接下来怎么办", conversation=None):
    return env["client"].post(
        "/v1/assistant/questions",
        headers=HEADERS,
        json={"question": question, **({"conversation_id": conversation} if conversation else {})},
    )


def test_agent_combines_tools_and_keeps_actual_answer_after_grant_expiry(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        observations, tokens = [], []
        reads = []

        def snapshot(settings, caller, order, kind):
            reads.append((order, kind))
            return {
                "order_no": order,
                "source": "cpms." + kind,
                "observed_at": "2026-11-05T00:00:00Z",
                "state": "ended",
                "amount": "8.54",
            }

        monkeypatch.setattr("aiops_diagnostics.gateway.gateway_api._authorized_consumer_snapshot", snapshot)

        class Agent(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                observations.append(json.loads(kwargs["inputs"]["conversation_context"]))
                assert set(kwargs["inputs"]) == {"language", "data_grant", "conversation_context"}
                assert kwargs["conversation_id"] == ""
                token = kwargs["inputs"]["data_grant"]
                tokens.append(token)
                assert (
                    env["client"]
                    .post(
                        "/v1/internal/consumer-data/knowledge",
                        headers={"Authorization": "Bearer " + token},
                        json={"knowledge_base_ids": ["kb-own"]},
                    )
                    .status_code
                    == 403
                )
                if len(observations) == 1:
                    for resource in ("status", "billing", "knowledge"):
                        response = env["client"].post(
                            "/v1/internal/consumer-data/agent",
                            headers={"Authorization": "Bearer " + token},
                            json={
                                "resource": resource,
                                "order_no": ORDER_A if resource != "knowledge" else "",
                                "query": "启动操作",
                                "knowledge_base_ids": ["kb-own"],
                            },
                        )
                        assert response.status_code == 200, response.text
                        assert response.json()["status"] == "found", response.text
                return ChatflowReply(*ids, "这笔订单已结束。请确认第2步的设备提示。", {})

        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", Agent)
        first = ask(env)
        assert first.status_code == 202, first.text
        first = first.json()
        final = done(env, first["qa_id"])
        assert final.status_code == 200 and final.json()["status"] == "completed", final.text
        assert len(reads) == 2 and len(kb.calls) == 1
        assert DataGrantStore(runtime.store.path).by_token(tokens[0]).state == "revoked"
        with sqlite3.connect(runtime.store.path) as db:
            db.execute(
                "UPDATE chat_dify_runs SET expires_at='2000-01-01T00:00:00+00:00' WHERE qa_id=?",
                (first["qa_id"],),
            )
        second = ask(env, "第2步仍然没有反应", first["conversation_id"])
        assert second.status_code == 202, second.text
        assert done(env, second.json()["qa_id"]).json()["status"] == "completed"
        assert observations[1]["history"][0]["answer"] == "这笔订单已结束。请确认第2步的设备提示。"
        assert observations[1]["history"][0]["order_ids"] == [ORDER_A]
        assert len(reads) == 2
        assert (
            env["client"]
            .post(
                "/v1/internal/consumer-data/agent",
                headers={"Authorization": "Bearer " + tokens[0]},
                json={"resource": "status", "order_no": ORDER_A},
            )
            .status_code
            == 401
        )
        other = env["client"].post(
            "/v1/assistant/questions",
            headers={**HEADERS, "Authorization": "Bearer token-b"},
            json={"question": "继续", "conversation_id": first["conversation_id"]},
        )
        assert other.status_code == 404
        env["orders"].ownership[ORDER_A] = "USER-B"
        assert done(env, first["qa_id"]).status_code == 404
        assert done(env, second.json()["qa_id"]).status_code == 404
        assert ask(env, "继续", first["conversation_id"]).status_code == 404


def test_tool_denial_does_not_read_and_budget_is_atomic(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        results = []

        class Agent(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                token = kwargs["inputs"]["data_grant"]
                for body in (
                    {"resource": "status", "order_no": "someone-else"},
                    {"resource": "knowledge", "query": "规则", "knowledge_base_ids": ["other-tenant"]},
                ):
                    r = env["client"].post(
                        "/v1/internal/consumer-data/agent",
                        headers={"Authorization": "Bearer " + token},
                        json=body,
                    )
                    assert r.status_code == 200 and r.json()["status"] == "refused", r.text
                grant = DataGrantStore(runtime.store.path).by_token(token)
                run = runtime.dify_run_store.by_qa(grant.qa_id)

                def reserve(_):
                    try:
                        n = runtime.dify_run_store.reserve_consumer_tool(run.run_id, "support", {})
                        assert runtime.dify_run_store.finish_consumer_tool(run.run_id, n, {"status": "found"})
                        return n
                    except DifyRunError as e:
                        assert e.code == "CONSUMER_TOOL_BUDGET"
                        return None

                with ThreadPoolExecutor(max_workers=6) as pool:
                    results.extend(pool.map(reserve, range(12)))
                return ChatflowReply(*ids, "无法查看这张订单，请确认本人订单号。", {})

        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", Agent)
        q = ask(env).json()
        assert done(env, q["qa_id"]).json()["status"] == "completed"
        assert len([x for x in results if x is not None]) == 6
        assert env["upstream"].calls == 0 and not kb.calls


def test_agent_trial_uses_same_tools_and_rechecks_selected_objects(tmp_path, monkeypatch):
    import copy
    import time

    import yaml
    from execution.test_app_runtime_node_entry import _Resolver

    from consumer.test_consumer_agent_flow import candidate
    from consumer.test_consumer_publication import APP, Console, prepare, publish

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _, routes = prepare(env, monkeypatch, tmp_path)
        assert publish(env).status_code == 200
        before = routes.get("env-a", "TENANT-A", "consumer", "consumer_service")
        workflow = candidate()
        workflow["environment_variables"].append(
            {
                "id": "knowledge_answer_mode",
                "name": "knowledge_answer_mode",
                "value_type": "string",
                "value": "documents",
            }
        )
        monkeypatch.setattr(
            kb, "list_knowledge_bases", lambda: [{"knowledge_base_id": "kb-own"}], raising=False
        )

        class Draft(Console):
            def export_dsl(self, *args, **kwargs):
                return yaml.safe_dump({"app": {"mode": "advanced-chat"}, "workflow": copy.deepcopy(workflow)})

        monkeypatch.setattr("aiops_diagnostics.dify.dify_dsl_pull.DifyDslClient", Draft)
        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyDslClient", Draft)
        from dataclasses import replace

        from aiops_diagnostics.identity.scope_context import DataScope

        caller = replace(
            _Resolver().resolve("token-a", required_scope="aiops:orders:read"),
            data_scope=DataScope(type="all"),
            scope_fingerprint="",
        )
        # 生产运营预览按公司范围授权选定单；夹具原授权器仅模拟消费者本人范围。
        monkeypatch.setattr(
            env["orders"],
            "can_access",
            lambda scope, order: (
                env["orders"].ownership.get(order) == "USER-A" and scope.effective_tenant_id == "TENANT-A"
            ),
        )
        monkeypatch.setattr(runtime, "trial_context_for", lambda *args: caller)
        monkeypatch.setattr(runtime, "trial_run_context", lambda *args: caller)
        monkeypatch.setattr(
            runtime,
            "classify_consumer",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("trial classified")),
        )
        env["settings"].dify_trial_api_key = "trial-key"
        deleted = []

        class Admin:
            def __init__(self, *args):
                pass

            def import_app_dsl(self, *args, **kwargs):
                return "99999999-9999-9999-9999-999999999999"

            def app_service_key(self, *args):
                return "temporary-key"

            def delete_app(self, *args):
                deleted.append(args[-1])

        class Trial(Client):
            def run(self, **kwargs):
                ids = [str(uuid.uuid4()) for _ in range(4)]
                kwargs["on_started"](*ids)
                for resource, order in (("orders", ""), ("status", "OTHER")):
                    denied = env["client"].post(
                        "/v1/internal/consumer-data/agent",
                        headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
                        json={"resource": resource, "order_no": order},
                    )
                    assert denied.status_code == 200 and denied.json()["status"] == "refused"
                knowledge = env["client"].post(
                    "/v1/internal/consumer-data/agent",
                    headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
                    json={"resource": "knowledge", "query": "充电操作", "knowledge_base_ids": ["kb-own"]},
                )
                assert knowledge.status_code == 200 and knowledge.json()["status"] == "found", knowledge.text
                response = env["client"].post(
                    "/v1/internal/consumer-data/agent",
                    headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
                    json={"resource": "status", "order_no": ORDER_A},
                )
                assert response.status_code == 200, response.text
                return ChatflowReply(*ids, "已查询所选订单，请确认第2步。", {})

        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyConsoleAdmin", Admin)
        monkeypatch.setattr("aiops_diagnostics.consumer.consumer_trial.DifyChatflowClient", Trial)
        response = env["client"].post(
            "/v1/dify/trial",
            headers={"Authorization": "Bearer trial-key"},
            json={"app_id": APP, "order_no": ORDER_A, "question": "看看选定这单"},
        )
        assert response.status_code == 202, response.text
        trial_id = response.json()["trial_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = runtime.get_trial(caller, trial_id)
            if record["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert record["status"] == "completed", record
        assert record["result"]["report"]["summary"] == "已查询所选订单，请确认第2步。"
        assert deleted and routes.get("env-a", "TENANT-A", "consumer", "consumer_service") == before
        env["orders"].ownership[ORDER_A] = "USER-B"
        assert (
            env["client"]
            .get("/v1/dify/trial/" + trial_id, headers={"Authorization": "Bearer trial-key"})
            .status_code
            == 404
        )


def test_recent_orders_preserve_authoritative_ordering_and_database_time():
    from datetime import datetime
    from types import SimpleNamespace

    from aiops_diagnostics.consumer.consumer_agent import _query

    runtime = SimpleNamespace(_require_chat_order_visible=lambda caller, order: None)
    binding = SimpleNamespace(live_order_status=True, live_billing_record=True)
    rows = [
        {"order_no": "NEW", "created_time": datetime(2026, 11, 5, 10, 0)},
        {"order_no": "OLDER", "created_time": datetime(2026, 11, 4, 10, 0)},
    ]
    result = _query(runtime, None, binding, None, "orders", "", "", None, None, lambda caller: rows)
    assert result["latest_order_no"] == "NEW"
    assert result["orders"][0]["created_time"] == "2026-11-05T10:00:00"
    json.dumps(result)


def test_natural_answer_retains_only_evidenced_order_identifiers():
    from aiops_diagnostics.consumer.consumer_agent import final_answer

    own, unverified = "2117784634192551937", "2117784634192551999"
    context = {"calls": [{"tool": "status", "result": {"order_ids": [own], "status": "found"}}]}
    text = final_answer(f"订单{own}已结束；其他编号{unverified}，电话13800138000。", context)["blocks"][0][
        "text"
    ]
    assert own in text and unverified not in text and "13800138000" not in text
