"""复现同一中文问题仅因H5语言切换被拒，以及下游语言丢失。"""

import pytest
from test_app_runtime_dify_entry import HEADERS, dify_chat
from test_consumer_chat_service import Client, done
from test_consumer_live_context import ask, wire
from test_consumer_semantic_routing import semantic

from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_wf01_accepts_every_declared_language(tmp_path, monkeypatch, language):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, kb = wire(env, monkeypatch, tmp_path)
        calls = semantic(runtime, monkeypatch)
        response = ask(env, "帮我查一下上一个订单", headers={**HEADERS, "Accept-Language": language})
        assert response.status_code == 202, response.text
        assert response.json()["language"] == language
        final = done(env, response.json()["qa_id"]).json()
        assert final["status"] == "completed", final
        assert final["language"] == language
        assert calls[0][1]["language"] == language
        assert Client.calls[-1]["inputs"]["language"] == language
        assert env["upstream"].calls == 1 and not kb.calls


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_order_shortcut_keeps_diagnosis_and_selected_language(tmp_path, language):
    from test_assistant_api import _client, _headers

    client, runtime = _client(tmp_path)
    headers = {**_headers(), "Accept-Language": language}
    created = client.post(
        "/v1/shortcuts",
        headers=headers,
        json={
            "business_entry": "consumer",
            "code": "smart_diagnosis",
            "intent": "order_issue",
            "requires_order": True,
            "labels": {"zh": "智能检测"},
        },
    )
    assert created.status_code == 201
    shortcut = created.json()
    assert (
        client.post(
            f"/v1/shortcuts/{shortcut['shortcut_id']}/publish",
            headers=headers,
            json={"expected_revision": shortcut["revision"]},
        ).status_code
        == 200
    )
    payload = {
        "question": "帮我检测（2096164064667852801）这个订单的充电异常",
        "shortcut_code": "smart_diagnosis",
    }
    response = client.post("/v1/assistant/questions", headers=headers, json=payload)
    assert response.status_code == 202, response.text
    assert response.json()["type"] == "diagnosis"
    assert response.json()["language"] == runtime.stored_language == language
    assert len(runtime.calls) == 1 and not runtime.qa_calls
    denied = client.post(
        "/v1/assistant/questions",
        headers=headers,
        json={
            **payload,
            "question": "检测另一笔订单",
            "order_no": "2096164064667852999",
        },
    )
    assert denied.status_code == 404
    assert len(runtime.calls) == 1


def test_fixed_copy_preserves_placeholders_and_covers_all_other_languages():
    from string import Formatter

    from aiops_diagnostics.consumer_intent import message
    from aiops_diagnostics.consumer_knowledge import MESSAGES, text_for
    from aiops_diagnostics.consumer_language import COPY

    assert set(COPY) == set(SUPPORTED_LANGUAGES) - {"zh", "en"}
    expected = {
        "status": {"order_no", "status", "status_code", "source", "observed_at"},
        "billing": {
            "order_no",
            "order_amount",
            "recorded_payment_amount",
            "refund",
            "recorded_refund_amount",
            "source",
            "observed_at",
        },
    }
    for language, pack in COPY.items():
        assert set(pack) == set(COPY["mn"])
        for key, value in pack.items():
            assert value.strip()
            fields = {f for _, f, _, _ in Formatter().parse(value) if f is not None}
            assert fields == expected.get(key, set()), (language, key)
        for key in MESSAGES["en"]:
            assert text_for(key, language) == pack["knowledge_" + key]
        for key in (
            "unavailable",
            "clarify",
            "context",
            "write_request",
            "trial_order",
            "order",
            "disabled",
            "conversation",
        ):
            assert message(key, language) == pack["intent_" + key]


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_fact_rendering_keeps_zero_amounts_unknown_status_and_identifiers(language):
    from aiops_diagnostics.consumer_billing import billing_answer
    from aiops_diagnostics.consumer_language import COPY
    from aiops_diagnostics.consumer_order_chat import status_answer

    snapshot = {
        "order_no": "2096164064667852801",
        "source": "cpms.orderDetail",
        "observed_at": "2026-10-28T00:00:00+00:00",
        "status_role": "unknown",
        "status_code": 999,
        "order_amount": "0",
        "recorded_payment_amount": "12.30",
        "refund_status": None,
        "recorded_refund_amount": None,
    }
    for answer in (status_answer(snapshot, language, localized=True), billing_answer(snapshot, language)):
        text = answer["blocks"][0]["text"]
        assert all(snapshot[k] in text for k in ("order_no", "source", "observed_at"))
    bill = billing_answer(snapshot, language)["blocks"][0]["text"]
    assert "12.30" in bill and "0" in bill
    if language in COPY:
        assert COPY[language]["refund_unknown"] in bill
        assert COPY[language]["missing"] in bill
        assert (
            COPY[language]["unknown"]
            in status_answer(snapshot, language, localized=True)["blocks"][0]["text"]
        )


def test_live_flow_upgrade_carries_all_languages_and_explicit_answer_language():
    import runpy
    from pathlib import Path

    import yaml

    module = runpy.run_path("tools/upgrade_consumer_flow.py")
    live_documents, select_knowledge = module["live_documents"], module["select_knowledge"]

    source = yaml.safe_load(Path("deploy/dify-chat/consumer-service.dsl.yml").read_text())
    source = select_knowledge(source, ["a" * 32], "trendpower/consumer_knowledge:0.0.1@" + "a" * 64)
    result = live_documents(source)
    nodes = {n["id"]: n["data"] for n in result["workflow"]["graph"]["nodes"]}
    lang = next(v for v in nodes["start"]["variables"] if v["variable"] == "language")
    assert set(lang["options"]) == set(SUPPORTED_LANGUAGES)
    assert "{{#start.language#}}" in nodes["answer"]["prompt_template"][0]["text"]


@pytest.mark.parametrize("language", ["mn", "th", "zh-Hant"])
def test_older_context_keeps_original_rendering_on_readback(tmp_path, monkeypatch, language):
    import json
    import sqlite3

    from aiops_diagnostics.consumer_knowledge import live_answer

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch)
        response = ask(env, headers={**HEADERS, "Accept-Language": language}).json()
        qa_id = response["qa_id"]
        assert done(env, qa_id).json()["status"] == "completed"
        run = runtime.dify_run_store.by_qa(qa_id)
        context = runtime.dify_run_store.context(run.run_id)
        context.pop("answer_language_version")
        old = live_answer(context["live_snapshot"], language, localized=False)
        # 模拟升级前已经保存的旧模板正文；不可因此变成503或重写正文。
        with sqlite3.connect(env["db"]) as db:
            db.execute(
                "UPDATE chat_dify_runs SET context_json=?, snapshot_json=? WHERE run_id=?",
                (json.dumps(context), json.dumps(old), run.run_id),
            )
            db.execute("UPDATE assistant_questions SET result_json=? WHERE qa_id=?", (json.dumps(old), qa_id))
        result = done(env, qa_id)
        assert result.status_code == 200, result.text
        assert result.json()["result"] == old
