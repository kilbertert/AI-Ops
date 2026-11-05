"""「这门语言没有文案时读哪门」只有一个出处（#1371 c/d）。

四个读者曾经各答各的：`health_report_copy._pack` 回中文、`consumer_plain.words`
回英文、`i18n.free_text_unavailable_message` 回中文、`consumer_language.COPY`
直接 KeyError。现在方向是清单上的声明（`LanguageSpec.fallback`），读者走
`localized_pack` / `rendering_language`；组合渲染面按「这张表实际能渲染
哪门语言」收口，整句同一语种，不再把外语状态词嵌进英文句壳。
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.common.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    SUPPORTED_LANGUAGES,
    fallback_language,
    language_spec,
    localized_pack,
    rendering_language,
)
from aiops_diagnostics.consumer.consumer_knowledge import MESSAGES
from aiops_diagnostics.consumer.consumer_language import COPY, status_word
from aiops_diagnostics.consumer.consumer_plain import WORDS
from aiops_diagnostics.operator.operator_agent import _SOURCE_LABELS, render

STATUS = {
    "order_no": "1",
    "status_role": "charging",
    "status_code": 0,
    "observed_at": "2026-10-29T12:34:45+00:00",
    "source": "cpms.chargingInfo",
}
BILLING = {
    "order_no": "1",
    "order_amount": "12.34",
    "recorded_payment_amount": "5.00",
    "refund_status": "2",
    "recorded_refund_amount": "7.34",
    "observed_at": "2026-10-29T12:34:45+00:00",
    "source": "cpms.orderDetail",
}

#: 本测试逐字钉住的方向表：`fallback` 字段的全部取值理由。中文家族读权威
#: 文案所在语言 `zh`；其余缺行读 `en` —— 对非中文读者英文尚可读，中文则必触
#: 泄漏守卫；`en` 自己缺行时回到权威语言。
EXPECTED_FALLBACKS = {
    "zh": "zh",
    "zh-Hant": "zh",
    "en": "zh",
    "de": "en",
    "fr": "en",
    "es": "en",
    "pt": "en",
    "vi": "en",
    "mn": "en",
    "th": "en",
    "km": "en",
}

#: 只覆盖部分语言的词表（整段模板表 `COPY` 按合同只含九个本地化语种，不在此列）。
#: 每张都是「这门语言有没有文案」曾经自己回答的一张表；现在回退方向共享。
_PARTIAL_TABLES = (
    pytest.param(WORDS, id="consumer_plain.WORDS"),
    pytest.param(MESSAGES, id="consumer_knowledge.MESSAGES"),
    pytest.param(_SOURCE_LABELS, id="operator_agent._SOURCE_LABELS"),
)


def test_every_spec_declares_a_fallback_inside_the_inventory() -> None:
    """`fallback` 必须指向清单里存在的语言，且取值就是上表——防呆也防漂移。"""
    assert set(EXPECTED_FALLBACKS) == set(SUPPORTED_LANGUAGES)
    for spec in LANGUAGES:
        assert spec.fallback == EXPECTED_FALLBACKS[spec.tag], spec.tag
        assert spec.fallback in SUPPORTED_LANGUAGES, spec.tag


def test_fallback_language_answers_for_registered_and_unregistered() -> None:
    for tag, expected in EXPECTED_FALLBACKS.items():
        assert fallback_language(tag) == expected, tag
    # 未登记语种与 `resolve_language` 同向：回落默认语言。
    assert fallback_language("xx-invalid") == DEFAULT_LANGUAGE


def test_rendering_language_prefers_request_then_fallback_then_default() -> None:
    table = {"zh": 1, "en": 2}
    assert rendering_language(table, "zh") == "zh"
    assert rendering_language(table, "en") == "en"
    assert rendering_language(table, "zh-Hant") == "zh"  # 声明的 fallback 是 zh
    assert rendering_language(table, "de") == "en"
    assert rendering_language(table, "xx-invalid") == DEFAULT_LANGUAGE
    # fallback 也不在表里时，落到默认语言 —— 链是完整的，不存在「退无可退」。
    zh_only = {"zh": 1}
    assert rendering_language(zh_only, "de") == "zh"


def test_localized_pack_returns_the_resolved_row() -> None:
    table = {"zh": {"k": "中"}, "en": {"k": "en"}}
    assert localized_pack(table, "de") is table["en"]
    assert localized_pack(table, "zh-Hant") is table["zh"]


@pytest.mark.parametrize("table", _PARTIAL_TABLES)
def test_a_registered_language_missing_from_a_table_reads_its_declared_fallback(table) -> None:
    """清单一门「表里没有」的语言，各表都读到同一个已声明目标。"""
    # `de` 在这些表里都没有行，其声明 fallback 是 `en`。
    assert localized_pack(table, "de") == table["en"]
    # `zh-Hant` 在只有 zh/en 的表里读 zh；有行的表读自己的行。
    expected = table["zh-Hant"] if "zh-Hant" in table else table["zh"]
    assert localized_pack(table, "zh-Hant") == expected


@pytest.mark.parametrize("table", _PARTIAL_TABLES)
def test_an_unregistered_language_reads_the_default_row(table) -> None:
    assert localized_pack(table, "xx-invalid") == table[DEFAULT_LANGUAGE]


def test_status_word_stays_the_single_authority() -> None:
    assert status_word("zh", "charging") == "充电中"
    assert status_word("zh-Hant", "finished") == COPY["zh-Hant"]["finished"]
    assert status_word("de", "charging") == COPY["de"]["charging"]
    # 未登记语种读中性的角色名，不再按前缀猜中文。
    assert status_word("xx-invalid", "charging") == "charging"


def test_copy_lookup_is_contract_scoped() -> None:
    """`COPY` 按合同只服务九个本地化语种；组合语种（zh/en）到这里仍是 KeyError。"""
    for tag in COPY:
        assert localized_pack(COPY, tag) is COPY[tag]
    with pytest.raises(KeyError):
        localized_pack(COPY, "zh")


def test_the_composed_surface_speaks_one_language_per_answer() -> None:
    """非中文语种在组合面上读不到一个中文字符 —— 语言矩阵，不是抽样。"""
    from aiops_diagnostics.common.i18n import chinese_leak
    from aiops_diagnostics.consumer.consumer_billing import billing_answer
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

    for language in SUPPORTED_LANGUAGES:
        if language_spec(language).is_chinese:
            continue
        for text in (
            status_answer(dict(STATUS), language, localized=False)["blocks"][0]["text"],
            billing_answer(dict(BILLING), language, localized=False)["blocks"][0]["text"],
        ):
            assert chinese_leak(text) == "", (language, text)


def test_a_language_the_surface_cannot_render_reads_the_fallback_verbatim() -> None:
    """de 在组合词表里没有行：整句按声明的 fallback 读英文，德语状态词不再
    嵌进英文句壳 —— 混排与「中文字符」是同一个缺陷的两半。"""
    from aiops_diagnostics.consumer.consumer_billing import billing_answer
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

    assert status_answer(dict(STATUS), "de", localized=False) == status_answer(
        dict(STATUS), "en", localized=False
    )
    assert billing_answer(dict(BILLING), "de", localized=False) == billing_answer(
        dict(BILLING), "en", localized=False
    )
    en_text = status_answer(dict(STATUS), "en", localized=False)["blocks"][0]["text"]
    assert "charging" in en_text and "Ladevorgang" not in en_text


def test_traditional_billing_reads_the_traditional_words_table() -> None:
    """组合面的词表本就覆盖 zh-Hant：不本地化不再把繁体请求折成简体。"""
    from aiops_diagnostics.consumer.consumer_billing import billing_answer

    text = billing_answer(dict(BILLING), "zh-Hant", localized=False)["blocks"][0]["text"]
    assert "這筆訂單" in text


def test_the_orchestrator_no_longer_folds_what_the_surface_resolves() -> None:
    """入口折叠与各面自解析必须给出同一份正文，否则折叠仍是一份私有语法。"""
    from aiops_diagnostics.consumer import consumer_knowledge
    from aiops_diagnostics.consumer.consumer_billing import billing_answer
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

    assert consumer_knowledge.live_answer(dict(STATUS), "de", localized=False) == status_answer(
        dict(STATUS), "de", localized=False
    )
    assert consumer_knowledge.live_answer(dict(BILLING), "de", localized=False) == billing_answer(
        dict(BILLING), "de", localized=False
    )


def test_operator_answer_sources_follow_the_answer_language() -> None:
    """数据来源尾段是拼进用户正文的部分：它必须跟答复同语言，而不是永远中文。"""
    from aiops_diagnostics.common.i18n import chinese_leak

    evidence = {"cpms_order_detail": {"observed_at": "2026-10-29T12:34:45+00:00"}}
    assert "【本轮数据来源】" in render("答复。", evidence, "zh")["blocks"][0]["text"]
    assert "【本輪資料來源】" in render("答覆。", evidence, "zh-Hant")["blocks"][0]["text"]
    text = render("Antwort.", evidence, "de")["blocks"][0]["text"]
    assert "Sources used for this answer" in text and "Order details" in text
    assert chinese_leak(text) == ""
