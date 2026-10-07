"""The FAQ merge tool's DEFAULTS must be safe to run (#529 review).

Both defects this pins were defaults, not logic: a language default that only
suited one platform, and a version default that predated the data. A tool whose
plain re-run rewrites published metadata backward is worse than one that refuses
to run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
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
        # The default grows as a platform gains languages, so it is asserted as
        # a SUPERSET of the set it had when this test was written plus the four
        # #565 added — a fixed literal would have to be edited every time, and
        # editing it is exactly the drift this test is meant to catch.
        assert {"en", "de", "fr", "es", "pt"} <= set(consumer["languages"])
        assert {"vi", "mn", "th", "km"} <= set(consumer["languages"]), (
            "consumer 默认集里少了 #565 新增的语言 —— 它们没有宽表列，是靠 questions_<lang>.json 被发现的"
        )
        operator = _run("--platform", "operator")
        assert operator["platform"] == "operator"
        assert "vi" in operator["languages"]
        # The two sets are now EQUAL (both platforms are complete), so equality
        # can no longer distinguish "derived per platform" from "one shared
        # fixed literal" — which is the bug this test pins. The distinguishing
        # move is to take a source away and watch the default follow. Done
        # against a COPY of the answers dir so the repo's assets are untouched.
        assert operator["languages"] == consumer["languages"], (
            "两个平台都已补齐；若这行失败，说明某个平台的默认集没有跟着它的答案文件走"
        )
        with tempfile.TemporaryDirectory() as trimmed:
            trimmed_dir = Path(trimmed)
            for source in sorted((ROOT / "tools" / "faq_i18n").glob("operator_*.json")):
                shutil.copy(source, trimmed_dir / source.name)
            (trimmed_dir / "operator_answers_km.json").unlink()
            # Run with the DEFAULT language set (no --languages). The tool's
            # "a published language went missing" guard fires, and the language
            # it names IS the evidence: the default discovery followed the
            # answer sources instead of a fixed list.
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL),
                    "--platform",
                    "operator",
                    "--answers-dir",
                    str(trimmed_dir),
                    "--questions-dir",
                    str(trimmed_dir),
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=False,
            )
        assert result.returncode != 0
        assert "km" in result.stderr, (
            "移掉 km 的答案文件后，默认集仍认为 km 存在 —— 说明它不是从答案文件派生的"
        )
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


def test_a_recommendation_only_change_also_requires_a_new_version() -> None:
    """The guard must cover BOTH versioned outputs, not just the catalog.

    Recommendations carry their own `i18n` titles, and the merge only rewrites
    them for the languages it is merging — so a change to a language OUTSIDE
    that set (or to the recommendation order) reaches the file untouched by the
    catalog comparison. Comparing only the catalog let exactly that ship under
    the old revision, which is the "guard covers one consumer" shape this
    workstream keeps meeting.
    """
    recommendations = RECOMMENDATIONS.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")
    try:
        payload = json.loads(recommendations)
        # A language the run does NOT merge: the merge leaves it alone, so the
        # edit survives into the output and the guard must notice.
        entry = payload["platforms"]["operator"][1]  # index 0 already has de
        entry.setdefault("i18n", {}).setdefault("de", {})["title"] = "nur-Empfehlung"
        RECOMMENDATIONS.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        message = _run_expecting_failure("--platform", "operator", "--languages", "vi,mn")
        assert "--version" in message
        assert CATALOG.read_text(encoding="utf-8") == catalog
    finally:
        _restore(catalog, recommendations)


def test_merge_refuses_a_language_with_no_question_source(tmp_path: Path) -> None:
    """A language the wide table has no column for needs `questions_<lang>.json`.

    The consumer table predates vi/mn/th/km, so asking the merge to add one of
    them without a question source must FAIL — merging would otherwise write an
    empty question into the catalog, which reads as a translation that exists.
    """
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "answers_zz.json").write_text(json.dumps({}), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--platform",
            "consumer",
            "--languages",
            "zz",
            "--answers-dir",
            str(answers),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )

    assert result.returncode != 0
    assert "题面没有来源" in result.stderr, result.stderr


def test_merge_reads_questions_from_the_questions_dir(tmp_path: Path) -> None:
    """And when the source IS present, the merge accepts it.

    Guards the other direction: the refusal above must not be so eager that a
    language with a proper question file is rejected too.
    """
    source = ROOT / "src" / "aiops_diagnostics" / "faq_catalog.json"
    catalog = json.loads(source.read_text(encoding="utf-8"))
    entries = catalog["platforms"]["consumer"]
    # Build a fake language whose questions + answers exist for every entry.
    questions = {entry["question_id"]: f"Q-{entry['question_id']}" for entry in entries}
    answers = {entry["question_id"]: f"A-{entry['question_id']}" for entry in entries}

    directory = tmp_path / "i18n"
    directory.mkdir()
    (directory / "questions_zz.json").write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")
    (directory / "answers_zz.json").write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")

    # Point the tool at a COPY of the catalog so the real one is untouched.
    import shutil

    shutil.copy(source, tmp_path / "faq_catalog.json")
    result = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--platform",
            "consumer",
            "--languages",
            "zz",
            "--answers-dir",
            str(directory),
            "--questions-dir",
            str(directory),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    # The run must get PAST question resolution; whether it then complains about
    # the version bump is fine — the point is that it did not refuse the source.
    assert "题面没有来源" not in result.stderr, result.stderr
