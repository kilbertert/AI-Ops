"""The operator FAQ catalog serves Vietnamese and Mongolian (#529).

`operator` carried NO translations at all before this: every language fell back
to Chinese while the endpoint echoed the requested tag. Adding two languages is
therefore also the first time this surface can be honest about what it serves —
`served_language` (#540) reports `zh` only while an entry really lacks the
language, and these tests pin both halves.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.faq import FAQCatalog
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES, chinese_leak

_ADDED = ("vi", "mn")
#: Languages the operator catalog still does NOT carry. Kept explicit so adding
#: one later has to be a deliberate edit here too. (th/km left this list in
#: #530, en/de/fr/es/pt in #565 — moving one over is the point of keeping it
#: written down. The list is now EMPTY, so the "still reports zh" test below
#: has nothing to run on and is skipped rather than deleted: the next language
#: added to the operator catalog is what brings it back.)
_NOT_YET: tuple[str, ...] = ()


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


@pytest.mark.skipif(not _NOT_YET, reason="operator 目录已覆盖全部语言（#565）—— 没有可测的缺口")
@pytest.mark.parametrize("language", _NOT_YET)
def test_a_language_the_operator_catalog_lacks_still_reports_zh(language: str) -> None:
    """The honesty fix from #540 must not be weakened by adding languages.

    `served_language` reports `zh` only while an entry really lacks the
    language. That property has no observable case today because the operator
    catalog is complete — but it is what keeps a future partial translation
    honest, so the test is kept and turns back on with the next gap.
    """
    catalog = _catalog()
    assert catalog.served_language("operator", language) == "zh"
    assert catalog.entry_served_language("operator", "operator.faq.q001", language) == "zh"


def test_the_operator_catalog_is_now_complete() -> None:
    """And the positive half is asserted, so completing it is not only a skip.

    Without this, emptying `_NOT_YET` would silently make the module test less
    than it did — the gap test would skip and nothing would say the catalog got
    to complete.
    """
    catalog = _catalog()
    missing = [
        language
        for language in SUPPORTED_LANGUAGES
        if catalog.served_language("operator", language) != language
    ]
    assert missing == [], f"operator 目录仍缺：{missing}"


def test_the_consumer_catalog_is_untouched_by_the_operator_merge() -> None:
    """Two platforms, two catalogs. The merge must not have reached across."""
    catalog = _catalog()
    for language in ("en", "de", "fr", "es", "pt"):
        assert catalog.served_language("consumer", language) == language
    # And the consumer catalog has NOT gained vi/mn through the operator merge:
    # those are a separate slice, translated separately in #565.
    assert catalog.served_language("consumer", "vi") == "zh"


def test_tenant_facets_of_the_catalog_are_unchanged() -> None:
    """Adding copy must not change the entry set any caller counts on."""
    catalog = _catalog()
    assert len(catalog.catalog("operator")) == 17
    assert len(catalog.catalog("consumer")) == 28


def test_every_supported_language_is_still_representable() -> None:
    """A sanity net: the inventory and the catalog agreement this ticket relies on."""
    catalog = _catalog()
    for language in SUPPORTED_LANGUAGES:
        assert catalog.served_language("operator", language) in SUPPORTED_LANGUAGES
