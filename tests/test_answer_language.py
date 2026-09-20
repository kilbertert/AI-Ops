"""Tests for the shared answer-surface language guard (#293).

The guard is judged on observable behaviour: given an answer payload and a
requested language, which Chinese does it consider leaked? Nothing here asserts
how a surface calls the guard, only what the guard decides.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.answer_language import (
    answer_chinese_leak,
    looks_like_asset_name,
    text_block_leak,
)


@pytest.mark.parametrize(
    "value",
    [
        "新加坡无人电动巴士.mp4",
        "重卡充电.mp4",
        "趋势智能_渠道订单归集功能.png",
        "重卡流量平台_互联互通过程图.png",
    ],
)
def test_resource_filenames_are_recognised_by_shape(value: str) -> None:
    assert looks_like_asset_name(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "Singapore has limited land resources.",
        "标题: 新加坡国家级无人电动巴士项目",
        "stopped_reason_content",
        "",
        "这是一个很长的中文句子，用来证明文件名豁免不会把整段散文也放过。",
    ],
)
def test_prose_is_not_mistaken_for_a_resource_name(value: str) -> None:
    assert looks_like_asset_name(value) is False


def test_chinese_in_an_english_answer_is_a_leak() -> None:
    answer = {"blocks": [{"kind": "text", "text": "标题: 新加坡项目"}]}

    assert answer_chinese_leak(answer, "en") != ""


def test_clean_english_answer_has_no_leak() -> None:
    answer = {"blocks": [{"kind": "text", "text": "The order was force-stopped."}]}

    assert answer_chinese_leak(answer, "en") == ""


def test_resource_filename_in_a_media_title_is_exempt() -> None:
    """A media title is an identifier; translating it would orphan the asset."""
    answer = {
        "blocks": [
            {"kind": "text", "text": "See the attached case video."},
            {"kind": "video", "media": {"title": "新加坡无人电动巴士.mp4"}},
        ]
    }

    assert answer_chinese_leak(answer, "en") == ""


def test_chinese_prose_beside_an_exempt_filename_is_still_a_leak() -> None:
    """The exemption is per-value, not per-answer."""
    answer = {
        "blocks": [
            {"kind": "text", "text": "标题: 新加坡项目"},
            {"kind": "video", "media": {"title": "新加坡无人电动巴士.mp4"}},
        ]
    }

    assert answer_chinese_leak(answer, "en") != ""


def test_chinese_leak_never_fires_for_the_chinese_answer() -> None:
    answer = {"blocks": [{"kind": "text", "text": "标题: 新加坡项目"}]}

    assert answer_chinese_leak(answer, "zh") == ""
    assert text_block_leak(answer["blocks"], "zh") == ""


def test_unsupported_language_is_left_alone() -> None:
    """Upstream already resolves unknown tags to the default language."""
    answer = {"blocks": [{"kind": "text", "text": "标题: 新加坡项目"}]}

    assert answer_chinese_leak(answer, "ja") == ""


def test_text_block_leak_judges_only_prose() -> None:
    blocks = [
        {"kind": "text", "text": "All clear."},
        {"kind": "image", "media": {"title": "重卡充电桩流量平台_产品海报.png"}},
    ]

    assert text_block_leak(blocks, "en") == ""


def test_text_block_leak_reports_the_offending_prose() -> None:
    blocks = [{"kind": "text", "text": "行业痛点: 土地资源有限"}]

    assert text_block_leak(blocks, "de") == "业土地有源点痛行资限"


def test_non_chinese_languages_are_all_covered() -> None:
    """One assertion per supported language, so a new language cannot slip by."""
    blocks = [{"kind": "text", "text": "标题"}]
    answer = {"blocks": blocks}

    for language in ("en", "de", "fr", "es", "pt"):
        assert answer_chinese_leak(answer, language) != ""


def test_reference_titles_are_exempt_as_resource_names() -> None:
    """The contract defines this field as the source DOCUMENT NAME, and the
    knowledge base stores Chinese-named documents, so a Chinese value here is a
    name, not a leak.

    Accepted limitation, recorded deliberately: a model that invents a Chinese
    heading instead of citing a real document name is indistinguishable by
    shape from a document that is genuinely named that, so this case is not
    caught. Closing it needs document-name provenance (compare the title
    against the chunks actually returned this turn), not a smarter pattern.
    """
    answer = {"blocks": [{"kind": "reference", "reference_id": "r1", "title": "重卡充电案例"}]}

    assert answer_chinese_leak(answer, "en") == ""


def test_reference_title_prose_punctuation_still_flags() -> None:
    """A sentence is judged even in a title field — names carry no punctuation."""
    answer = {
        "blocks": [{"kind": "reference", "reference_id": "r2", "title": "错误 402：余额不足，请先充值.pdf"}]
    }

    assert answer_chinese_leak(answer, "en") != ""


# --------------------------------------------------------------------------
# Zero-order surface (#293, F7)
#
# Reachable, not theoretical: when customer-RAG degrades on an unavailable
# knowledge base it deliberately falls through here, so the same user who asked
# for English receives this text. Its prompt is authored in Chinese, so the
# model copies from Chinese instructions — same shape as the card headings.
# --------------------------------------------------------------------------


def test_zero_order_answer_that_leaked_chinese_is_replaced() -> None:
    from aiops_diagnostics.gateway_runtime import _guard_zero_order_language
    from aiops_diagnostics.i18n import QA_FALLBACK_MESSAGES

    guarded = _guard_zero_order_language({"text": "先按卡扣再拔枪。", "reminder": True}, "en")

    assert guarded["text"] == QA_FALLBACK_MESSAGES["en"]["unavailable"]
    # The payload keeps exactly the keys the contract defines.
    assert set(guarded) == {"text", "reminder"}


def test_zero_order_clean_answer_is_untouched() -> None:
    from aiops_diagnostics.gateway_runtime import _guard_zero_order_language

    original = {"text": "Press the latch, then pull the connector.", "reminder": True}

    assert _guard_zero_order_language(original, "en") == original


def test_zero_order_chinese_answer_is_untouched() -> None:
    from aiops_diagnostics.gateway_runtime import _guard_zero_order_language

    original = {"text": "先按卡扣再拔枪。", "reminder": True}

    assert _guard_zero_order_language(original, "zh") == original


def test_fullwidth_chinese_punctuation_counts_as_a_leak() -> None:
    """A model writing through a Chinese input method emits fullwidth marks."""
    from aiops_diagnostics.answer_language import answer_chinese_leak

    answer = {"blocks": [{"kind": "text", "text": "Scan the QR code，then start charging."}]}

    assert answer_chinese_leak(answer, "en") != ""
