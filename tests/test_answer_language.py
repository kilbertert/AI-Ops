"""Tests for the shared answer-surface language guard (#293).

The guard is judged on observable behaviour: given an answer payload and a
requested language, which Chinese does it consider leaked? How a surface calls
it is a source-level guard of its own; what the guard accepts as a payload is
part of the behaviour asserted here — since #366 anything but the contract type
or a plain string is a TypeError, not a second judgement path.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from aiops_diagnostics.answer_language import (
    AnswerSurface,
    answer_chinese_leak,
    looks_like_asset_name,
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
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": "标题: 新加坡项目"}])

    assert answer_chinese_leak(surface, "en") != ""


def test_clean_english_answer_has_no_leak() -> None:
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": "The order was force-stopped."}])

    assert answer_chinese_leak(surface, "en") == ""


def test_resource_filename_in_a_media_title_is_exempt() -> None:
    """A media title is an identifier; translating it would orphan the asset."""
    surface = AnswerSurface.from_public_blocks(
        [
            {"kind": "text", "text": "See the attached case video."},
            {"kind": "video", "media": {"title": "新加坡无人电动巴士.mp4"}},
        ]
    )

    assert answer_chinese_leak(surface, "en") == ""


def test_chinese_prose_beside_an_exempt_filename_is_still_a_leak() -> None:
    """The exemption is per-value, not per-answer."""
    surface = AnswerSurface.from_public_blocks(
        [
            {"kind": "text", "text": "标题: 新加坡项目"},
            {"kind": "video", "media": {"title": "新加坡无人电动巴士.mp4"}},
        ]
    )

    assert answer_chinese_leak(surface, "en") != ""


def test_chinese_leak_never_fires_for_the_chinese_answer() -> None:
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": "标题: 新加坡项目"}])

    assert answer_chinese_leak(surface, "zh") == ""


def test_unsupported_language_is_left_alone() -> None:
    """Upstream already resolves unknown tags to the default language."""
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": "标题: 新加坡项目"}])

    assert answer_chinese_leak(surface, "ja") == ""


def test_media_titles_are_exempt_as_resource_names() -> None:
    """Only prose is judged: what a non-text block carries is an identifier.

    Reaches the guard through the same construction path a surface uses, so the
    exemption is not an artefact of the shape the test picked.
    """
    blocks = [
        {"kind": "text", "text": "All clear."},
        {"kind": "image", "media": {"title": "重卡充电桩流量平台_产品海报.png"}},
    ]

    assert answer_chinese_leak(AnswerSurface.from_public_blocks(blocks), "en") == ""


def test_text_block_prose_is_reported() -> None:
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": "行业痛点: 土地资源有限"}])

    assert answer_chinese_leak(surface, "de") == "业土地有源点痛行资限"


def test_non_chinese_languages_are_all_covered() -> None:
    """One assertion per supported language, so a new language cannot slip by."""
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": "标题"}])

    for language in ("en", "de", "fr", "es", "pt"):
        assert answer_chinese_leak(surface, language) != ""


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
    surface = AnswerSurface.from_public_blocks(
        [{"kind": "reference", "reference_id": "r1", "title": "重卡充电案例"}]
    )

    assert answer_chinese_leak(surface, "en") == ""


def test_reference_title_prose_punctuation_still_flags() -> None:
    """A sentence is judged even in a title field — names carry no punctuation."""
    surface = AnswerSurface.from_public_blocks(
        [{"kind": "reference", "reference_id": "r2", "title": "错误 402：余额不足，请先充值.pdf"}]
    )

    assert answer_chinese_leak(surface, "en") != ""


# --------------------------------------------------------------------------
# The payload as a contract (#363)
#
# The guard used to accept `Any` and pick a branch by shape, and the one
# production shape that needed the exemption (a bare list of block dicts) fell
# through to a shape-blind flatten that knows neither the block's kind nor its
# field names. The payload is a value now: a surface carries blocks, and each
# block carries its own kind, its own title and the descriptor mounted on it.
# --------------------------------------------------------------------------


def test_a_media_descriptor_mounted_on_its_block_is_judged_under_that_kind() -> None:
    """The descriptor the guard recursed into was unreachable in production:
    `to_public_dict` mounts `media` after the judgement point, so nothing ever
    judged a `media.title`. On the contract type the descriptor is a field of
    the block that carries it, so the same predicate that exempts a block title
    exempts the resource's name beside it.
    """
    surface = AnswerSurface.from_public_blocks(
        [
            {"kind": "text", "text": "See the attached case video."},
            {
                "kind": "video",
                "title": "新加坡无人电动巴士.mp4",
                "media": {"title": "新加坡无人电动巴士.mp4"},
            },
        ]
    )

    assert surface.blocks[1].kind == "video"
    assert surface.blocks[1].title == "新加坡无人电动巴士.mp4"
    assert surface.blocks[1].media_title == "新加坡无人电动巴士.mp4"
    assert answer_chinese_leak(surface, "en") == ""


@pytest.mark.parametrize("kind", ["image", "video", "reference"])
def test_a_resource_name_in_a_title_is_exempt_on_the_contract(kind: str) -> None:
    """Same rule as the dict shape, now run on the contract: the kind names the
    resource, so the title is an identifier rather than prose."""
    surface = AnswerSurface.from_public_blocks([{"kind": kind, "title": "重卡充电案例"}])

    assert answer_chinese_leak(surface, "en") == ""


def test_the_exemption_follows_the_kind_not_the_key() -> None:
    """A title on a block whose kind does not name a resource is prose."""
    surface = AnswerSurface.from_public_blocks(
        [{"kind": "text", "text": "Scan the QR code to start charging.", "title": "标题"}]
    )

    assert answer_chinese_leak(surface, "en") != ""


@pytest.mark.parametrize("kind", ["image", "video", "reference"])
def test_a_descriptor_holding_a_real_resource_name_is_exempt(kind: str) -> None:
    """The descriptor's name rides on its block's kind — the same predicate that
    exempts the block's own title, not a second rule for a second layer."""
    surface = AnswerSurface.from_public_blocks([{"kind": kind, "media": {"title": "重卡充电案例"}}])

    assert answer_chinese_leak(surface, "en") == ""


