"""知识回调与答案必须同一轮，安全/人工不等待模型。"""

import json

import pytest
from test_app_runtime_dify_entry import HEADERS, dify_chat

from aiops_diagnostics.chat_data_grant import DataGrantStore
from aiops_diagnostics.chat_execution import ChatRunUnavailable
from aiops_diagnostics.consumer import consumer_knowledge
from aiops_diagnostics.dify_chatflow import ChatflowReply
from aiops_diagnostics.knowledge_retrieval import KnowledgeSearchUnavailable, MediaResourceSigner
from consumer.test_consumer_chat_service import Client, configure, done


class Knowledge:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.empty = False
        self.after = None

    def for_tenant(self, tenant):
        assert tenant == "TENANT-A"
        return self

    def search(self, ids, query, top_k):
        self.calls.append((ids, query, top_k))
        if self.after:
            self.after()
        if self.fail:
            raise KnowledgeSearchUnavailable("unavailable")
        if self.empty:
            return []
        return [
            {
                "kb_id": "kb-own",
                "doc_id": "doc-1",
                "chunk_id": "chunk-1",
                "docnm_kwd": "KB-C-007.md",
                "content_with_weight": "# KB-C-007\n按设备提示启动。",
                "score": 0.9,
            }
        ]


def setup(env, monkeypatch, tmp_path):
    configure(env, monkeypatch)
    body = {
        "schema": "aiops-consumer-reviewed/1",
        "state": "published",
        "tenant_id": "TENANT-A",
        "environment": "env-a",
        "entries": {
            "KB-C-007": {
                "state": "approved",
                "title": "开始充电",
                "answers": {"zh": "按设备提示启动。", "en": "Follow the charger instructions."},
            }
        },
        "support": {"zh": {"label": "官方客服", "url": "https://support.example.test/"}},
    }
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    d = json.loads(env["bindings"].read_text())
    d["bindings"][0].update(content_file=str(path), knowledge_base_ids=["kb-own"])
    env["bindings"].write_text(json.dumps(d))
    runtime = env["app"].state.gateway.runtime
    kb = Knowledge()
    runtime.kb_search_client = kb
    runtime.media_signer = MediaResourceSigner("s" * 32)
    return runtime, kb, path


class Workflow(Client):
    token = ""
    result = None
    extra = {}
    after = None
    env = None

    def run(self, **kwargs):
        import uuid

        self.calls.append(kwargs)
        ids = [str(uuid.uuid4()) for _ in range(4)]
        kwargs["on_started"](*ids)
        Workflow.token = kwargs["inputs"]["data_grant"]
        r = self.env["client"].post(
            "/v1/internal/consumer-knowledge",
            headers={"Authorization": "Bearer " + self.token},
            json={},
        )
        assert r.status_code == 200, r.text
        self.result = r.json()
        again = self.env["client"].post(
            "/v1/internal/consumer-knowledge",
            headers={"Authorization": "Bearer " + self.token},
            json={},
        )
        assert again.json() == r.json()
        if Workflow.after:
            Workflow.after()
        answer = (
            {"kind": "knowledge", "faq_id": "KB-C-007"}
            if r.json()["candidates"]
            else {"kind": r.json()["status"], "faq_id": ""}
        )
        return ChatflowReply(*ids, json.dumps(answer), {})


def ask(env):
    return env["client"].post("/v1/assistant/questions", headers=HEADERS, json={"question": "怎么开始充电？"})


def wire(env, monkeypatch):
    Workflow.env = env
    Workflow.after = None
    Workflow.extra = {}
    Workflow.token = ""
    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", Workflow)


def test_workflow_retrieves_once_and_returns_only_the_approved_answer(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)
        post = ask(env)
        assert post.status_code == 202, post.text
        result = done(env, post.json()["qa_id"])
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "completed", result.text
        assert result.json()["result"]["blocks"][0]["text"] == "按设备提示启动。"
        assert len(kb.calls) == 1 and kb.calls[0][0] == ("kb-own",)
        assert DataGrantStore(runtime.store.path).by_token(Workflow.token).state == "revoked"
        response = env["client"].post(
            "/v1/internal/consumer-knowledge",
            headers={"Authorization": "Bearer " + Workflow.token},
            json={"image_text": ""},
        )
        assert response.status_code == 401


