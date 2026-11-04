"""从原HTTP入口验证连续问答，不以拼入一句文本代替会话闭环。"""

import pytest

from aiops_diagnostics.conversation.conversation_store import ConversationError
from aiops_diagnostics.operator import operator_cpms
from operations.test_operator_cpms import APP, HEADERS, ORDER, environment, node, terminal


@pytest.mark.parametrize(
    "question",
    ["那这笔单的电压呢", "那为什么服务费折扣还不能判通过？", "那么增值税为什么不一致？"],
)
def test_formal_followup_keeps_order_and_gets_fresh_evidence(tmp_path, monkeypatch, question):
    with environment(tmp_path, monkeypatch) as f:
        seen = []

        def dispatch(self, config, run_id):
            result = node(f, run_id)
            run = f["ctx"].agent_run_store.get(run_id)
            seen.append((run.order_no, f["ctx"].operator_cpms.tasks[run_id].messages))
            return result

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "分析这笔订单的电费", "order_no": ORDER},
        )
        assert first.status_code == 202
        assert terminal(f, first.json()["qa_id"]).json()["status"] == "completed"
        conversation = first.json().get("conversation_id")
        assert conversation, "首轮必须返回可续问会话"
        before = len(f["state"]["calls"])
        second = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": question, "conversation_id": conversation},
        )
        assert second.status_code == 202, second.text
        assert terminal(f, second.json()["qa_id"]).json()["status"] == "completed"
        assert [x[0] for x in seen] == [ORDER, ORDER]
        assert "分析这笔订单的电费" in str(seen[1][1])
        assert len(f["state"]["calls"]) > before, "追问不能把旧证据当成当前事实"


def test_history_reads_latest_completed_questions_and_not_other_scope(tmp_path):
    from aiops_diagnostics.conversation.conversation_store import ConversationStore

    store = ConversationStore(tmp_path / "history.db")
    scope = "owner"
    conv = store.create(
        scope_fingerprint=scope, business_entry="operator", agent_version_key="agt_test0000#v1"
    )
    for n in range(1, 8):
        store.begin_turn(conv["conversation_id"], scope, kind="qa", question=f"历史问题{n}")
        store.complete_turn(conv["conversation_id"], scope, n, answer={"blocks": []})
    turns = store.completed_context(conv["conversation_id"], scope, before=8, limit=3)
    assert [t["question"] for t in turns] == ["历史问题5", "历史问题6", "历史问题7"]
    with pytest.raises(ConversationError):
        store.completed_context(conv["conversation_id"], "other", before=8)


@pytest.mark.parametrize("mode", ["foreign-app", "preview", "foreign-owner"])
def test_formal_entry_rejects_other_context_before_dispatch(tmp_path, monkeypatch, mode):
    with environment(tmp_path, monkeypatch) as f:
        scope = f["resolver"].current.scope_fingerprint
        key = f"{f['route'].key}:{APP}:formal"
        if mode == "foreign-app":
            key = key.replace(APP, "other-app")
        elif mode == "preview":
            key = key.removesuffix("formal") + "preview"
        else:
            scope = "another-owner"
        conv = f["ctx"].conversation_store.create(
            scope_fingerprint=scope,
            business_entry="operator",
            agent_version_key="agt_test0000#v1",
            operator_binding=key,
        )
        before = len(f["state"]["calls"])
        reply = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "那电压呢", "conversation_id": conv["conversation_id"]},
        )
        assert reply.status_code == 404
        assert len(f["state"]["calls"]) == before


@pytest.mark.parametrize("question", ["那电压呢", "那为什么服务费折扣还不能判通过？"])
def test_revoked_order_is_not_replayed_and_new_topic_does_not_inherit(tmp_path, monkeypatch, question):
    with environment(tmp_path, monkeypatch) as f:
        calls = []

        def dispatch(self, config, run_id):
            calls.append(f["ctx"].agent_run_store.get(run_id).order_no)
            return node(f, run_id)

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post("/v1/assistant/questions", headers=HEADERS, json={"question": "电费", "order_no": ORDER})
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "completed"
        f["state"]["site"] = "OTHER-SITE"
        refused = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": question, "conversation_id": first["conversation_id"]},
        )
        assert refused.status_code == 202
        assert terminal(f, refused.json()["qa_id"]).json()["status"] == "failed"
        assert calls == ["", ""]
        assert not f["ctx"].conversation_store.get(
            first["conversation_id"], f["resolver"].current.scope_fingerprint
        )["is_generating"]


@pytest.mark.parametrize("question", ["那电压呢", "那为什么服务费折扣还不能判通过？"])
def test_native_trial_returns_and_reuses_conversation(tmp_path, monkeypatch, question):
    from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces

    with environment(tmp_path, monkeypatch) as f:
        f["settings"].environment = "env-a"
        f["settings"].dify_trial_api_key = "trial-only"
        with DifyWorkspaces(f["db"])._connection(write=True) as c:
            c.execute(
                "INSERT INTO dify_workspaces "
                "(tenant_id,environment,workspace_id,setup_state,created_at,updated_at) "
                "VALUES (?,?,?,'ready',?,?)",
                ("TENANT-A", "env-a", "workspace", "2026-11-04", "2026-11-04"),
            )
        monkeypatch.setattr(
            f["ctx"].operator_cpms,
            "console_identity",
            lambda session: {"token": "operator-private", "user_id": "OPERATOR-A", "tenant_id": "TENANT-A"},
        )
        seen = []

        def dispatch(self, config, run_id):
            result = node(f, run_id)
            seen.append(f["ctx"].agent_run_store.get(run_id).order_no)
            return result

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        headers = {"Authorization": "Bearer trial-only", "X-AIOps-Console-Session": "valid-session"}
        first = f["client"].post(
            "/v1/dify/trial", headers=headers, json={"app_id": APP, "order_no": ORDER, "question": "电费"}
        )
        assert first.status_code == 202, first.text
        assert terminal(f, first.json()["trial_id"]).json()["status"] == "completed"
        second = f["client"].post(
            "/v1/dify/trial",
            headers=headers,
            json={"app_id": APP, "question": question, "conversation_id": first.json()["conversation_id"]},
        )
        assert second.status_code == 202, second.text
        assert terminal(f, second.json()["trial_id"]).json()["status"] == "completed"
        assert second.json()["conversation_id"] == first.json()["conversation_id"]
        assert seen == [ORDER, ORDER]