def test_a_descriptor_holding_prose_is_judged() -> None:
    """The descriptor is not a blanket pass on names: a sentence inside it is."""
    surface = AnswerSurface.from_public_blocks(
        [
            {
                "kind": "video",
                "title": "新加坡无人电动巴士.mp4",
                "media": {"title": "错误 402：余额不足，请先充值.pdf"},
            }
        ]
    )

    assert answer_chinese_leak(surface, "en") != ""


def test_a_descriptor_on_a_block_that_names_no_resource_is_judged() -> None:
    """Judged rather than exempted when no kind says the name is a resource.

    The public shape never mounts a descriptor on a text block; this pins that
    the exemption comes from the block's kind, so a future block kind gets the
    conservative answer until it declares `title` a resource name.
    """
    surface = AnswerSurface.from_public_blocks(
        [{"kind": "text", "text": "See the attached case video.", "media": {"title": "重卡充电案例"}}]
    )

    assert answer_chinese_leak(surface, "en") != ""


def test_a_plain_text_payload_is_judged_as_text() -> None:
    """Text alone is a declared input, not a shape the guard noticed: the
    zero-order and casual finalization points return exactly this."""
    assert answer_chinese_leak("标题: 新加坡项目", "en") != ""
    assert answer_chinese_leak("Scan the QR code, then start charging.", "en") == ""


@pytest.mark.parametrize(
    ("key", "value"),
    [("resource_id", "media_充电指南"), ("reference_id", "chunk-充电枪时序数据")],
    ids=["the media resource id", "the chunk or document id"],
)
def test_the_ids_a_block_delivers_are_judged(key: str, value: str) -> None:
    """The payload's ids are delivered to the user, so the promise covers them.

    The pre-contract production payload reached them as leaves of the shape-blind
    flatten, so this is coverage kept rather than coverage added — and the note
    is deliberate, because the contract type's field list is what would silently
    drop them otherwise.
    """
    surface = AnswerSurface.from_public_blocks([{"kind": "reference", key: value}])

    assert answer_chinese_leak(surface, "en") != ""


def test_an_identifier_the_library_issued_in_ascii_contributes_nothing() -> None:
    """The other half: judging the ids is not a new way to withhold a card."""
    surface = AnswerSurface.from_public_blocks(
        [{"kind": "video", "resource_id": "media_9f2c1d", "title": "新加坡无人电动巴士.mp4"}]
    )

    assert answer_chinese_leak(surface, "en") == ""