@pytest.mark.parametrize("mode", ["empty", "fail"])
def test_missing_knowledge_and_unavailable_knowledge_are_distinct(tmp_path, monkeypatch, mode):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, _ = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)
        setattr(kb, mode, True)
        result = done(env, ask(env).json()["qa_id"]).json()["result"]
        assert result["retrieval_status"] == ("unavailable" if mode == "fail" else "not_found")


def test_read_cancelled_inside_search_is_not_released(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)

        def cancel():
            grant = DataGrantStore(runtime.store.path).by_token(Workflow.token)
            runtime.cancel_assistant_qa(
                __import__("test_app_runtime_node_entry")
                ._Resolver()
                .resolve("token-a", required_scope="aiops:diagnoses:write"),
                grant.qa_id,
            )

        kb.after = cancel
        result = done(env, ask(env).json()["qa_id"])
        assert result.json()["status"] == "cancelled"
        run = runtime.dify_run_store.by_qa(result.json()["qa_id"])
        assert runtime.dify_run_store.context(run.run_id) is None


def test_content_change_after_retrieval_refuses_answer(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, path = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)

        def change():
            path.write_text(path.read_text() + "\n")

        Workflow.after = change
        result = done(env, ask(env).json()["qa_id"]).json()
        assert result["status"] == "failed" and result.get("result") is None


def test_safety_and_human_return_before_dify_or_knowledge(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, _ = setup(env, monkeypatch, tmp_path)
        for text in ["充电桩冒烟了", "我要转人工客服"]:
            r = env["client"].post("/v1/assistant/questions", headers=HEADERS, json={"question": text})
            assert r.status_code == 200 and r.json()["type"] == "clarification"
            assert "https://support.example.test/" in r.json()["message"]
            assert "已转接" not in r.json()["message"]
        assert not Client.calls and not kb.calls


def test_unretrieved_faq_and_extra_model_claims_are_rejected():
    context = {
        "status": "found",
        "candidates": [{"faq_id": "KB-C-007", "answer": "已审核答案", "reference_id": "r1", "title": "操作"}],
    }
    for raw in [
        {"kind": "knowledge", "faq_id": "KB-C-009"},
        {"kind": "knowledge", "faq_id": "KB-C-007", "answer": "已经退款"},
        {"kind": "stop_session", "faq_id": ""},
    ]:
        with pytest.raises(ChatRunUnavailable):
            consumer_knowledge.final_answer(json.dumps(raw), context, "zh", {})


def test_other_grant_kind_cannot_read_knowledge_and_no_grant_is_rejected(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb, _ = setup(env, monkeypatch, tmp_path)
        token, _ = DataGrantStore(runtime.store.path).issue(
            scope_fingerprint="fake",
            order_no="ORDER-OTHER",
            environment="env-a",
            tenant_id="TENANT-A",
            qa_id="qa_other",
            workflow_id="11111111-2222-3333-4444-555555555555",
        )
        assert env["client"].post("/v1/internal/consumer-knowledge").status_code == 401
        response = env["client"].post(
            "/v1/internal/consumer-knowledge", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 403 and kb.calls == []


def test_draft_content_refuses_normal_qa_but_safety_still_works(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, kb, path = setup(env, monkeypatch, tmp_path)
        body = json.loads(path.read_text())
        body["state"] = "draft"
        path.write_text(json.dumps(body))
        assert ask(env).status_code == 503
        response = env["client"].post(
            "/v1/assistant/questions", headers=HEADERS, json={"question": "有火花，无法提供订单号"}
        )
        assert response.status_code == 200 and "远离" in response.json()["message"]
        assert Client.calls == [] and kb.calls == []


def test_same_path_changed_content_invalidates_completed_qa(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _, path = setup(env, monkeypatch, tmp_path)
        wire(env, monkeypatch)
        qa = ask(env).json()["qa_id"]
        assert done(env, qa).json()["status"] == "completed"
        path.write_text(path.read_text() + "\n")
        assert done(env, qa).status_code == 410
