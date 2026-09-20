from __future__ import annotations

import re
from typing import Any

import pytest

from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, resolve_language


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # Missing, blank, or unparseable headers fall back to the default.
        (None, "zh"),
        ("", "zh"),
        ("   ", "zh"),
        (";", "zh"),
        (";;,", "zh"),
        # Region/script subtags fold onto supported base tags; case folds.
        ("en-US", "en"),
        ("zh-Hans-CN", "zh"),
        ("pt-BR", "pt"),
        ("DE", "de"),
        # Highest q wins regardless of list order.
        ("fr;q=0.9, en;q=0.5", "fr"),
        ("en;q=0.5, fr;q=0.9", "fr"),
        ("de;q=0.1, fr;q=0.2, en", "en"),
        ("zh;q=0.2, en;q=0.3", "en"),
        # A bare tag defaults to q=1 and beats any lower weight.
        ("pt-BR;q=0.8, en", "en"),
        ("fr;q=0.8, es;q=0.9", "es"),
        ("en-US,en;q=0.9", "en"),
        # Equal q keeps the first occurrence.
        ("fr, de", "fr"),
        ("fr;q=0.5, de;q=0.5", "fr"),
        # q=0 means "not acceptable" (RFC 7231) and is excluded.
        ("fr;q=0, en", "en"),
        ("fr;q=0.0", "zh"),
        # Wildcards, unsupported languages, and malformed weights are ignored.
        ("*", "zh"),
        ("ja, ko", "zh"),
        ("*;q=0.9, ja;q=0.8", "zh"),
        ("en;q=abc", "zh"),
        ("en;q=1.5", "zh"),
        ("en;q=-1", "zh"),
        ("en;q=abc, fr", "fr"),
    ],
)
def test_resolve_language_matrix(header: Any, expected: str) -> None:
    assert resolve_language(header) == expected


def test_supported_languages_and_default_match_the_i18n_catalog_plan() -> None:
    assert DEFAULT_LANGUAGE == "zh"
    assert SUPPORTED_LANGUAGES == ("zh", "en", "de", "fr", "es", "pt")
    assert len(set(SUPPORTED_LANGUAGES)) == len(SUPPORTED_LANGUAGES)


# Every table whose strings are shown to a user. Kept as a list so a NEW table
# added later must be registered here to be checked — the point is that
# coverage is verified structurally, not remembered.
_USER_FACING_MESSAGE_TABLES = (
    "QA_FALLBACK_MESSAGES",
    "PROMO_EMPTY_MESSAGES",
    "PROMO_UNAVAILABLE_MESSAGES",
    "CLARIFICATION_MESSAGES",
)


@pytest.mark.parametrize("table_name", _USER_FACING_MESSAGE_TABLES)
def test_every_user_facing_table_covers_all_supported_languages(table_name: str) -> None:
    """resolve_language accepts all six languages, so a table missing some of
    them makes the service announce a language it then answers in Chinese.

    41 live (2026-09-18): the clarification and promo tables carried only zh+en
    while `language` echoed de/fr/es/pt. Structural check rather than trusting
    each table to have been filled in by hand."""
    from aiops_diagnostics import i18n

    table = getattr(i18n, table_name)
    missing = [lang for lang in SUPPORTED_LANGUAGES if lang not in table]
    assert not missing, f"{table_name} is missing: {missing}"

    # And each language's entries must be non-empty for every key in the table.
    keys = set(table[DEFAULT_LANGUAGE])
    assert keys, f"{table_name} has no keys under the default language"
    for lang in SUPPORTED_LANGUAGES:
        assert set(table[lang]) == keys, (
            f"{table_name}[{lang}] keys differ from {DEFAULT_LANGUAGE}: {sorted(set(table[lang]) ^ keys)}"
        )
        for key, text in table[lang].items():
            assert isinstance(text, str) and text.strip(), f"{table_name}[{lang}][{key}] is empty"
            # A wrap that split after "." must keep the separating space. Not
            # hypothetical: an automatic line-wrapper dropped it while these
            # tables were being filled, producing "verfügbar.Bitte".
            assert not re.search(r"[.!?][A-Za-zÀ-ɏ]", text), (
                f"{table_name}[{lang}][{key}] lost a space after a sentence end: {text!r}"
            )


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_clarification_message_never_falls_back_to_chinese_when_translated(language: str) -> None:
    """A supported language must get its OWN copy, not the zh fallback."""
    from aiops_diagnostics.i18n import clarification_message

    zh = clarification_message("zh", "order_no")
    for key in ("order_no", "context", "wrong_entry"):
        text = clarification_message(language, key)
        assert text.strip(), (language, key)
        if language != "zh":
            assert text != zh, f"{language}/{key} fell back to Chinese"


# --------------------------------------------------------------------------
# Shared answer-language guard (#293)
#
# The defect class this covers appeared three times, each time on a different
# surface, because the guard lived in one place (the diagnosis validator) while
# answers left through several. These tests pin the shared judgement and the
# media exemption that keeps resource filenames intact.
# --------------------------------------------------------------------------


def test_chinese_leak_names_the_offending_characters() -> None:
    from aiops_diagnostics.i18n import chinese_leak

    assert chinese_leak('stop reason "余额耗尽停止订单" (balance exhausted)') == "余停单尽止耗订额"


def test_chinese_leak_is_empty_for_ascii_and_for_english_prose() -> None:
    from aiops_diagnostics.i18n import chinese_leak

    assert chinese_leak("stopped_reason_code=-1; balance_insufficient_stop=1") == ""
    assert chinese_leak("") == ""


def test_non_chinese_languages_is_derived_from_the_supported_set() -> None:
    from aiops_diagnostics.i18n import (
        DEFAULT_LANGUAGE,
        NON_CHINESE_LANGUAGES,
        SUPPORTED_LANGUAGES,
    )

    assert DEFAULT_LANGUAGE not in NON_CHINESE_LANGUAGES
    assert set(SUPPORTED_LANGUAGES) - {DEFAULT_LANGUAGE} == set(NON_CHINESE_LANGUAGES)