# --------------------------------------------------------------------------
# The shape bypass is gone (#366)
#
# Every finalisation point passes the contract type now (#364, #365), so the
# shape dispatch had no caller left to serve — and it was the root cause, not a
# symptom: a bare list of block dicts was judged leaf by leaf with no kinds, so
# the resource-name exemption could not reach the one surface that needed it.
# The branch and `text_block_leak` are deleted, and any other shape raises,
# because a shape mistake must not be able to repick a judgement path.
# --------------------------------------------------------------------------


def test_the_serialised_dict_is_no_longer_a_way_in() -> None:
    """The serialised payload used to reach the same conclusion through a branch
    of its own. It is a caller bug now, and the contract type is the only way
    in: the exemption travels with the type rather than with the shape the
    caller happened to pick.
    """
    blocks = [
        {"kind": "text", "text": "标题: 新加坡项目"},
        {
            "kind": "video",
            "title": "新加坡无人电动巴士.mp4",
            "media": {"title": "新加坡无人电动巴士.mp4"},
        },
    ]

    # Prose beside an exempt name is still a leak: the exemption is per-value.
    assert answer_chinese_leak(AnswerSurface.from_public_blocks(blocks), "en") != ""

    with pytest.raises(TypeError):
        answer_chinese_leak({"blocks": blocks}, "en")


def test_the_bare_list_does_not_get_a_judgement_of_its_own() -> None:
    """The root-cause shape: `qa_rag` passed a bare list of block dicts, which the
    guard flattened leaf by leaf without knowing a block's kind — so the only
    surface that needed the resource-name exemption never got it, and one good
    card was replaced with "the knowledge base is unavailable".

    Judged on the contract the same blocks are exempt, because the block says it
    carries a resource name.
    """
    blocks = [
        {"kind": "text", "text": "See the attached case video."},
        {
            "kind": "video",
            "title": "新加坡无人电动巴士.mp4",
            "media": {"title": "新加坡无人电动巴士.mp4"},
        },
    ]

    assert answer_chinese_leak(AnswerSurface.from_public_blocks(blocks), "en") == ""

    with pytest.raises(TypeError):
        answer_chinese_leak(blocks, "en")


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "text", "text": "All clear."},
        None,
        42,
        object(),
    ],
    ids=["a single block", "None", "an int", "an arbitrary object"],
)
def test_any_other_shape_is_a_type_error(payload: object) -> None:
    """Nothing reaches a second judgement path: a shape the contract does not
    name is a caller bug, reported as one."""
    with pytest.raises(TypeError):
        answer_chinese_leak(payload, "en")


def test_the_contract_is_a_frozen_value_object() -> None:
    """The guard reads a settled payload, so it must not be able to reshape one."""
    surface = AnswerSurface.from_public_blocks([{"kind": "text", "text": "All clear."}])

    assert surface.blocks[0].kind == "text"
    with pytest.raises(FrozenInstanceError):
        surface.blocks = ()


def test_an_empty_surface_leaks_nothing() -> None:
    assert AnswerSurface().leaked_chinese() == ""
    assert answer_chinese_leak(AnswerSurface(), "en") == ""


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
    surface = AnswerSurface.from_public_blocks(
        [{"kind": "text", "text": "Scan the QR code，then start charging."}]
    )

    assert answer_chinese_leak(surface, "en") != ""


# --------------------------------------------------------------------------
# Name glosses (2026-09-20, 41 live)
#
# After the card headings were fixed, the promo card still got withheld — and
# the ONLY Chinese left was the company's own name written as a gloss:
# `TrendPower (趋势智能)`. That is an identifier written twice, not a leak, and
# suppressing a whole good card over it is worse than the leak it prevented.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "TrendPower (趋势智能) joined forces with Huawei and BYD.",
        "a partnership between Huawei, BYD and TrendPower (趋势智能).",
        "TrendPower（趋势智能）won the bid.",
    ],
)
def test_a_name_gloss_beside_its_latin_form_is_not_a_leak(text: str) -> None:
    from aiops_diagnostics.i18n import chinese_leak

    assert chinese_leak(text) == ""


@pytest.mark.parametrize(
    "text",
    [
        "标题\nSingapore's First National-Level Project",
        "Industry Pain Points\n标题: 新加坡项目",
        "提供订单号可获得更精确的结果哦。",
        "Balance exhausted，please retry",
        "The stop reason is 余额耗尽停止订单.",
    ],
)
def test_the_narrow_gloss_rule_does_not_reopen_the_hole(text: str) -> None:
    """A bare Chinese name or sentence is still judged.

    Told apart from a cited proper noun needs semantics a pattern lacks, so the
    rule stays narrow rather than guessing — see the `_NAME_GLOSS` comment.
    """
    from aiops_diagnostics.i18n import chinese_leak

    assert chinese_leak(text) != ""


