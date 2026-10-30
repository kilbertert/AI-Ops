"""C端状态与账单答复：先给结论，再用人话解释每一项。

契约见 `docs/acceptance/consumer-plain-answers/acceptance.feature`，取舍见 ADR-0181。
这里断言的是**读者能看到什么**，不是措辞副本：

* 第一段就是结论（问的人第一眼要看到"这单怎么样"）；
* 术语要有人话解释，且不许把「账务里记的数」说成「已经扣款」；
* 内部标识（`cpms.*`、`status_code=`）不出现在正文；
* 十一语种同形，免责语义不许被简化掉。
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.common.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.consumer.consumer_billing import billing_answer
from aiops_diagnostics.consumer.consumer_order_chat import status_answer
from aiops_diagnostics.consumer.consumer_plain import SOURCE_KEYS, WORDS

ISO = "2026-10-29T12:34:45.123456+00:00"

STATUS = {
    "order_no": "2113549588485718017",
    "status_code": "1",
    "status_role": "finished",
    "observed_at": ISO,
    "source": "cpms.chargingInfo",
}
BILLING = {
    **STATUS,
    "source": "cpms.orderDetail",
    "order_amount": "27.36",
    "recorded_payment_amount": "0.00",
    "refund_status": "0",
    "recorded_refund_amount": None,
}


def _text(answer: dict) -> str:
    return answer["blocks"][0]["text"]


def _first_paragraph(text: str) -> str:
    return text.split("\n\n")[0]


def _bullet(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("·") and needle in line)


# ── CP-01 / CP-02：第一段就是结论 ────────────────────────────────────────────


def test_billing_answer_opens_with_a_conclusion_not_a_field_list():
    first = _first_paragraph(_text(billing_answer(BILLING, "zh")))
    # 结论句里必须同时有金额与实付：只写「订单金额：」不算回答了「这单花了我多少」。
    assert "27.36" in first and "0.00" in first
    assert "没有退款" in first
    # 结论句不能是「字段名：」开头 —— 那正是反馈里"干巴巴罗列"的形状。
    assert not first.startswith(("订单金额", "支付金额", "订单账单"))


def test_status_answer_opens_with_a_sentence_not_a_field_name():
    first = _first_paragraph(_text(status_answer(STATUS, "zh")))
    assert "充电结束" in first
    assert not first.startswith(("订单状态", "订单号"))


# ── CP-03 / CP-06：缺值说成「上游没有」，不编数字 ────────────────────────────


def test_missing_amount_is_explained_and_never_invented():
    thin = {**BILLING, "order_amount": None, "recorded_payment_amount": None, "recorded_refund_amount": None}
    text = _text(billing_answer(thin, "zh"))
    assert "上游未提供" in text
    assert "原订单页面核对" in text
    # 没有值就不能在结论句里出现任何金额：编一个数字比说"没提供"更糟。
    assert "None" not in text and "null" not in text


def test_zero_is_a_value_not_a_missing_field():
    zero = {**BILLING, "order_amount": "0", "recorded_payment_amount": "0"}
    text = _text(billing_answer(zero, "zh"))
    assert "上游未提供" not in text.split("\n\n")[0]
    assert "0" in _first_paragraph(text)


# ── CP-04 / CP-09：免责语义不许被简化 ───────────────────────────────────────


def test_payment_line_denies_debit_and_refund_line_denies_arrival():
    text = _text(billing_answer(BILLING, "zh"))
    payment = _bullet(text, "支付金额")
    refund = _bullet(text, "退款")
    assert "扣款" in payment and any(w in payment for w in ("不等", "不代表", "不证明"))
    assert "到账" in refund and any(w in refund for w in ("不等", "不代表", "不证明"))
    assert "没有发起任何付款或退款" in text


# ── CP-05：内部标识不进正文 ─────────────────────────────────────────────────


@pytest.mark.parametrize("render,payload", [(status_answer, STATUS), (billing_answer, BILLING)])
def test_internal_identifiers_do_not_reach_the_reader(render, payload):
    text = _text(render(payload, "zh"))
    assert "cpms." not in text
    assert "status_code=" not in text and "status_role=" not in text


# ── CP-07 / CP-08：十一语种同形 ─────────────────────────────────────────────


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_every_language_leads_with_a_conclusion(language):
    status = _text(status_answer(STATUS, language, localized=True))
    bill = _text(billing_answer(BILLING, language))

    for text in (status, bill):
        first = _first_paragraph(text)
        assert first.strip(), (language, text)
        # 结论句不能是空壳：里面要么有状态名，要么有金额。
        assert language in ("zh", "en") or first != text.split("\n\n")[1], (language, text)
        assert ISO not in text, "微秒 ISO 串不该摆给用户"
        assert "UTC" in text, "读者要能对上手机时钟"
        assert STATUS["order_no"] in text
        assert "cpms." not in text and "status_code=" not in text

    assert "27.36" in bill and "0.00" in bill
    assert STATUS["order_no"] in status


def test_code_word_tables_cover_the_same_keys_as_the_rendered_languages():
    """三张代码词表必须同构：少一个键就会在某个语种上抛 KeyError。"""
    reference = set(WORDS["zh"])
    assert set(WORDS["zh-Hant"]) == reference
    assert set(WORDS["en"]) == reference
    # 来源映射里的键必须在每张表里都有对应标签。
    for key in SOURCE_KEYS.values():
        assert key in reference
