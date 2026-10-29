"""新资料、正文更新和视频不再依赖冻结审核副本。"""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from test_app_runtime_dify_entry import dify_chat

from aiops_diagnostics.chat_execution import ChatRunUnavailable
from aiops_diagnostics.consumer.consumer_documents import SCHEMA
from aiops_diagnostics.consumer.consumer_knowledge import final_answer
from aiops_diagnostics.dify_chatflow import ChatflowReply
from consumer.test_consumer_chat_service import Client, done
from consumer.test_consumer_knowledge_flow import ask, setup


def document_fixture(env, monkeypatch, tmp_path):
    runtime, kb, content = setup(env, monkeypatch, tmp_path)
    content.write_text(
        json.dumps({"schema": SCHEMA, "state": "published", "tenant_id": "TENANT-A", "environment": "env-a"})
    )
    doc = json.loads(env["bindings"].read_text())
    doc["bindings"][0]["callback_version"] = 4
    env["bindings"].write_text(json.dumps(doc))
    monkeypatch.setattr(kb, "list_knowledge_bases", lambda: [{"knowledge_base_id": "kb-own"}], raising=False)
    rows = []
    monkeypatch.setattr(kb, "search", lambda *args: rows.copy())
    received = []

    class Flow(Client):
        def run(self, **kwargs):
            assert kwargs["conversation_id"] == ""
            ids = [str(uuid.uuid4()) for _ in range(4)]
            kwargs["on_started"](*ids)
            response = env["client"].post(
                "/v1/internal/consumer-data/knowledge",
                headers={"Authorization": "Bearer " + kwargs["inputs"]["data_grant"]},
                json={"knowledge_base_ids": ["kb-own"]},
            )
            assert response.status_code == 200, response.text
            context = response.json()
            received.append(context)
            source = next(iter(context["sources"]), None)
            return ChatflowReply(
                *ids,
                json.dumps(
                    {
                        "kind": "knowledge" if source else "not_found",
                        "answer": source["content"].split("\n")[0] if source else "",
                        "reference_ids": [source["reference_id"]] if source else [],
                    }
                ),
                {},
            )

    monkeypatch.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", Flow)
    return runtime, kb, rows, content, received


