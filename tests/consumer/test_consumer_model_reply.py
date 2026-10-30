"""编排页的提示词可以写正文：`reply` 字段的接受、校验与回读。

产品诉求：**在 Dify 编排页改提示词就能调整客户端的说话方式**。此前做不到，因为
`answer` 节点按 `{kind, faq_id}` 严格解析，模型写的字只用于分类，正文由代码按快照渲染。

取舍见 ADR-0187；契约见 `docs/acceptance/consumer-model-reply/acceptance.feature`。
"""

from __future__ import annotations

import json

import pytest

from aiops_diagnostics.consumer.consumer_reply import MAX_REPLY_CHARS, validate_reply
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

SNAPSHOT = {
    "order_no": "2107546575906942977",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T09:08:26+00:00",
    "order_amount": "2.4",
    "recorded_payment_amount": "2.40",
    "recorded_refund_amount": "0",
    "refund_status": "0",
    "status_code": "1",
    "status_role": "finished",
}

GOOD = "这单账单合计 2.4，账上记的付款 2.40，没有退款（已退 0）。订单号 2107546575906942977。"


def test_a_reply_that_carries_the_evidence_is_accepted():
    assert validate_reply(GOOD, SNAPSHOT, language="zh") == GOOD


@pytest.mark.parametrize(
    "text,reason",
    [
        # 漏一个金额：读者会以为那项不存在。
        ("这单合计 2.4，付款 2.40。订单号 2107546575906942977。", "recorded_refund_amount"),
        # 编一个金额：即使该有的都在，多出来的数也是编的。
        # 这里的理由从 `not in the evidence` 换成了标签绑定 —— 数字 2.5 挂在「合计」上，
        # 而快照的合计是 2.4。这是**更具体**的判据，不是更松的判据：旧判据只能说出
        # 「这个数没出处」，新判据能说出「你把哪个值说成了什么」。
        (
            "这单合计 2.5，账上记的付款 2.40，已退 0。订单号 2107546575906942977。",
            "CHATFLOW_FACT_LABEL_MISMATCH",
        ),
        # 数据里没有的时长。
        ("约 47 分钟充满，合计 2.4 / 2.40 / 0。订单号 2107546575906942977。", "not in the evidence"),
        # 内部标识。
        ("合计 2.4 / 2.40 / 0，来源 cpms.orderDetail，订单号 2107546575906942977。", "internal identifiers"),
        ("合计 2.4 / 2.40 / 0，status_code=1。订单号 2107546575906942977。", "internal identifiers"),
        # 缺订单号：读者无法核对是哪一单。
        ("合计 2.4 / 2.40 / 0，没有退款。", "order number"),
        ("   ", "empty"),
        (None, "empty"),
    ],
)
def test_replies_that_break_the_evidence_are_refused(text, reason):
    with pytest.raises(ChatRunUnavailable) as excinfo:
        validate_reply(text, SNAPSHOT, language="zh")
    if reason.startswith("CHATFLOW_"):
        assert excinfo.value.code == reason
    else:
        assert reason in str(excinfo.value)


def test_overlong_reply_is_refused():
    text = GOOD + "补" * MAX_REPLY_CHARS
    with pytest.raises(ChatRunUnavailable) as excinfo:
        validate_reply(text, SNAPSHOT, language="zh")
    assert "too long" in str(excinfo.value)


def test_small_counts_are_allowed_but_money_is_not():
    """「第 2 条」「3 步」这类计数不构成对金额的断言；金额必须逐字来自快照。"""
    assert validate_reply(
        "这单合计 2.4，付款 2.40，已退 0，共 3 项。订单号 2107546575906942977。",
        SNAPSHOT,
        language="zh",
    )
    with pytest.raises(ChatRunUnavailable):
        validate_reply(
            "这单合计 2.4，付款 2.40，已退 0，另有 47 元优惠。订单号 2107546575906942977。",
            SNAPSHOT,
            language="zh",
        )


# ── 端到端：提示词写的正文真的会交付，回读也仍然成立 ────────────────────────


def _wire_with_reply(tmp_path, monkeypatch, reply):
    """让 chatflow 桩在 realtime 分支带回一段 reply。"""
    from aiops_diagnostics.dify.dify_chatflow import ChatflowReply
    from consumer.test_consumer_live_context import Flow, wire

    class ReplyFlow(Flow):
        def run(self, **kwargs):
            base = super().run(**kwargs)
            context_status = None
            try:
                payload = json.loads(base.answer)
                if payload.get("kind") == "realtime":
                    context_status = reply
            except (TypeError, ValueError):
                pass
            if context_status is None:
                return base
            return ChatflowReply(
                workflow_run_id=base.workflow_run_id,
                task_id=base.task_id,
                conversation_id=base.conversation_id,
                message_id=base.message_id,
                answer=json.dumps({"kind": "realtime", "faq_id": "", "reply": reply}),
                metadata={},
            )

    def setup(env, mp, tmp):
        runtime, kb = wire(env, mp, tmp)
        # 必须**在 wire 之后**覆盖：wire 自己会把客户端换成 Flow。
        mp.setattr("aiops_diagnostics.consumer.consumer_chat_service.DifyChatflowClient", ReplyFlow)
        return runtime, kb

    return setup


