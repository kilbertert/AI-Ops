"""泰/高棉模型提示已开放；旧FAQ分词的反例与拒绝仍保留。"""

from __future__ import annotations

import pytest

from aiops_diagnostics.common.i18n import (
    SUPPORTED_LANGUAGES,
    can_prompt_in,
    chinese_leak,
    free_text_unavailable_message,
    resolve_language,
)
from aiops_diagnostics.conversation.faq import FAQCatalog

_READ_ONLY = ("th", "km")


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_catalog_serves_the_language(language: str) -> None:
    """Readable: whole catalog, not a sample."""
    catalog = FAQCatalog.bundled()
    for entry in catalog.catalog("operator"):
        answer = catalog.answer("operator", entry["question_id"], language)
        assert chinese_leak(answer["question"]) == "", entry["question_id"]
        assert chinese_leak(answer["answer"]) == "", entry["question_id"]
    assert catalog.served_language("operator", language) == language


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_language_is_promptable(language: str) -> None:
    """模型支持语言与旧分词能力分开。"""
    assert language in SUPPORTED_LANGUAGES, "读的语言也必须在受支持集里"
    assert can_prompt_in(language) is True


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_proposal_is_echoed_honestly(language: str) -> None:
    """The user's language is recognised — they are told what we cannot do.

    Falling back to `zh` at the resolver would tell a Thai speaker we do not
    know their language. Recognising it and refusing the free-text path tells
    them the truth: we can show you this in Thai, we cannot answer a Thai
    question.
    """
    assert resolve_language(f"{language}-TH,en;q=0.9") == language


@pytest.mark.parametrize("language", _READ_ONLY)
def test_the_refusal_is_in_their_own_language(language: str) -> None:
    """Not a Chinese sentence explaining that we cannot do Thai.

    The refusal is copy WE write, so it localises like any other: the user reads
    their own language while the model is never asked to write in it.
    """
    message = free_text_unavailable_message(language)
    assert message.strip()
    assert chinese_leak(message) == "", message
    assert "快捷" not in message and "shortcut" not in message.lower()


def test_the_refusal_message_covers_every_language() -> None:
    """A missing entry would hand the default language's sentence to a reader
    who asked for another — the silent-fallback shape this workstream removes."""
    for language in SUPPORTED_LANGUAGES:
        assert free_text_unavailable_message(language).strip()
        for other in SUPPORTED_LANGUAGES:
            if other != language:
                assert free_text_unavailable_message(language) != free_text_unavailable_message(other), (
                    f"{language} 与 {other} 的拒绝文案相同"
                )


def test_the_boundary_is_the_read_only_languages_and_nothing_else() -> None:
    """Every other language must remain askable — the split is a boundary, not
    a general disable."""
    for language in SUPPORTED_LANGUAGES:
        if language in _READ_ONLY:
            continue
        assert can_prompt_in(language) is True, language
