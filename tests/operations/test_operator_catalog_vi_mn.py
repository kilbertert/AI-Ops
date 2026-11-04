"""The operator FAQ catalog serves Vietnamese and Mongolian (#529).

`operator` carried NO translations at all before this: every language fell back
to Chinese while the endpoint echoed the requested tag. Adding two languages is
therefore also the first time this surface can be honest about what it serves —
`served_language` (#540) reports `zh` only while an entry really lacks the
language, and these tests pin both halves.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.common.i18n import SUPPORTED_LANGUAGES, chinese_leak
from aiops_diagnostics.conversation.faq import FAQCatalog

_ADDED = ("vi", "mn")


def _catalog() -> FAQCatalog:
    return FAQCatalog.bundled()


@pytest.mark.parametrize("language", _ADDED)
def test_every_operator_entry_has_the_new_language(language: str) -> None:
    """Whole catalog, not a sample: a partial translation is what #540 was about."""
    catalog = _catalog()
    for entry in catalog.catalog("operator"):
        answer = catalog.answer("operator", entry["question_id"], language)
        assert chinese_leak(answer["question"]) == "", entry["question_id"]
        assert chinese_leak(answer["answer"]) == "", entry["question_id"]


@pytest.mark.parametrize("language", _ADDED)
def test_the_operator_list_reports_the_served_language(language: str) -> None:
    catalog = _catalog()
    assert catalog.served_language("operator", language) == language
    assert catalog.entry_served_language("operator", "operator.faq.q001", language) == language


def test_the_operator_catalog_is_now_complete() -> None:
    """The catalog covers every supported language — asserted positively.

    This is what replaced the "still reports zh for a missing language" test:
    with no gap left that test collected zero cases and could never fail.
    """
    catalog = _catalog()
    missing = [
        language
        for language in SUPPORTED_LANGUAGES
        if catalog.served_language("operator", language) != language
    ]
    assert missing == [], f"operator 目录仍缺：{missing}"


def test_the_two_platform_catalogs_stay_separate() -> None:
    """Two platforms, two catalogs — content added to one must not appear in the other.

    The original form proved this by asserting `consumer` had NOT gained `vi`.
    #565 translated consumer's `vi` separately, so that assertion is now false
    while the property it guarded still holds. It is asserted the way the
    property actually reads: each platform serves its OWN entry text.
    """
    catalog = _catalog()
    for language in ("en", "de", "fr", "es", "pt", "vi", "mn"):
        assert catalog.served_language("consumer", language) == language
        assert catalog.served_language("operator", language) == language
        assert catalog.answer("consumer", "consumer.faq.q001", language) != catalog.answer(
            "operator", "operator.faq.q001", language
        ), f"{language}: 两个平台返回了同一段正文"


def test_tenant_facets_of_the_catalog_are_unchanged() -> None:
    """Adding copy must not change the entry set any caller counts on."""
    catalog = _catalog()
    assert len(catalog.catalog("operator")) == 17
    assert len(catalog.catalog("consumer")) == 28
