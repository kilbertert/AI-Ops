"""复现同一中文问题仅因H5语言切换被拒，以及下游语言丢失。"""

import pytest
from execution.test_app_runtime_dify_entry import HEADERS, dify_chat

from aiops_diagnostics.common.i18n import SUPPORTED_LANGUAGES
from consumer.test_consumer_chat_service import Client, done
from consumer.test_consumer_live_context import ask, wire
from consumer.test_consumer_semantic_routing import semantic


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


def test_fixed_copy_preserves_placeholders_and_covers_all_other_languages():
    from string import Formatter

    from aiops_diagnostics.consumer.consumer_intent import message
    from aiops_diagnostics.consumer.consumer_knowledge import MESSAGES, text_for
    from aiops_diagnostics.consumer.consumer_language import COPY

    assert set(COPY) == set(SUPPORTED_LANGUAGES) - {"zh", "en"}
    expected = {
        # `status_code` 已从所有语种一起下线：正文不再把原始状态码摆给用户
        # （ADR-0181）。这条断言逐语种比对占位符集合，所以下线必须整齐。
        "status": {"order_no", "status", "source", "observed_at"},
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
    from aiops_diagnostics.consumer.consumer_billing import billing_answer
    from aiops_diagnostics.consumer.consumer_language import COPY
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

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
        # 订单号原样保留；来源**换成人话**（ADR-0181）——所以这里断言的是
        # 「读者还能认出这段数据来自哪条记录」，而不是内部标识字面值。
        # 时间改成人读格式（2026-10-28 00:00:00 UTC），比较的是可核对的时刻而不是原始 ISO 串。
        from aiops_diagnostics.consumer.consumer_plain import source_label

        # 走 JSON 模板的语种，标签在**该语种的语言包**里；zh/zh-Hant/en 走代码词表。
        expected_label = (
            COPY[language]["source_billing"]
            if language in COPY
            else source_label(language, snapshot["source"])
        )
        assert snapshot["order_no"] in text
        assert expected_label in text
        assert snapshot["source"] not in text, "内部接口标识不应出现在用户正文里"
        assert "2026-10-28 00:00:00" in text and "UTC" in text
        assert snapshot["observed_at"] not in text, "微秒 ISO 串不应直接摆给用户"
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

    module = runpy.run_path("tools/content/upgrade_consumer_flow.py")
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

    from aiops_diagnostics.consumer.consumer_knowledge import live_answer

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


@pytest.mark.parametrize("language,expected", [("th", "en"), ("km", "en"), ("zh-Hant", "zh")])
def test_legacy_reviewed_answer_reports_its_actual_copy_language(tmp_path, monkeypatch, language, expected):
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, "knowledge", "none")
        response = ask(env, "如何开始充电", headers={**HEADERS, "Accept-Language": language})
        assert response.status_code == 202
        answer = done(env, response.json()["qa_id"]).json()
        assert answer["status"] == "completed"
        assert answer["language"] == expected
        assert answer["result"]["blocks"][0]["text"] == (
            "按设备提示启动。" if expected == "zh" else "Follow the charger instructions."
        )


@pytest.mark.parametrize("configured", ["zh", "en", "fr"])
def test_support_route_survives_missing_translation_without_foreign_label(configured):
    from aiops_diagnostics.consumer.consumer_knowledge import fallback

    support = {
        configured: {
            "label": "assistance" if configured == "fr" else "foreign label",
            "url": "https://support.example.test/",
        }
    }
    answer = fallback("human", "fr", {"support": support})
    text = answer["blocks"][0]["text"]
    assert "https://support.example.test/" in text
    assert "foreign label" not in text


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_order_shortcut_uses_business_workflow_language(tmp_path, monkeypatch, language):
    from execution.test_app_runtime_dify_entry import ORDER_A

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        semantic(runtime, monkeypatch, "order_status", "explicit")
        response = env["client"].post(
            "/v1/assistant/questions",
            headers={**HEADERS, "Accept-Language": language},
            json={
                "question": "帮我检测这次充电",
                "order_no": ORDER_A,
                "shortcut_code": "smart_diagnosis",
            },
        )
        assert response.status_code == 202, response.text
        final = done(env, response.json()["qa_id"]).json()
        assert final["status"] == "completed", final
        assert final["language"] == language
        assert Client.calls[-1]["inputs"]["language"] == language
        assert env["upstream"].calls == 1