def test_prompt_written_reply_is_delivered_and_survives_readback(tmp_path, monkeypatch):
    from execution.test_app_runtime_dify_entry import dify_chat
    from execution.test_app_runtime_node_entry import ORDER_A

    from consumer.test_consumer_live_context import ask, done

    # 状态必须与快照同向：桩返回 status=0（充电中），正文就不能说「已经结束」。
    # 这句在引入状态方向判据之前是「合格正文」，而它与快照是矛盾的 —— 这正是该判据存在的理由。
    reply = f"这单还在充电中。订单号 {ORDER_A}，账上记的支付金额与订单金额都按原单页面对，没有退款记录。"
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _wire_with_reply(tmp_path, monkeypatch, reply)(env, monkeypatch, tmp_path)
        first = ask(env).json()
        final = done(env, first["qa_id"]).json()
        assert final["status"] == "completed", final
        assert final["result"]["blocks"][0]["text"] == reply
        # 回读：模型写的正文无法逐字重算，但必须仍是这条快照的合法答复。
        again = done(env, first["qa_id"])
        assert again.status_code == 200, again.text
        assert again.json()["result"]["blocks"][0]["text"] == reply


def test_readback_refuses_a_reply_whose_numbers_do_not_match(tmp_path, monkeypatch):
    import sqlite3

    from execution.test_app_runtime_dify_entry import dify_chat
    from execution.test_app_runtime_node_entry import ORDER_A

    from consumer.test_consumer_live_context import ask, done

    reply = f"这单还在充电中。订单号 {ORDER_A}，没有退款记录。"
    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        _wire_with_reply(tmp_path, monkeypatch, reply)(env, monkeypatch, tmp_path)
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        # 把库里正文改成编了一个数字的版本：不能交出去。
        tampered = {
            "blocks": [{"kind": "text", "text": f"这单多收了 47 元。订单号 {ORDER_A}。"}],
            "retrieval_status": "not_found",
        }
        # 两处都改：只改 assistant_questions 会被"正文 == 快照"那条拦下，
        # 那条与本片的容差无关。两处都改成同一份，才真正测到"模型正文的证据校验"。
        with sqlite3.connect(env["db"]) as db:
            db.execute(
                "UPDATE assistant_questions SET result_json=? WHERE qa_id=?",
                (json.dumps(tampered), first["qa_id"]),
            )
            db.execute(
                "UPDATE chat_dify_runs SET snapshot_json=? WHERE qa_id=?",
                (json.dumps(tampered), first["qa_id"]),
            )
        refused = done(env, first["qa_id"])
        assert refused.status_code != 200
        assert "多收了" not in refused.text


def test_reply_without_live_data_is_refused():
    """没有实时快照时，**知识类**答复不许由模型写正文。

    否则一段无取数依据的话会被当成知识库回答交付。澄清／无结果／转人工是例外：那三类
    本来就没有证据可引，判据换成「一个数字都不许出现」（`test_notice_is_written_by_the_prompt`）。
    """
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    with pytest.raises(ChatRunUnavailable) as excinfo:
        final_answer(
            json.dumps({"kind": "knowledge", "faq_id": "", "reply": "随便说点什么"}),
            {"status": "no_result", "candidates": []},
            "zh",
            None,
        )
    assert "without live data" in str(excinfo.value)


def test_legacy_two_field_answer_still_works():
    """老应用只回 `{kind, faq_id}` 时行为不变 —— 这次改动不能要求所有人重发应用。"""
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    answer = final_answer(
        json.dumps({"kind": "realtime", "faq_id": ""}),
        {"status": "live", "candidates": [], "live_snapshot": SNAPSHOT},
        "zh",
        None,
    )
    assert "2.4" in answer["blocks"][0]["text"]


# ── documents 契约（线上应用那一版）：realtime 分支同样可以写 reply ──────────


def test_documents_contract_accepts_a_realtime_reply():
    from aiops_diagnostics.consumer.consumer_documents import answer_selection

    raw = json.dumps({"kind": "realtime", "answer": "", "reference_ids": [], "reply": GOOD})
    kind, answer = answer_selection(raw, {"status": "live", "live_snapshot": SNAPSHOT})
    assert kind == "realtime"
    assert answer["blocks"][0]["text"] == GOOD


def test_documents_contract_still_refuses_reply_on_other_kinds():
    from aiops_diagnostics.consumer.consumer_documents import answer_selection

    raw = json.dumps({"kind": "clarify", "answer": "", "reference_ids": [], "reply": "随便说"})
    with pytest.raises(ChatRunUnavailable):
        answer_selection(raw, {"status": "live", "live_snapshot": SNAPSHOT})


def test_documents_contract_without_reply_still_renders_deterministically():
    from aiops_diagnostics.consumer.consumer_documents import answer_selection

    raw = json.dumps({"kind": "realtime", "answer": "", "reference_ids": []})
    kind, answer = answer_selection(raw, {"status": "live", "live_snapshot": SNAPSHOT})
    assert kind == "realtime" and answer is None