def test_add_update_delete_indexed_document_without_content_edit_or_publish(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _, rows, content, received = document_fixture(env, monkeypatch, tmp_path)
        original = content.read_bytes(), env["bindings"].read_bytes()
        assert done(env, ask(env).json()["qa_id"]).json()["result"]["retrieval_status"] == "not_found"
        rows.append(
            {
                "knowledge_base_id": "kb-own",
                "doc_id": "new",
                "chunk_id": "chunk-new",
                "docnm_kwd": "印尼兼容性说明.md",
                "content": "协议兼容性需要测试确认。",
                "score": 0.95,
            }
        )
        first = ask(env).json()
        result = done(env, first["qa_id"]).json()["result"]
        assert result["blocks"][0]["text"] == "协议兼容性需要测试确认。"
        assert result["blocks"][1]["title"] == "印尼兼容性说明.md"
        rows[0]["content"] = "更新后的协议集成说明。"
        from test_app_runtime_dify_entry import HEADERS

        followup = env["client"].post(
            "/v1/assistant/questions",
            headers=HEADERS,
            json={"question": "再说明一下", "conversation_id": first["conversation_id"]},
        )
        assert followup.status_code == 202, followup.text
        assert done(env, followup.json()["qa_id"]).json()["result"]["blocks"][0]["text"] == rows[0]["content"]
        assert done(env, ask(env).json()["qa_id"]).json()["result"]["blocks"][0]["text"] == rows[0]["content"]
        rows.clear()
        assert done(env, ask(env).json()["qa_id"]).json()["result"]["retrieval_status"] == "not_found"
        assert (content.read_bytes(), env["bindings"].read_bytes()) == original
        assert all(item["mode"] == "documents" for item in received)
        assert env["upstream"].calls == 0


def test_document_video_and_image_reach_blocks_without_media_registry(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _, rows, _, _ = document_fixture(env, monkeypatch, tmp_path)
        video = "https://www.youtube.com/shorts/t57PMqSw7iE"
        rows.append(
            {
                "knowledge_base_id": "kb-own",
                "doc_id": "doc-v",
                "chunk_id": "chunk-v",
                "docnm_kwd": "KB-C-059.md",
                "content": "印尼市场充电桩软件兼容性介绍。\n观看：" + video,
                "score": 0.95,
                "image_id": "image-from-document",
                "mime_type": "image/png",
            }
        )
        post = ask(env).json()
        result = done(env, post["qa_id"]).json()["result"]
        assert [b["kind"] for b in result["blocks"]] == ["text", "reference", "image", "video"]
        media = result["blocks"][-1]["media"]
        assert media["url"] == video
        image = result["blocks"][-2]["media"]
        grant = runtime.media_signer.verify_signed(image["url"].rsplit("/", 1)[1])
        assert grant.agent_version.startswith("consumer:") and grant.session_id == post["qa_id"]
        assert "media_ids" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["reference", "url", "missing", "empty", "invented"])
def test_answer_requires_current_source_and_cannot_generate_media(mutation):
    context = {
        "mode": "documents",
        "status": "found",
        "sources": [
            {
                "reference_id": "source-1",
                "content": "说明",
                "title": "资料",
                "media": [],
            }
        ],
    }
    value = {"kind": "knowledge", "answer": "简短解释", "reference_ids": ["source-1"]}
    if mutation == "reference":
        value["reference_ids"] = ["foreign"]
    if mutation == "url":
        value["answer"] = "访问 https://invented.test/refund"
    if mutation == "missing":
        context["sources"] = []
    if mutation == "empty":
        value["reference_ids"] = []
    if mutation == "invented":
        value["media"] = [{"url": "https://invented.test"}]
    with pytest.raises(ChatRunUnavailable):
        final_answer(json.dumps(value), context, "zh", {})


def test_live_publication_ignores_missing_reviewed_file_and_snapshot_contains_no_faq(tmp_path):
    from aiops_diagnostics.consumer.consumer_flow_contract import publication_settings
    from aiops_diagnostics.consumer.consumer_publication import frozen_settings
    from consumer.test_consumer_flow_contract import BASE, CEILING
    from consumer.test_consumer_knowledge_picker import picker_graph

    spec = importlib.util.spec_from_file_location(
        "upgrade", Path(__file__).parents[2] / "tools/upgrade_consumer_flow.py"
    )
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    flow = upgrade.live_documents({"workflow": picker_graph(["kb-own"])})["workflow"]
    target = {
        "tenant_id": "TENANT-A",
        "environment": "env-a",
        "content_file": str(tmp_path / "absent.json"),
        "knowledge_base_ids": ["old"],
        **CEILING,
    }
    frozen = frozen_settings(target, documents=True)
    result = publication_settings(flow, frozen, BASE, owned_libraries=[{"knowledge_base_id": "kb-own"}])
    assert result["callback_version"] == 4 and result["knowledge_base_ids"] == ["kb-own"]
    assert set(result["content"]) == {"schema", "state", "tenant_id", "environment"}
    assert result["content"]["schema"] == SCHEMA


@pytest.mark.parametrize("kind", ["status", "billing"])
def test_document_mode_cannot_replace_cpms_snapshot_with_model_prose(kind):
    from aiops_diagnostics.consumer.consumer_knowledge import live_answer

    snapshot = {
        "order_no": "order-a",
        "status_code": 1,
        "status_text": "finished",
        "status_role": "finished",
        "refund_status": "0",
        "order_amount": "2.00",
        "recorded_payment_amount": "2.00",
        "recorded_refund_amount": "0.00",
        "observed_at": "2026-10-27T01:00Z",
        "source": "cpms.chargingInfo" if kind == "status" else "cpms.orderDetail",
    }
    context = {"mode": "documents", "status": "live", "live_snapshot": snapshot}
    raw = json.dumps({"kind": "realtime", "answer": "", "reference_ids": []})
    assert final_answer(raw, context, "zh", {}) == live_answer(snapshot, "zh")
    with pytest.raises(ChatRunUnavailable):
        final_answer(
            json.dumps({"kind": "realtime", "answer": "已经退款", "reference_ids": []}), context, "zh", {}
        )


def test_document_media_never_accepts_credentials_or_plaintext_urls():
    from aiops_diagnostics.knowledge_retrieval import MediaResourceSigner, normalize_search_response

    for url in [
        "http://www.youtube.com/shorts/t57PMqSw7iE",
        "https://secret:token@www.youtube.com/shorts/t57PMqSw7iE",
    ]:
        rows = normalize_search_response(
            [
                {
                    "knowledge_base_id": "kb-own",
                    "doc_id": "doc",
                    "chunk_id": "chunk",
                    "content": "视频：" + url,
                }
            ],
            tenant_id="tenant",
            agent_version="v1",
            session_id="qa",
            knowledge_base_ids=("kb-own",),
            media_signer=MediaResourceSigner("x" * 32),
        )
        assert rows and rows[0].media == ()


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "[]",
        '{"kind":[],"answer":"","reference_ids":[]}',
        '{"kind":"knowledge","answer":3,"reference_ids":[]}',
    ],
)
def test_invalid_model_document_output_is_refused(value):
    with pytest.raises(ChatRunUnavailable):
        final_answer(value, {"mode": "documents", "sources": [], "status": "found"}, "zh", {})


