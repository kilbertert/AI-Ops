"""System copy covers the four added languages (#540).

Covering a language in the catalog but not in the message tables produces a
half-rendered language: buttons in the new language, fallback and clarification
copy still Chinese. That is the #283 shape this workstream exists to remove, and
the coverage gate is what makes it impossible rather than remembered.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.health_report_copy import (
    HEALTH_CURVE_NAMES,
    HEALTH_STOP_FALLBACK_MESSAGES,
    HEALTH_SUMMARY_MESSAGES,
    YKC_STOP_REASON_MESSAGES,
)
from aiops_diagnostics.i18n import (
    NON_CHINESE_LANGUAGES,
    SUPPORTED_LANGUAGES,
    ZERO_ORDER_REMINDER_MESSAGES,
    chinese_leak,
    clarification_message,
    diagnosis_error_message,
    diagnosis_failure_message,
    zero_order_reminder,
)
from aiops_diagnostics.shortcut_lifecycle import _BUNDLED_SHORTCUTS, _SHARED_ACTION_FIELDS

_ADDED = ("vi", "mn", "th", "km")


def test_the_four_languages_are_in_the_inventory() -> None:
    for language in _ADDED:
        assert language in SUPPORTED_LANGUAGES, language
        assert language in NON_CHINESE_LANGUAGES, language


def test_the_four_languages_are_added_exactly_once() -> None:
    assert len(set(SUPPORTED_LANGUAGES)) == len(SUPPORTED_LANGUAGES)
    assert SUPPORTED_LANGUAGES == (
        "zh",
        "en",
        "de",
        "fr",
        "es",
        "pt",
        "vi",
        "mn",
        "th",
        "km",
    )


@pytest.mark.parametrize("language", _ADDED)
def test_no_system_copy_is_chinese_for_the_added_languages(language: str) -> None:
    """EVERY key of every table, not one representative per table.

    The first version sampled a single key from each — `insufficient_evidence`
    from the failure table, `DIAGNOSIS_FAILED` from the error table. Putting
    Chinese back into a *different* key (`incomplete`) therefore left the suite
    green. A gate that checks one key per table guards one key per table.
    """
    from aiops_diagnostics.i18n import (
        CLARIFICATION_MESSAGES,
        DIAGNOSIS_ERROR_MESSAGES,
        DIAGNOSIS_FAILURE_MESSAGES,
        QA_FALLBACK_MESSAGES,
    )

    samples = [zero_order_reminder(language)]
    samples += [QA_FALLBACK_MESSAGES[language][k] for k in QA_FALLBACK_MESSAGES["zh"]]
    samples += [CLARIFICATION_MESSAGES[language][k] for k in CLARIFICATION_MESSAGES["zh"]]
    samples += [DIAGNOSIS_FAILURE_MESSAGES[language][k] for k in DIAGNOSIS_FAILURE_MESSAGES["zh"]]
    samples += [DIAGNOSIS_ERROR_MESSAGES[language][k] for k in DIAGNOSIS_ERROR_MESSAGES["zh"]]

    # And the render-time lookups really resolve to those strings.
    for key in DIAGNOSIS_FAILURE_MESSAGES["zh"]:
        samples.append(diagnosis_failure_message(language, key))
    for key in DIAGNOSIS_ERROR_MESSAGES["zh"]:
        samples.append(diagnosis_error_message(language, key))
    for key in CLARIFICATION_MESSAGES["zh"]:
        samples.append(clarification_message(language, key))

    for text in samples:
        assert chinese_leak(text) == "", f"{language}: {text[:40]}"


@pytest.mark.parametrize("language", _ADDED)
def test_health_report_copy_is_localized_for_the_added_languages(language: str) -> None:
    for field in HEALTH_CURVE_NAMES["zh"]:
        assert chinese_leak(HEALTH_CURVE_NAMES[language][field]) == ""
    for key in HEALTH_SUMMARY_MESSAGES["zh"]:
        assert chinese_leak(HEALTH_SUMMARY_MESSAGES[language][key]) == ""
    for code in YKC_STOP_REASON_MESSAGES["zh"]:
        assert chinese_leak(YKC_STOP_REASON_MESSAGES[language][code]) == ""
    for key in HEALTH_STOP_FALLBACK_MESSAGES["zh"]:
        assert chinese_leak(HEALTH_STOP_FALLBACK_MESSAGES[language][key]) == ""


def test_every_health_copy_table_has_the_same_keys_in_every_language() -> None:
    for name, table in (
        ("HEALTH_SUMMARY_MESSAGES", HEALTH_SUMMARY_MESSAGES),
        ("HEALTH_CURVE_NAMES", HEALTH_CURVE_NAMES),
        ("HEALTH_STOP_FALLBACK_MESSAGES", HEALTH_STOP_FALLBACK_MESSAGES),
        ("YKC_STOP_REASON_MESSAGES", YKC_STOP_REASON_MESSAGES),
    ):
        keys = set(table["zh"])
        for language in SUPPORTED_LANGUAGES:
            assert set(table[language]) == keys, f"{name}[{language}] 键集合不一致"


def test_the_zero_order_reminder_covers_every_language() -> None:
    """A flat table (tag -> string), so it gets its own check rather than the
    key-set comparison the nested tables use."""
    missing = [lang for lang in SUPPORTED_LANGUAGES if lang not in ZERO_ORDER_REMINDER_MESSAGES]
    assert not missing, f"提醒语缺 {missing}"
    for language in _ADDED:
        assert chinese_leak(zero_order_reminder(language)) == ""


def test_every_shortcut_seed_field_covers_every_language() -> None:
    """The seed is the third storage layer: code tables, seed, and live rows.

    Fixing the code tables alone leaves the bundled copy short, and a shortcut
    with a missing language falls back to Chinese while the response echoes the
    requested one — the defect that does not raise.
    """
    for entry, actions in _BUNDLED_SHORTCUTS:
        for code, fields in actions.items():
            for field in ("labels", "descriptions", "question_templates"):
                if field not in fields:
                    continue
                missing = [lang for lang in SUPPORTED_LANGUAGES if lang not in fields[field]]
                assert not missing, f"{entry}/{code}/{field} 缺 {missing}"
                for language in _ADDED:
                    assert chinese_leak(fields[field][language]) == "", (entry, code, field, language)

    for code, fields in _SHARED_ACTION_FIELDS.items():
        missing = [lang for lang in SUPPORTED_LANGUAGES if lang not in fields["question_templates"]]
        assert not missing, f"shared/{code} 缺 {missing}"
