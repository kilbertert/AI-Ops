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


def _run_expecting_failure(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode != 0, f"预期失败但成功了：{result.stdout[-200:]}"
    return result.stdout + result.stderr


def test_changed_copy_without_an_explicit_version_is_refused() -> None:
    """New copy must not ship under the OLD revision.

    Preserving the current version on an unchanged rerun is right; preserving it
    when the copy changed is not — consumers comparing `faq_version` see no
    update at all. The tool refuses BEFORE writing, so a refused run leaves the
    tree exactly as it found it.
    """
    answers = ROOT / "tools" / "faq_i18n" / "operator_answers_vi.json"
    original_answers = answers.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")
    recommendations = RECOMMENDATIONS.read_text(encoding="utf-8")
    try:
        payload = json.loads(original_answers)
        payload["operator.faq.q001"] = payload["operator.faq.q001"] + " (edited)"
        answers.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        message = _run_expecting_failure("--platform", "operator")
        assert "--version" in message
        # And nothing was written on the way out.
        assert CATALOG.read_text(encoding="utf-8") == catalog
    finally:
        answers.write_text(original_answers, encoding="utf-8")
        _restore(catalog, recommendations)


def test_a_missing_answer_file_is_refused_rather_than_silently_dropped() -> None:
    """A language that is already published may not vanish by omission.

    `default_languages` discovers what has an answer file TODAY. If one went
    missing, merging the rest and preserving the old entries leaves copy that no
    longer matches its source, and the run says nothing — the worst outcome,
    because it looks like a successful run.
    """
    answers = ROOT / "tools" / "faq_i18n" / "operator_answers_vi.json"
    hidden = answers.with_suffix(".hidden")
    catalog = CATALOG.read_text(encoding="utf-8")
    recommendations = RECOMMENDATIONS.read_text(encoding="utf-8")
    try:
        answers.rename(hidden)
        message = _run_expecting_failure("--platform", "operator")
        assert "vi" in message
        assert CATALOG.read_text(encoding="utf-8") == catalog
    finally:
        hidden.rename(answers)
        _restore(catalog, recommendations)


def test_removing_a_language_is_allowed_when_asked_for_explicitly() -> None:
    """Refusing by omission must not make a deliberate removal impossible."""
    answers = ROOT / "tools" / "faq_i18n" / "operator_answers_vi.json"
    hidden = answers.with_suffix(".hidden")
    catalog = CATALOG.read_text(encoding="utf-8")
    recommendations = RECOMMENDATIONS.read_text(encoding="utf-8")
    try:
        answers.rename(hidden)
        assert _run("--platform", "operator", "--languages", "mn")["languages"] == ["mn"]
    finally:
        hidden.rename(answers)
        _restore(catalog, recommendations)
