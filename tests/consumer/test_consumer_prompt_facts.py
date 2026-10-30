"""提示词写正文的四条事实判据，与两个契约上的澄清/无结果出口。

契约见 `docs/acceptance/dify-prompt-lifecycle/acceptance.feature`；
取舍与边界见 `src/aiops_diagnostics/consumer/consumer_facts.py` 的模块文档。

**这一组用例证明什么、不证明什么**：它证明"金额标签互换／编造单价／到账承诺／状态说反"
这四类可机械判定的错话会被拦下，且合格正文（两种风格）都能通过。它不证明业务准确率——
一句每个值都挂在正确标签上、但因果关系说反的话仍会通过。
"""

from __future__ import annotations

import json

import pytest

from aiops_diagnostics.consumer.consumer_reply import validate_notice, validate_reply
from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

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

CHARGING = {**STATUS, "status_code": "0", "status_role": "charging"}


def _reason(text, snapshot, language="zh"):
    with pytest.raises(ChatRunUnavailable) as excinfo:
        validate_reply(text, snapshot, language=language)
    return excinfo.value.code


def test_amount_labels_cannot_be_swapped():
    """两个值都是真的，标签互换了 —— 数字集合比对发现不了这件事。"""
    swapped = "这单账单合计 20，系统记录的支付金额是 27.36，已退 0。订单号 2107546575906942977。"
    assert _reason(swapped, BILLING) == "CHATFLOW_FACT_LABEL_MISMATCH"
    # 换回来就通过：判据拦的是"挂错标签"，不是"出现这两个数"。
    validate_reply(
        "这单账单合计 27.36，系统记录的支付金额是 20，已退 0。订单号 2107546575906942977。",
        BILLING,
        language="zh",
    )


def test_a_currency_amount_must_be_in_the_snapshot():
    """`每小时 3 元` 曾因"0–10 的小计数豁免"通过。带货币单位的数字不再是计数。"""
    assert (
        _reason(
            "这单合计 27.36，支付 20，退款 0，另外每小时 3 元。订单号 2107546575906942977。",
            BILLING,
        )
        == "CHATFLOW_FACT_INVENTED_AMOUNT"
    )
    # 同一个 3 作为**计数**仍然放行：`第 3 条` 不构成对钱数的断言。
    validate_reply(
        "这单合计 27.36，支付 20，退款 0。第 3 条是退款说明。订单号 2107546575906942977。",
        BILLING,
        language="zh",
    )


@pytest.mark.parametrize(
    "claim",
    ["已到账", "已经退款成功", "款项将原路退回", "已扣款", "预计今天到账"],
)
def test_payment_promises_are_refused(claim):
    """系统只有业务记录，没有支付渠道回执 —— "已到账"是一句我们证明不了的话。"""
    assert (
        _reason(
            f"这单合计 27.36，支付 20，退款 0，{claim}。订单号 2107546575906942977。",
            BILLING,
        )
        == "CHATFLOW_FACT_PROMISE"
    )


@pytest.mark.parametrize(
    "sentence",
    ["没有退款记录", "系统记录的支付金额不等于银行已经扣款", "不代表已经到账", "尚未退款"],
)
def test_negated_payment_statements_are_allowed(sentence):
    """否定形式正是我们要说的话：它没有断言任何未发生的事。"""
    validate_reply(
        f"这单合计 27.36，支付 20，退款 0，{sentence}。订单号 2107546575906942977。",
        BILLING,
        language="zh",
    )


def test_status_cannot_be_inverted():
    """快照说结束，正文不能说"未结束"；快照说充电中，不能说"已结束"。"""
    assert (
        _reason("这单还没结束，仍在充电。订单号 2107546575906942977。", STATUS)
        == "CHATFLOW_FACT_STATUS_INVERTED"
    )
    assert (
        _reason("这单已经结束了。订单号 2107546575906942977。", CHARGING) == "CHATFLOW_FACT_STATUS_INVERTED"
    )
    validate_reply("这单已经结束。订单号 2107546575906942977。", STATUS, language="zh")
    validate_reply("这单还在充电中，没有结束。订单号 2107546575906942977。", CHARGING, language="zh")


def test_unknown_status_role_is_not_judged():
    """状态未知时不做方向判断：把"未知"读成任何一种都是假陈述。"""
    unknown = {**STATUS, "status_code": "99", "status_role": "unknown"}
    validate_reply("这单的状态我还确认不了。订单号 2107546575906942977。", unknown, language="zh")


# --- 澄清／无结果／转人工：正文可以由提示词写，但一个数字都不许出现 -------------------


def test_notice_is_written_by_the_prompt():
    text = validate_notice("这个问题我这边没有查到对应资料，可以补充一下具体场景吗？", language="zh")
    assert text.startswith("这个问题")


@pytest.mark.parametrize(
    "text",
    [
        "请在 3 个工作日内补充。",  # 时限：没有依据
        "大概 20 元。",  # 金额：没有依据
        "详见 https://example.com/help",  # 链接：客服入口由系统追加
        "已为您提交工单。",  # 执行声明：系统没有做
        "已经退款成功。",  # 到账承诺
        "",
    ],
)
def test_notice_cannot_assert_anything(text):
    with pytest.raises(ChatRunUnavailable):
        validate_notice(text, language="zh")


def test_legacy_contract_delivers_a_prompt_written_notice():
    """旧两字段契约：`clarify` 的正文由提示词写，系统仍追加受控客服入口。"""
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    content = {"support": {"zh": {"label": "客服", "url": "https://support.example/zh"}}}
    answer = final_answer(
        json.dumps({"kind": "clarify", "faq_id": "", "reply": "能说一下是哪一单吗？"}),
        {"status": "no_result", "candidates": []},
        "zh",
        content,
    )
    text = answer["blocks"][0]["text"]
    assert text.startswith("能说一下是哪一单吗？")
    assert "https://support.example/zh" in text


def test_legacy_contract_keeps_system_text_on_service_failure():
    """服务故障时以系统文案为准：模型没有故障信息，让它描述故障只会多一个错处。"""
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    answer = final_answer(
        json.dumps({"kind": "unavailable", "faq_id": "", "reply": "服务出了点小问题"}),
        {"status": "unavailable", "candidates": []},
        "zh",
        None,
    )
    assert "服务出了点小问题" not in answer["blocks"][0]["text"]
    assert answer["retrieval_status"] == "unavailable"


def test_safety_and_photo_outputs_never_depend_on_the_model():
    """紧急安全提示与图片不可读：提示词写了什么都不影响这条出口。"""
    from aiops_diagnostics.consumer.consumer_knowledge import final_answer

    for status in ("safety", "photo_unreadable"):
        answer = final_answer(
            json.dumps({"kind": "realtime", "faq_id": "", "reply": "已到账，放心。"}),
            {"status": status, "candidates": []},
            "zh",
            None,
        )
        assert "已到账" not in answer["blocks"][0]["text"]
