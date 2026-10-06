from __future__ import annotations

import re
from typing import Any

import pytest

from aiops_diagnostics.i18n import (
    DEFAULT_LANGUAGE,
    NON_CHINESE_LANGUAGES,
    SUPPORTED_LANGUAGES,
    resolve_language,
)


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


def test_the_language_inventory_is_self_consistent() -> None:
    """The inventory is the single source; assert its PROPERTIES, not a literal.

    A literal tuple here would have to be edited by hand on every language
    addition, which is the drift this inventory exists to end (#527). What must
    hold is that the list is well-formed and that the derived constants agree
    with it — not that it happens to contain six specific tags.
    """
    from aiops_diagnostics.i18n import LANGUAGES, language_spec

    tags = [spec.tag for spec in LANGUAGES]
    assert len(set(tags)) == len(tags), "语言清单里有重复 tag"
    assert tags == list(SUPPORTED_LANGUAGES), "SUPPORTED_LANGUAGES 与清单不同步"
    assert DEFAULT_LANGUAGE in tags, "默认语言不在清单里"

    # `is_chinese` is a declared property, and the guard set derives from it —
    # NOT from "supported minus default" (#527). Those coincide only while the
    # default is the sole Chinese language.
    chinese = {spec.tag for spec in LANGUAGES if spec.is_chinese}
    assert set(tags) - chinese == NON_CHINESE_LANGUAGES
    assert DEFAULT_LANGUAGE in chinese, "默认语言应当是中文语系（清单里没这么声明）"

    # Every supported tag resolves to a spec with a prompt-usable display name.
    for tag in SUPPORTED_LANGUAGES:
        spec = language_spec(tag)
        assert spec is not None and spec.name.strip(), tag
    assert language_spec("ja") is None, "未支持的语言不该有 spec"


# Every table whose strings are shown to a user. Kept as a list so a NEW table
# added later must be registered here to be checked — the point is that
# coverage is verified structurally, not remembered.
_USER_FACING_MESSAGE_TABLES = (
    "QA_FALLBACK_MESSAGES",
    "PROMO_EMPTY_MESSAGES",
    "PROMO_UNAVAILABLE_MESSAGES",
    "CLARIFICATION_MESSAGES",
    # Registered when they were added (#536/#549). They were user-facing from
    # the start and the gate did not cover them, so the four languages added in
    # #540 left them Chinese with nothing to say so — the exact "a new table
    # must be registered here" case this comment has always described.
    "DIAGNOSIS_FAILURE_MESSAGES",
    "DIAGNOSIS_ERROR_MESSAGES",
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


def test_the_default_language_is_not_in_the_guarded_set() -> None:
    from aiops_diagnostics.i18n import (
        DEFAULT_LANGUAGE,
        NON_CHINESE_LANGUAGES,
    )

    assert DEFAULT_LANGUAGE not in NON_CHINESE_LANGUAGES
    # NOT asserted as "supported minus default": that reading is the one this
    # change replaced. A second Chinese script makes the two disagree, and this
    # assertion would then be pinning the wrong rule — see
    # `test_non_chinese_set_follows_the_declared_property_not_the_default`.


def test_prompt_only_tables_are_covered_too() -> None:
    """The prompt-only tables are inside the gate now, not beside it.

    `conversation_context`'s label and header tables are user-*invisible* (the
    model reads them), so they never appeared in `_USER_FACING_MESSAGE_TABLES`
    and no check required them to cover a new language: adding one would leave
    those tables short with nothing to say so (#527). Their values are not
    user-facing; their COVERAGE is still a property worth holding.
    """
    from aiops_diagnostics import conversation_context as cc
    from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES

    for table_name, table in (("_LABELS", cc._LABELS), ("_HEADERS", cc._HEADERS)):
        # Every key must be a language the inventory knows. A typo'd or
        # unsupported tag ("eng", "zh-Hant" before it is declared) is DEAD DATA:
        # the entry is never selected and that language silently falls back.
        ghosts = sorted(set(table) - set(SUPPORTED_LANGUAGES))
        assert not ghosts, f"{table_name} 有清单里不存在的语言键：{ghosts}"
        assert all(value for value in table.values()), f"{table_name} 有空值"
        assert cc._LABEL_FALLBACK_LANGUAGE in table, f"{table_name} 缺回退语言"


def test_non_chinese_set_follows_the_declared_property_not_the_default() -> None:
    """The rule, proven on an inventory that can tell the two readings apart.

    Asserting this over the real language list proves nothing: `zh` is both the
    only Chinese language and the default, so "declared Chinese" and "supported
    minus default" yield the same set and either implementation passes. The
    distinguishing case is a SECOND Chinese script, which is exactly the
    addition this workstream is heading for.
    """
    from aiops_diagnostics.i18n import LanguageSpec, non_chinese_languages

    synthetic = (
        LanguageSpec("zh", "Simplified Chinese", is_chinese=True),
        LanguageSpec("zh-Hant", "Traditional Chinese", is_chinese=True),
        LanguageSpec("en", "English", is_chinese=False),
    )
    result = non_chinese_languages(synthetic)

    assert result == {"en"}, f"繁体被当成了非中文：{sorted(result)}"
    # And the reading this replaced would have got it wrong — stated as an
    # assertion so the reason for the function survives.
    default = "zh"
    old_reading = {spec.tag for spec in synthetic} - {default}
    assert "zh-Hant" in old_reading and "zh-Hant" not in result
