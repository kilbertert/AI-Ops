"""The FAQ merge tool's DEFAULTS must be safe to run (#529 review).

Both defects this pins were defaults, not logic: a language default that only
suited one platform, and a version default that predated the data. A tool whose
plain re-run rewrites published metadata backward is worse than one that refuses
to run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
TOOL = ROOT / "tools" / "merge_faq_i18n.py"
CATALOG = ROOT / "src" / "aiops_diagnostics" / "faq_catalog.json"
RECOMMENDATIONS = ROOT / "src" / "aiops_diagnostics" / "faq_recommendations.json"


def _run(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _restore(original: str, recommendations: str) -> None:
    """Both files the tool writes. Restoring only the catalog left the
    recommendations file with a `2099.01.01` version from the explicit-version
    case — a test that dirties the tree on every run."""
    CATALOG.write_text(original, encoding="utf-8")
    RECOMMENDATIONS.write_text(recommendations, encoding="utf-8")


def test_the_default_languages_come_from_the_platforms_own_answer_files() -> None:
    """`--platform operator` alone must not look for files that do not exist.

    A single fixed default (`en,de,fr,es,pt`) serves whichever platform was
    written first; for operator it raised FileNotFoundError before reading a
    single entry, because operator's translations landed as vi/mn.
    """
    original = CATALOG.read_text(encoding="utf-8")
    recommendations = RECOMMENDATIONS.read_text(encoding="utf-8")
    try:
        consumer = _run()
        assert consumer["platform"] == "consumer"
        assert consumer["languages"] == ["en", "de", "fr", "es", "pt"]
        operator = _run("--platform", "operator")
        assert operator["platform"] == "operator"
        assert operator["languages"] == ["vi", "mn"]
    finally:
        _restore(original, recommendations)


def test_a_plain_re_run_does_not_roll_the_version_backward() -> None:
    """The version default is the CURRENT one, not a literal that predates it.

    Before the fix, running the tool with its defaults stamped `2026.09.12`
    over a `2026.10.07` catalog — the content stayed, the published revision
    went backward.
    """
    original = CATALOG.read_text(encoding="utf-8")
    recommendations = RECOMMENDATIONS.read_text(encoding="utf-8")
    try:
        before = json.loads(original)["faq_version"]
        after = _run()["version"]
        assert after == before, f"默认重跑把版本从 {before} 改成了 {after}"
    finally:
        _restore(original, recommendations)


def test_an_explicit_version_still_wins() -> None:
    original = CATALOG.read_text(encoding="utf-8")
    recommendations = RECOMMENDATIONS.read_text(encoding="utf-8")
    try:
        assert _run("--version", "2099.01.01")["version"] == "2099.01.01"
    finally:
        _restore(original, recommendations)
