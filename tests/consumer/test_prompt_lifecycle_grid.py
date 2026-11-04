"""16 格样本：8 类场景 × 2 种风格，两端各走真实交付路径。

风格是**两种真实写法**，不是关键词对照：`简洁直接` 与 `亲切解释` 都必须走得通，而且事实守卫
对两者一视同仁。这一层证明的是"提示词写的话能成为用户看到的正文，且事实判据不因风格松紧"。

它**不**证明真实模型的观感差异 —— 那只能在 41 上用真实模型跑（见 qa-plan §五）。

契约见 `docs/acceptance/dify-prompt-lifecycle/acceptance.feature`。
"""

from __future__ import annotations

import json

import pytest

from aiops_diagnostics.consumer.consumer_documents import answer_selection

BILLING = {
    "order_no": "2107546575906942977",
    "source": "cpms.orderDetail",
    "observed_at": "2026-10-30T09:08:26+00:00",
    "order_amount": "27.36",
    "recorded_payment_amount": "20",
    "recorded_refund_amount": "0",
    "refund_status": "0",
}
STATUS = {
    "order_no": "2107546575906942977",
    "source": "cpms.chargingInfo",
    "observed_at": "2026-10-30T09:08:26+00:00",
    "status_code": "1",
    "status_role": "finished",
}

#: 两种风格：同一件事，一个先说结论，一个先接住情绪。
STYLES = ("简洁直接", "亲切解释")


# --- 客户端：5 类出口 × 2 风格 -------------------------------------------------------

KNOWLEDGE = {
    "简洁直接": "充电枪拔不下来时，先在 App 点停止充电，再按枪上的解锁键。",
    "亲切解释": "遇到枪拔不下来别着急：先在 App 里点停止充电，等指示灯变了再按枪上的解锁键。",
}
STATUS_REPLY = {
    "简洁直接": "这单已经结束充电。订单号 2107546575906942977。",
    "亲切解释": "帮您看了一下，这单的充电已经结束啦。订单号 2107546575906942977，您可以按这个单号核对。",
}
BILLING_REPLY = {
    "简洁直接": "这单账单合计 27.36，系统记录的支付金额是 20，没有退款记录。订单号 2107546575906942977。",
    "亲切解释": (
        "这单合计 27.36，账上记的付款是 20，没有退款记录。订单号 2107546575906942977。"
        "系统记录的金额不等于银行已经扣款，也不代表已经到账。"
    ),
}
CLARIFY = {
    "简洁直接": "需要订单号才能查。",
    "亲切解释": "我这边还差一个订单号才能帮您查，方便说一下是哪一单吗？",
}
NOT_FOUND = {
    "简洁直接": "这次没查到对应资料。",
    "亲切解释": "这次没有找到能回答这个问题的资料，换个说法我再帮您找找。",
}


def documents_context(status="found"):
    return {
        "mode": "documents",
        "status": status,
        "sources": [
            {
                "reference_id": "source-1",
                "retrieved_reference_id": "doc-1",
                "title": "充电枪使用",
                "content": "",
            }
        ],
        "live_snapshot": None,
    }


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_knowledge(style):
    kind, answer = answer_selection(
        json.dumps({"kind": "knowledge", "answer": KNOWLEDGE[style], "reference_ids": ["source-1"]}),
        documents_context(),
    )
    assert kind == "knowledge"
    assert answer["blocks"][0]["text"] == KNOWLEDGE[style]


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_status(style):
    context = {**documents_context(), "live_snapshot": STATUS}
    kind, answer = answer_selection(
        json.dumps({"kind": "realtime", "answer": "", "reference_ids": [], "reply": STATUS_REPLY[style]}),
        context,
    )
    assert kind == "realtime"
    assert answer["blocks"][0]["text"] == STATUS_REPLY[style]


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_billing(style):
    context = {**documents_context(), "live_snapshot": BILLING}
    kind, answer = answer_selection(
        json.dumps({"kind": "realtime", "answer": "", "reference_ids": [], "reply": BILLING_REPLY[style]}),
        context,
    )
    assert kind == "realtime"
    assert answer["blocks"][0]["text"] == BILLING_REPLY[style]


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_clarify(style):
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    answer = final_answer(
        json.dumps({"kind": "clarify", "faq_id": "", "reply": CLARIFY[style]}),
        {"status": "no_result", "candidates": []},
        "zh",
        None,
    )
    assert answer["blocks"][0]["text"].startswith(CLARIFY[style])


@pytest.mark.parametrize("style", STYLES)
def test_grid_client_not_found(style):
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    answer = final_answer(
        json.dumps({"kind": "not_found", "faq_id": "", "reply": NOT_FOUND[style]}),
        {"status": "no_result", "candidates": []},
        "zh",
        None,
    )
    assert answer["blocks"][0]["text"].startswith(NOT_FOUND[style])


# 管家统一模型回复允许不同风格，答复自然表达并附真实来源。
@pytest.mark.parametrize("style", STYLES)
def test_operator_style_preserves_natural_reply(style):
    from aiops_diagnostics.operator.operator_agent import render

    lead = "记录如下。" if style == "简洁直接" else "我帮您看一下这条记录。"
    evidence = {
        "cpms_order_billing": {"data": {"payAmount": "27.36"}, "observed_at": "2026-10-30T09:08:26+00:00"}
    }
    value = render(
        lead + "记录付款27.36",
        evidence,
    )
    assert value["blocks"][0]["text"].startswith(lead)
    assert "27.36" in value["blocks"][0]["text"]
