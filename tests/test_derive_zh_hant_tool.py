"""`tools/derive_zh_hant.py` must compare VALUES, not presence (#531 review).

The first version only asked "does this module mention `zh-Hant` at all". That
is worthless as a gate: once ANY key carries the tag, every later edit to a `zh`
string passes, and the stale Traditional copy ships — which is the exact defect
the script exists to prevent. The check is therefore driven from the `zh`
authority, walking every entry.

`opencc` is deliberately NOT a dependency of this repo (see the script's
docstring), so the real converter cannot run in CI. These tests exercise the
walker directly with a stand-in converter that is a SCRIPT-AWARE no-op, which is
enough to prove the comparison and the traversal — the properties that can
regress. That the shipped `zh-Hant` copy equals real opencc output is a
development-side check the author runs; it cannot be asserted here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load():
    """Import the tool by path — `tools/` is not a package."""
    spec = importlib.util.spec_from_file_location("derive_zh_hant", ROOT / "tools" / "derive_zh_hant.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load()


@pytest.fixture(scope="module")
def cc(tool):
    """A stand-in converter: maps the source marker to the target marker.

    The real opencc conversion is not what these tests are about; the walker is.
    A deterministic, injectable converter makes that separation explicit rather
    than hiding it behind a dependency CI cannot install.
    """

    class _StandIn:
        def convert(self, text: str) -> str:
            return text.replace("简", "繁")

    return _StandIn()


def test_a_stale_entry_is_reported(tool, cc) -> None:
    """`zh` edited, `zh-Hant` left behind — the shape the old gate waved through."""
    stale: list[str] = []
    tool._check_language_map(
        {"zh": {"normal": "简体壹"}, "zh-Hant": {"normal": "繁体旧"}},
        "T",
        cc,
        stale,
    )
    assert len(stale) == 1
    assert "不一致" in stale[0]
    assert "['zh-Hant']['normal']" in stale[0]


def test_a_missing_entry_is_reported_as_a_fallback(tool, cc) -> None:
    """A missing key falls back to `zh` at runtime: Traditional users get Simplified."""
    stale: list[str] = []
    tool._check_language_map({"zh": {"normal": "简体壹"}, "zh-Hant": {}}, "T", cc, stale)
    assert len(stale) == 1
    assert "缺失" in stale[0]


def test_a_correct_entry_is_silent(tool, cc) -> None:
    """The check must be able to say yes — otherwise it is a constant `[]`."""
    stale: list[str] = []
    tool._check_language_map({"zh": {"normal": "简体壹"}, "zh-Hant": {"normal": "繁体壹"}}, "T", cc, stale)
    assert stale == []


def test_a_flat_table_is_handled(tool, cc) -> None:
    """`{tag: str}` and `{tag: {field: str}}` are both real shapes here."""
    stale: list[str] = []
    tool._check_language_map({"zh": "简体", "zh-Hant": "繁体"}, "T", cc, stale)
    assert stale == []
    tool._check_language_map({"zh": "简体", "zh-Hant": "陈旧"}, "T", cc, stale)
    assert len(stale) == 1


def test_a_table_without_source_is_skipped(tool, cc) -> None:
    """Nothing to derive from — not an error, and not a silent pass of content."""
    stale: list[str] = []
    tool._check_language_map({"en": "English"}, "T", cc, stale)
    assert stale == []


def test_the_nested_shortcut_seed_is_walked(tool, cc, monkeypatch) -> None:
    """The shortcut seeds are `entry -> code -> field -> {tag: text}`.

    They are the copy a FRESH install creates, so a gap here is a Traditional
    button that reads Simplified the day it is created. A flat-only walker would
    never look at them.
    """
    from aiops_diagnostics import shortcut_lifecycle

    field = {"zh": "简体按钮", "zh-Hant": "陈旧按钮"}  # deliberately stale
    monkeypatch.setattr(shortcut_lifecycle, "_BUNDLED_SHORTCUTS", (("consumer", {"c": {"labels": field}}),))
    monkeypatch.setattr(shortcut_lifecycle, "_SHARED_ACTION_FIELDS", {})

    stale = tool.check_python_tables(cc)
    assert any("_BUNDLED_SHORTCUTS[consumer][c].labels" in item for item in stale), stale


def test_the_walker_reaches_the_real_tables(tool, cc) -> None:
    """Traversal must actually visit the shipped tables, not just its own input.

    A walker that silently found nothing would pass every unit test above. Here
    the converter is identity-like, so EVERY real entry must be reported — the
    error strings naming real table paths are the proof the walker reached them.
    """

    class _Identity:
        def convert(self, text: str) -> str:
            return text  # never equals a Traditional entry, so all are reported

    stale = tool.check_python_tables(_Identity())

    # 实测 83 条；下限取 50，留出增删文案的余量，同时足以证伪"一条都没遍历到"。
    assert len(stale) >= 50, f"遍历没有覆盖到真实文案表，只报了 {len(stale)} 条"
    for marker in (
        "aiops_diagnostics.i18n.QA_FALLBACK_MESSAGES",
        "aiops_diagnostics.health_report_copy.HEALTH_SUMMARY_MESSAGES",
        "shortcut_lifecycle._BUNDLED_SHORTCUTS",
        "shortcut_lifecycle._SHARED_ACTION_FIELDS",
    ):
        assert any(marker in item for item in stale), f"遍历没有触达到 {marker}"
