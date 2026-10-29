"""真实Dify曾把中文资料直接复制给蒙古语用户，不能只断言language字段。"""

import json
import runpy
from pathlib import Path

import pytest
import yaml
from execution.test_app_runtime_dify_entry import HEADERS, dify_chat

from consumer.test_consumer_chat_service import done
from consumer.test_consumer_live_documents import document_fixture


def test_chinese_model_answer_is_not_delivered_as_mongolian(tmp_path, monkeypatch):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _, _, rows, _, _ = document_fixture(env, monkeypatch, tmp_path)
        rows.append(
            {
                "knowledge_base_id": "kb-own",
                "doc_id": "new",
                "chunk_id": "new",
                "docnm_kwd": "充电操作.md",
                "content": "请按设备提示启动。",
                "score": 0.95,
            }
        )
        response = env["client"].post(
            "/v1/assistant/questions",
            headers={**HEADERS, "Accept-Language": "mn"},
            json={"question": "怎么开始充电？"},
        )
        assert response.status_code == 202
        result = done(env, response.json()["qa_id"]).json()
        assert result["status"] == "failed"
        assert result.get("result") is None


def test_live_prompt_names_languages_and_repeats_preference_after_context():
    module = runpy.run_path("tools/upgrade_consumer_flow.py")
    source = yaml.safe_load(Path("deploy/dify-chat/consumer-service.dsl.yml").read_text())
    source = module["select_knowledge"](source, ["a" * 32], "trendpower/consumer_knowledge:0.0.1@" + "a" * 64)
    nodes = {n["id"]: n["data"] for n in module["live_documents"](source)["workflow"]["graph"]["nodes"]}
    prompt = nodes["answer"]["prompt_template"][0]["text"]
    assert "mn=Mongolian" in prompt and "th=Thai" in prompt and "km=Khmer" in prompt
    assert "{{#start.language#}}" in nodes["answer"]["memory"]["query_prompt_template"]


@pytest.mark.parametrize(
    "language,text",
    [
        ("mn", "Цэнэглэгчийн зааврыг дагана уу."),
        ("th", "โปรดทำตามคำแนะนำของเครื่องชาร์จ"),
        ("km", "សូមធ្វើតាមការណែនាំរបស់ឆ្នាំងសាក"),
    ],
)
def test_translated_answer_keeps_original_chinese_reference_name(language, text):
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    context = {
        "mode": "documents",
        "status": "found",
        "sources": [
            {
                "reference_id": "source-1",
                "retrieved_reference_id": "ref",
                "title": "充电操作.md",
                "content": "操作",
            }
        ],
    }
    result = final_answer(
        json.dumps({"kind": "knowledge", "answer": text, "reference_ids": ["source-1"]}),
        context,
        language,
        {},
    )
    assert result["blocks"][0]["text"] == text
    assert result["blocks"][1]["title"] == "充电操作.md"


def test_only_cited_resource_name_is_exempt_inside_translated_prose():
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer
    from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

    context = {
        "mode": "documents",
        "status": "found",
        "sources": [
            {
                "reference_id": "source-1",
                "retrieved_reference_id": "ref",
                "title": "充电操作.md",
                "content": "操作",
            },
            {
                "reference_id": "source-2",
                "retrieved_reference_id": "ref2",
                "title": "退款说明.md",
                "content": "退款",
            },
        ],
    }
    text = "Дэлгэрэнгүйг 充电操作.md файлаас үзнэ үү."
    raw = {"kind": "knowledge", "answer": text, "reference_ids": ["source-1"]}
    assert final_answer(json.dumps(raw), context, "mn", {})["blocks"][0]["text"] == text
    for bad in (
        text + " 未引用的中文。",
        "Дэлгэрэнгүйг 退款说明.md файлаас үзнэ үү.",
        "充电操作.md",
        "《充电操作.md》",
    ):
        with pytest.raises(ChatRunUnavailable, match="answer language differs"):
            final_answer(json.dumps({**raw, "answer": bad}), context, "mn", {})