def test_a_bare_chinese_proper_noun_is_still_judged() -> None:
    """Known tradeoff, recorded: a bare name would be flagged. Narrow beats
    broad here, because the broad rule is how a customer reads Chinese."""
    from aiops_diagnostics.i18n import chinese_leak

    assert chinese_leak("See the case from 特来电.") != ""


# --- source-truth exemption (#376) ------------------------------------------------
#
# The exemption used to key on value SHAPE alone, so a short model-authored
# Chinese heading (`操作步骤`) on a media block passed as if it were the stored
# filename — while the same string in a text block was caught. The heading was
# never retrieved from the library, so shape cannot tell them apart; what can is
# whether the value is a resource THIS run actually returned.


def test_a_model_authored_title_on_a_media_block_is_a_leak() -> None:
    """A short Chinese heading the model invented is prose, not a resource name.

    `操作步骤` is short and punctuation-free, so `looks_like_asset_name` accepts
    it. It was never retrieved from the library, so it must be judged.
    """
    block = {"kind": "image", "title": "操作步骤", "text": "", "media": {"title": "charging-guide.png"}}
    surface = AnswerSurface.from_public_blocks([block], retrieved_titles=("charging-guide.png",))
    assert answer_chinese_leak(surface, "en") == "作操步骤"


def test_the_same_title_is_judged_the_same_way_on_any_kind() -> None:
    """Prose is prose: the block's kind must not decide whether its title is judged.

    Both calls supply the run's retrieved names, so the exemption runs on source
    truth for both — the media block's invented heading is prose exactly like the
    text block's, and both are judged.
    """
    retrieved = ("charging-guide.png",)
    media = {"kind": "image", "title": "操作步骤", "text": "", "media": {"title": "charging-guide.png"}}
    text = {"kind": "text", "title": "", "text": "操作步骤"}
    a = answer_chinese_leak(AnswerSurface.from_public_blocks([media], retrieved_titles=retrieved), "en")
    b = answer_chinese_leak(AnswerSurface.from_public_blocks([text], retrieved_titles=retrieved), "en")
    assert a == b == "作操步骤"


def test_a_retrieved_resource_name_is_still_exempt() -> None:
    """The 41 acceptance conclusion: a Chinese resource name stays exempt.

    AL-COV-10 records the live capture as 「残留中文仅媒体块 title =
    新加坡无人电动巴士.mp4 —— 资源文件名，按设计豁免」. That must not regress.
    """
    name = "新加坡无人电动巴士.mp4"
    block = {"kind": "video", "title": name, "text": "", "media": {"title": name}}
    surface = AnswerSurface.from_public_blocks([block], retrieved_titles=(name,))
    assert answer_chinese_leak(surface, "en") == ""


def test_a_reference_title_is_exempt_even_without_a_descriptor() -> None:
    """Reference blocks carry no mounted descriptor; their title is the source doc.

    `to_public_dict` mounts `media` for image/video only, so keying the
    exemption on "matches the descriptor" would withdraw it from every reference
    card — the outcome the 41 record depends on. A reference title that this run
    actually retrieved stays exempt.
    """
    name = "宣传.docx"
    block = {"kind": "reference", "title": name, "text": ""}
    surface = AnswerSurface.from_public_blocks([block], retrieved_titles=(name,))
    assert answer_chinese_leak(surface, "en") == ""


def test_an_unretrieved_reference_title_is_a_leak() -> None:
    """Source truth cuts both ways: a Chinese title no retrieval returned is prose."""
    block = {"kind": "reference", "title": "操作步骤", "text": ""}
    surface = AnswerSurface.from_public_blocks([block], retrieved_titles=("宣传.docx",))
    assert answer_chinese_leak(surface, "en") == "作操步骤"


def test_ascii_resource_names_contribute_nothing() -> None:
    """The reverse half: an ASCII retrieved name is not a new reason to withhold."""
    block = {
        "kind": "image",
        "title": "charging-guide.png",
        "text": "",
        "media": {"title": "charging-guide.png"},
    }
    surface = AnswerSurface.from_public_blocks([block], retrieved_titles=("charging-guide.png",))
    assert answer_chinese_leak(surface, "en") == ""