def test_only_cited_document_media_and_original_reference_are_delivered():
    from aiops_diagnostics.consumer.consumer_documents import answer_selection

    media = {"kind": "video", "url": "https://youtu.be/t57PMqSw7iE", "resource_id": "media-real"}
    context = {
        "mode": "documents",
        "status": "found",
        "sources": [
            {
                "reference_id": "source-1",
                "retrieved_reference_id": "real-chunk-1",
                "title": "资料A",
                "media": [media, media],
            },
            {
                "reference_id": "source-2",
                "retrieved_reference_id": "real-chunk-2",
                "title": "资料B",
                "media": [media],
            },
        ],
    }
    _, result = answer_selection(
        json.dumps({"kind": "knowledge", "answer": "说明", "reference_ids": ["source-1"]}), context
    )
    assert [b["kind"] for b in result["blocks"]] == ["text", "reference", "video"]
    assert result["blocks"][1]["reference_id"] == "real-chunk-1"
    assert "资料B" not in str(result)


def test_answer_may_repeat_only_links_from_its_cited_document():
    from aiops_diagnostics.consumer.consumer_documents import answer_selection

    video = "https://www.youtube.com/shorts/t57PMqSw7iE"
    context = {
        "mode": "documents",
        "status": "found",
        "sources": [
            {
                "reference_id": "source-1",
                "retrieved_reference_id": "real-chunk",
                "title": "资料",
                "content": "观看：" + video,
                "media": [],
            },
            {
                "reference_id": "source-2",
                "retrieved_reference_id": "other-chunk",
                "title": "其他",
                "content": "其他链接：https://example.org/other",
                "media": [],
            },
        ],
    }
    value = {"kind": "knowledge", "answer": "观看：[视频](" + video + ")", "reference_ids": ["source-1"]}
    _, answer = answer_selection(json.dumps(value), context)
    assert video in answer["blocks"][0]["text"]
    for prose in ["观看 (" + video + ")", "观看（" + video + "）"]:
        _, answer = answer_selection(json.dumps({**value, "answer": prose}), context)
        assert video in answer["blocks"][0]["text"]
    for url in [
        video + "?invented=1",
        "https://example.org/other",
        "http://www.youtube.com/shorts/t57PMqSw7iE",
        video + "(unverified)",
        "https://www.youtube.com(@evil.example)/shorts/t57PMqSw7iE",
        "[视频](" + video + "(unverified))",
    ]:
        with pytest.raises(ChatRunUnavailable):
            answer_selection(json.dumps({**value, "answer": url}), context)