def test_failed_turn_is_not_history_and_busy_turn_does_not_change_order(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", lambda self, cfg, run: node(f, run))
        first = (
            f["client"]
            .post("/v1/assistant/questions", headers=HEADERS, json={"question": "电费", "order_no": ORDER})
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "completed"
        store = f["ctx"].conversation_store
        scope = f["resolver"].current.scope_fingerprint
        turn = store.begin_turn(first["conversation_id"], scope, kind="qa", question="失败的问题")
        store.release_turn(first["conversation_id"], scope, turn)
        busy = store.begin_turn(first["conversation_id"], scope, kind="qa", question="正在生成")
        response = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "那电压呢", "conversation_id": first["conversation_id"]},
        )
        assert response.status_code == 409
        assert store.get(first["conversation_id"], scope)["active_order_no"] == ORDER
        assert store.recent_questions(first["conversation_id"], scope, before=busy) == ["电费"]


def test_late_completion_cannot_change_a_newer_turn_active_order(tmp_path):
    from aiops_diagnostics.conversation.conversation_store import ConversationStore

    store = ConversationStore(tmp_path / "race.db")
    c = store.create(
        scope_fingerprint="owner", business_entry="operator", agent_version_key="agt_test0000#v1"
    )
    first = store.begin_turn(c["conversation_id"], "owner", kind="qa", question="old")
    store.complete_turn(
        c["conversation_id"], "owner", first, answer={"blocks": []}, active_order_no="ORDER-A"
    )
    second = store.begin_turn(c["conversation_id"], "owner", kind="qa", question="new")
    store.complete_turn(
        c["conversation_id"], "owner", second, answer={"blocks": []}, active_order_no="ORDER-B"
    )
    store.complete_turn(
        c["conversation_id"], "owner", first, answer={"blocks": []}, active_order_no="ORDER-A"
    )
    assert store.get(c["conversation_id"], "owner")["active_order_no"] == "ORDER-B"


def test_explicit_order_disagreement_does_not_use_stale_order(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:

        def dispatch(_, cfg, run):
            from execution.test_m2_close_contract import NODE_A_TOKEN

            headers = {
                "Authorization": "Bearer " + NODE_A_TOKEN,
                "X-AI-Ops-Tool-Contract": operator_cpms.TOOL_CONTRACT,
            }
            pub = (
                f["client"]
                .post("/v1/internal/charging-status/execution-config/" + run, headers=headers)
                .json()
            )
            headers["X-AI-Ops-Exec-Config"] = pub["sha256"]
            denied = f["client"].post(
                f"/v1/internal/operator-runs/{run}/tools/cpms_order_detail/authorization",
                headers=headers,
                json={"arguments": {"order_no": "ORDER-1002"}},
            )
            assert denied.status_code == 403 and denied.json()["error"]["code"] == "OPERATOR_ORDER_CONFLICT"

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        response = f["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "分析订单 ORDER-1002 的电费", "order_no": ORDER},
        )
        assert response.status_code == 202
        assert terminal(f, response.json()["qa_id"]).json()["status"] == "failed"
        assert not f["state"]["calls"]


def test_preview_is_not_listed_or_read_as_a_formal_conversation(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        scope = f["resolver"].current.scope_fingerprint
        c = f["ctx"].conversation_store.create(
            scope_fingerprint=scope,
            business_entry="operator",
            agent_version_key="agt_test0000#v1",
            operator_binding=f"{f['route'].key}:{APP}:preview",
        )
        response = f["client"].get("/v1/conversations", headers=HEADERS)
        assert response.status_code == 200
        assert c["conversation_id"] not in response.text
        response = f["client"].get("/v1/conversations/" + c["conversation_id"], headers=HEADERS)
        assert response.status_code == 404


def test_new_charger_question_after_order_dispatches_without_order(tmp_path, monkeypatch):
    with environment(tmp_path, monkeypatch) as f:
        seen = []

        def dispatch(self, cfg, run_id):
            if not seen:
                result = node(f, run_id)
            else:
                result = node(
                    f, run_id, tools=(), answer={"reply": "您希望查询哪个站点？", "evidence_tools": []}
                )
            seen.append(f["ctx"].agent_run_store.get(run_id).order_no)
            return result

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post("/v1/assistant/questions", headers=HEADERS, json={"question": "电费", "order_no": ORDER})
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "completed"
        second = (
            f["client"]
            .post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={"question": "那还有空闲充电桩吗", "conversation_id": first["conversation_id"]},
            )
            .json()
        )
        assert terminal(f, second["qa_id"]).json()["status"] == "completed"
        assert seen == [ORDER, ""]
