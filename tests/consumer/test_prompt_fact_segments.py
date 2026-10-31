"""新版事实段的正例、边界和多语言必须一起验证。"""

import pytest

from aiops_diagnostics.consumer.consumer_facts import facts_for
from aiops_diagnostics.consumer.consumer_language import COPY
from aiops_diagnostics.consumer.consumer_reply import model_reply, validate_reply
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

SNAPSHOT = {
    "order_no": "123456789012345",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T00:00:00Z",
    "order_amount": "27.36",
    "recorded_payment_amount": "20",
    "recorded_refund_amount": "0",
    "refund_status": "0",
    "status_code": "1",
    "status_role": "finished",
}


@pytest.mark.parametrize("language", ["fr", "de", "th"])
def test_segments_use_existing_language_and_evidence(language):
    facts = facts_for(SNAPSHOT, language)
    assert COPY[language]["finished"] in facts["status_line"]
    answer = model_reply("{{fact:billing_line}}", SNAPSHOT, language=language, contract="slots")
    text = answer["blocks"][0]["text"]
    for value in ("123456789012345", "27.36", "20", COPY[language]["source_billing"]):
        assert value in text
    assert "CNY" not in text


@pytest.mark.parametrize(
    "text",
    [
        "合计27.36，付款20，退款0。订单123456789012345。",
        "这单并非{{fact:status_line}}",
        "每小时{{fact:billing_line}}元",
        "{{fact:order_line}}",
    ],
)
def test_new_contract_requires_complete_standalone_fact_segments(text):
    with pytest.raises(ChatRunUnavailable):
        validate_reply(text, SNAPSHOT, language="zh", contract="slots")


def test_style_surrounds_a_fact_segment_without_rewriting_it():
    text = "我帮你核对了一下。\n{{fact:billing_line}}\n可以再告诉我你想了解哪一项。"
    answer = model_reply(text, SNAPSHOT, language="zh", contract="slots")
    assert answer["blocks"][0]["text"].startswith("我帮你核对了一下。")
    assert "27.36" in answer["blocks"][0]["text"]


@pytest.mark.parametrize(
    "text",
    [
        "这笔订单没有结束，还在充电。",
        "这单未结束。",
        "The order has not finished and is still charging.",
        "Der Ladevorgang ist nicht abgeschlossen.",
        "La recharge n'est pas terminée.",
        "ยังไม่เสร็จสิ้น",
    ],
)
def test_free_prose_cannot_reverse_the_status_segment(text):
    with pytest.raises(ChatRunUnavailable):
        validate_reply(text + "\n{{fact:billing_line}}", SNAPSHOT, language="zh", contract="slots")


def test_live_segments_survive_readback_and_reject_changed_cached_text(tmp_path, monkeypatch):
    import json
    import sqlite3

    from execution.test_app_runtime_dify_entry import dify_chat
    from execution.test_app_runtime_node_entry import ORDER_A

    from aiops_diagnostics.consumer import consumer_chat_service
    from aiops_diagnostics.dify.dify_chatflow import ChatflowReply
    from consumer.test_consumer_live_context import Flow, ask, done, wire

    class SegmentFlow(Flow):
        def run(self, **kwargs):
            response = super().run(**kwargs)
            body = {
                "kind": "realtime",
                "answer": "",
                "reference_ids": [],
                "reply": "我帮你确认了一下。\n{{fact:status_line}}",
            }
            return ChatflowReply(
                response.workflow_run_id,
                response.task_id,
                response.conversation_id,
                response.message_id,
                json.dumps(body),
                {},
            )

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        doc = json.loads(env["bindings"].read_text())
        doc["bindings"][0]["callback_version"] = 5
        env["bindings"].write_text(json.dumps(doc))
        monkeypatch.setattr(consumer_chat_service, "DifyChatflowClient", SegmentFlow)
        qa = ask(env).json()["qa_id"]
        final = done(env, qa)
        assert final.status_code == 200 and final.json()["status"] == "completed", final.text
        assert ORDER_A in final.json()["result"]["blocks"][0]["text"]
        assert done(env, qa).json()["result"] == final.json()["result"]
        run = runtime.dify_run_store.by_qa(qa)
        tampered = final.json()["result"]
        tampered["blocks"][0]["text"] = "不是这次查询结果"
        with sqlite3.connect(env["db"]) as db:
            db.execute(
                "UPDATE assistant_questions SET result_json=? WHERE qa_id=?", (json.dumps(tampered), qa)
            )
            db.execute(
                "UPDATE chat_dify_runs SET snapshot_json=? WHERE run_id=?", (json.dumps(tampered), run.run_id)
            )
        assert done(env, qa).status_code != 200


# --- 第四轮复核的四个实际反例（先失败，再修） ---------------------------------


#: 状态轮的快照形状（`cpms.chargingInfo`）：没有金额字段，只有状态与时间。
STATUS_SNAPSHOT = {
    "order_no": "123456789012345",
    "source": "cpms.chargingInfo",
    "observed_at": "2026-10-30T00:00:00Z",
    "status_code": "1",
    "status_role": "finished",
}


def test_a_negated_state_prose_cannot_contradict_the_evidence():
    """A. 快照说结束，旁白写"尚未完结，仍在供电"必须拒绝。

    判据不是"再列几个同义词"，而是**否定词 × 状态域名词**：旁白是否定或改写状态时，
    无论用哪个词形，都在这里的两个维度上撞到。
    """
    text = "这笔订单尚未完结，仍在供电。\n{{fact:status_line}}"
    with pytest.raises(ChatRunUnavailable):
        model_reply(text, STATUS_SNAPSHOT, language="zh", contract="slots")


def test_b_a_normal_english_follow_up_survives():
    """B. 英文正常追问不属于状态断言，必须保留。"""
    text = "{{fact:status_line}}\nDo you have any other questions about charging?"
    answer = model_reply(text, STATUS_SNAPSHOT, language="en", contract="slots")
    assert "other questions" in answer["blocks"][0]["text"]


def test_d_an_inline_prefix_on_a_fact_segment_is_refused():
    """D. `账单内容：{{fact:billing_line}}` 违反独立行协议，必须拒绝。

    生成契约（协议块）与消费契约必须一致：片段单独占一行、不得加前后缀。
    """
    from aiops_diagnostics.consumer.consumer_documents import _protocol_block

    assert "每个片段单独占一行，不得添加前后缀" in _protocol_block()
    with pytest.raises(ChatRunUnavailable):
        model_reply("账单内容：{{fact:billing_line}}", SNAPSHOT, language="zh", contract="slots")
