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
        validate_reply(text, snapshot, language=language, contract="slots")
    return excinfo.value.code


def test_amount_labels_cannot_be_swapped():
    _reason("合计20，付款27.36，退款0。订单2107546575906942977。", BILLING)
    answer = validate_reply("{{fact:billing_line}}", BILLING, language="zh", contract="slots")
    assert "27.36" in answer and "20" in answer


def test_a_currency_amount_must_be_in_the_snapshot():
    _reason("{{fact:billing_line}}\n每小时3元。", BILLING)
    assert validate_reply("下面是查询结果。\n{{fact:billing_line}}", BILLING, language="zh", contract="slots")


@pytest.mark.parametrize(
    "claim", ["已到账", "已经退款成功", "款项将原路退回", "已扣款", "预计今天到账", "今天会到账"]
)
def test_payment_promises_are_refused(claim):
    assert _reason("{{fact:billing_line}}\n" + claim, BILLING) == "CHATFLOW_FACT_PROMISE"


@pytest.mark.parametrize(
    "sentence", ["没有退款记录", "系统记录的支付金额不等于银行已经扣款", "不代表已经到账", "尚未退款"]
)
def test_negated_payment_statements_are_allowed(sentence):
    assert validate_reply("{{fact:billing_line}}\n" + sentence, BILLING, language="zh", contract="slots")


def test_status_cannot_be_inverted():
    _reason("这单并非{{fact:status_line}}", STATUS)
    for snapshot in (STATUS, CHARGING):
        answer = validate_reply("{{fact:status_line}}", snapshot, language="zh", contract="slots")
        assert snapshot["order_no"] in answer


def test_unknown_status_role_is_not_judged():
    unknown = {**STATUS, "status_code": "99", "status_role": "unknown"}
    answer = validate_reply("{{fact:status_line}}", unknown, language="zh", contract="slots")
    assert "未知" in answer


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
