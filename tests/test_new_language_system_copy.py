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


def test_the_inventory_has_no_duplicates() -> None:
    """No duplicates, and every added language still present.

    The exact tuple is NOT asserted: it grows with each language slice (vi/mn,
    th/km, zh-Hant), so pinning it here would fail on every addition and have to
    be edited by hand — the drift the inventory exists to end.
    """
    assert len(set(SUPPORTED_LANGUAGES)) == len(SUPPORTED_LANGUAGES)
    for language in ("vi", "mn", "th", "km", "zh-Hant"):
        assert language in SUPPORTED_LANGUAGES, language


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


# --------------------------------------------------------------------------
# #540 review: a language can be accepted by the resolver and still have no
# content. The response must then report the language it actually served.
# --------------------------------------------------------------------------


def test_faq_reports_the_served_language_not_the_requested_one() -> None:
    """A catalog entry without a translation is served in the authority language.

    Accepting the four new tags made `vi`/`th`/... resolvable, while the bundled
    catalog carries only en/de/es/fr/pt for `consumer` and nothing at all for
    `operator`. Echoing the requested tag beside Chinese text is a false
    statement about the payload — the same shape this workstream removes
    everywhere else.
    """
    from aiops_diagnostics.faq import FAQCatalog

    catalog = FAQCatalog.bundled()
    for language in _ADDED:
        assert catalog.entry_served_language("consumer", "consumer.faq.q001", language) == "zh"
        assert catalog.served_language("consumer", language) == "zh"
    # A language the catalog DOES carry is reported as itself.
    assert catalog.entry_served_language("consumer", "consumer.faq.q001", "en") == "en"
    assert catalog.served_language("consumer", "en") == "en"
    # The operator catalog USED to have no translations at all, so even an old
    # language was served Chinese there. #565 completed it, so what is asserted
    # now is the same RULE on a language the operator side really lacks — an
    # unsupported tag, and the one gap left anywhere in the catalog.
    assert catalog.served_language("operator", "ja") == "zh"
    unknown_or_missing = [
        language
        for language in ("vi", "mn", "th", "km")
        if catalog.served_language("operator", language) != language
    ]
    for language in unknown_or_missing:
        assert catalog.served_language("operator", language) == "zh"


def test_the_public_catalog_entry_shape_is_unchanged() -> None:
    """The four-key entry contract survives: the honesty fix is not a shape change."""
    from aiops_diagnostics.faq import FAQCatalog

    entry = FAQCatalog.bundled().catalog("consumer")[0]
    assert set(entry) == {"question_id", "question", "answer", "format"}


def test_a_shortcut_row_reports_the_language_its_copy_is_in() -> None:
    """Rows published before a language existed keep Chinese copy; say so.

    The seed is not the listing: `seed_bundled` skips persisted rows, so a
    deployment with already-published shortcuts serves the OLD copy whatever the
    seed now contains. Reporting the requested tag for that row is the defect.
    """
    from aiops_diagnostics.shortcut_lifecycle import Shortcut

    row = Shortcut(
        shortcut_id="sc_1",
        tenant_id="__platform__",
        business_entry="consumer",
        code="case_exploration",
        intent="case_exploration",
        requires_order=False,
        sort_order=10,
        status="published",
        revision=1,
        labels={"zh": "客户案例", "en": "Customer Cases"},
        descriptions={"zh": "查看案例", "en": "Explore cases"},
        question_templates={"zh": "我想看看客户案例", "en": "Show me customer cases"},
        target_agent_version=None,
        jump_path=None,
        published_version=1,
        created_by="test",
        created_at="t",
        updated_at="t",
    )
    assert row.served_language("en") == "en"
    assert row.served_language("th") == "zh", "泰语行没有文案，应报实际服务的语言"
    assert row.public("th")["language"] == "zh"
    # And the row still renders — the fallback is not removed, only reported.
    assert row.public("th")["label"] == "客户案例"


def _catalog_with(consumer_i18n: dict[str, dict[str, dict[str, str]]]):
    """A minimal two-entry catalog, so list-level coverage is observable."""
    from aiops_diagnostics.faq import FAQCatalog

    def entry(qid: str, en: bool):
        e = {
            "question_id": qid,
            "question": f"问题 {qid}",
            "answer": "答案",
            "format": "text",
        }
        if en:
            e["i18n"] = {"en": {"question": "Q", "answer": "A"}}
        return e

    return FAQCatalog(
        {
            "faq_version": "test",
            "platforms": {
                "consumer": [entry("consumer.faq.q001", True), entry("consumer.faq.q002", False)],
                "operator": [entry("operator.faq.q001", False)],
            },
        }
    )


def test_a_partially_translated_catalog_is_not_claimed_to_be_translated() -> None:
    """The LIST is in a language only when EVERY entry is.

    One translated entry among many does not make an English catalog, and
    reporting `en` for it would describe the untranslated entries too.
    """
    catalog = _catalog_with({})
    assert catalog.served_language("consumer", "en") == "zh"
    assert catalog.served_language("consumer", "zh") == "zh"
    # Every consumer entry translated -> the list really is English.
    from aiops_diagnostics.faq import FAQCatalog

    full = FAQCatalog(
        {
            "faq_version": "test",
            "platforms": {
                "consumer": [
                    {
                        "question_id": "consumer.faq.q001",
                        "question": "问题",
                        "answer": "答案",
                        "format": "text",
                        "i18n": {"en": {"question": "Q", "answer": "A"}},
                    }
                ],
                "operator": [
                    {
                        "question_id": "operator.faq.q001",
                        "question": "问题",
                        "answer": "答案",
                        "format": "text",
                    }
                ],
            },
        }
    )
    assert full.served_language("consumer", "en") == "en"


def test_an_empty_shortcut_list_reports_the_requested_language() -> None:
    """No rows means no text, so nothing can contradict the request.

    Reporting the authority language for an empty list would describe a payload
    that does not exist. Asserted on the rule directly: the wiring is a
    one-liner that passes the rows it just read, and the non-empty cases are
    covered end-to-end through the listing endpoint above.
    """
    from aiops_diagnostics.gateway_api import _shortcut_list_language

    assert _shortcut_list_language([], "de") == "de"

    class _Row:
        def __init__(self, served: str) -> None:
            self._served = served

        def served_language(self, requested: str) -> str:
            return self._served

    # All rows agree -> that language; otherwise the authority language, because
    # one field cannot describe a mixed list.
    assert _shortcut_list_language([_Row("th"), _Row("th")], "th") == "th"
    assert _shortcut_list_language([_Row("th"), _Row("zh")], "th") == "zh"
