"""The FAQ merge tool's DEFAULTS must be safe to run (#529 review).

Both defects this pins were defaults, not logic: a language default that only
suited one platform, and a version default that predated the data. A tool whose
plain re-run rewrites published metadata backward is worse than one that refuses
to run.

**Every case runs against a private copy of the tree, not the repository.** The
tool rewrites `faq_catalog.json` / `faq_recommendations.json` in place, and 24
other test files read that same catalog with a bare `read_text`. Writing the
real file here and restoring it in `finally` made the suite unsafe to run in
parallel: measured on this repository, 2026-10-18,
`pytest tests/test_merge_faq_i18n_tool.py -n 4` gave 4 failed / 5 passed while
`-n 0` gave 9 passed. The window is tens of milliseconds, so a whole-suite run
also passed three times in a row before failing — a suite-wide green is not
evidence that this file is safe to run concurrently.

The copy is cheap (measured 13 ms per function-scoped tree, plus a one-time
55 ms template build) and it is what makes the file honest: the assertions check
exactly what they checked before, they just no longer borrow the repository as
scratch space.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

#: Everything the tool reads. A tree with only these is a complete, runnable
#: copy: the tool derives its own paths from `Path(__file__).parents[1]`, so it
#: addresses the copy once the copy is where it expects to be.
_TREE = (
    "tools/merge_faq_i18n.py",
    "tools/faq_i18n",
    "src/aiops_diagnostics/faq_catalog.json",
    "src/aiops_diagnostics/faq_recommendations.json",
    "docs/EV_Charging_FAQ_Multilingual_Wide_Table.xlsx",
    "docs/管家端问答国际化宽表.xlsx",
)


def _populate(destination: Path) -> None:
    for relative in _TREE:
        source = ROOT / relative
        target = destination / relative
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    # The tool's "did the content change" guard compares against the COMMITTED
    # revision (`git show HEAD:<path>`), so the copy needs a commit or every case
    # would compare against nothing and the guard would read as "changed".
    # `core.hooksPath` is cleared for the copy: this host's managed hooks reject
    # commits on a default branch and require Conventional Commits, neither of
    # which says anything about a throwaway fixture.
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "core.hooksPath", ""],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-q",
            "-m",
            "fixture baseline",
        ],
    ):
        subprocess.run(command, cwd=destination, check=True, capture_output=True)


class _Tree:
    """A private copy of the tool and its inputs, plus the paths inside it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tool = path / "tools" / "merge_faq_i18n.py"
        self.catalog = path / "src" / "aiops_diagnostics" / "faq_catalog.json"
        self.recommendations = path / "src" / "aiops_diagnostics" / "faq_recommendations.json"
        self.answers = path / "tools" / "faq_i18n"

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.tool), *args],
            capture_output=True,
            text=True,
            cwd=self.path,
            check=check,
        )

    def merged(self, *args: str) -> dict:
        return json.loads(self.run(*args).stdout.strip().splitlines()[-1])

    def refuses(self, *args: str) -> str:
        result = self.run(*args, check=False)
        assert result.returncode != 0, f"预期失败但成功了：{result.stdout[-200:]}"
        return result.stdout + result.stderr


@pytest.fixture(scope="session")
def _template() -> Path:
    with tempfile.TemporaryDirectory() as holder:
        pristine = Path(holder) / "pristine"
        pristine.mkdir()
        _populate(pristine)
        yield pristine


@pytest.fixture
def tree(_template: Path) -> _Tree:
    with tempfile.TemporaryDirectory() as holder:
        copy = Path(holder) / "tree"
        shutil.copytree(_template, copy)
        yield _Tree(copy)


def test_the_default_languages_come_from_the_platforms_own_answer_files(tree: _Tree) -> None:
    """`--platform operator` alone must not look for files that do not exist.

    A single fixed default (`en,de,fr,es,pt`) serves whichever platform was
    written first; for operator it raised FileNotFoundError before reading a
    single entry, because operator's translations landed as vi/mn.
    """
    consumer = tree.merged()
    assert consumer["platform"] == "consumer"
    # The default grows as a platform gains languages, so it is asserted as
    # a SUPERSET of the set it had when this test was written plus the four
    # #565 added — a fixed literal would have to be edited every time, and
    # editing it is exactly the drift this test is meant to catch.
    assert {"en", "de", "fr", "es", "pt"} <= set(consumer["languages"])
    assert {"vi", "mn", "th", "km"} <= set(consumer["languages"]), (
        "consumer 默认集里少了 #565 新增的语言 —— 它们没有宽表列，是靠 questions_<lang>.json 被发现的"
    )
    operator = tree.merged("--platform", "operator")
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
        for source in sorted((tree.path / "tools" / "faq_i18n").glob("operator_*.json")):
            shutil.copy(source, trimmed_dir / source.name)
        (trimmed_dir / "operator_answers_km.json").unlink()
        # Run with the DEFAULT language set (no --languages). The tool's
        # "a published language went missing" guard fires, and the language
        # it names IS the evidence: the default discovery followed the
        # answer sources instead of a fixed list.
        result = tree.run(
            "--platform",
            "operator",
            "--answers-dir",
            str(trimmed_dir),
            "--questions-dir",
            str(trimmed_dir),
            check=False,
        )
    assert result.returncode != 0
    assert "km" in result.stderr, "移掉 km 的答案文件后，默认集仍认为 km 存在 —— 说明它不是从答案文件派生的"


def test_a_plain_re_run_does_not_roll_the_version_backward(tree: _Tree) -> None:
    """The version default is the CURRENT one, not a literal that predates it.

    Before the fix, running the tool with its defaults stamped `2026.09.12`
    over a `2026.10.07` catalog — the content stayed, the published revision
    went backward.
    """
    before = json.loads(tree.catalog.read_text(encoding="utf-8"))["faq_version"]
    after = tree.merged()["version"]
    assert after == before, f"默认重跑把版本从 {before} 改成了 {after}"


def test_an_explicit_version_still_wins(tree: _Tree) -> None:
    assert tree.merged("--version", "2099.01.01")["version"] == "2099.01.01"


def test_changed_copy_without_an_explicit_version_is_refused(tree: _Tree) -> None:
    """New copy must not ship under the OLD revision.

    Preserving the current version on an unchanged rerun is right; preserving it
    when the copy changed is not — consumers comparing `faq_version` see no
    update at all. The tool refuses BEFORE writing, so a refused run leaves the
    tree exactly as it found it.
    """
    answers = tree.answers / "operator_answers_vi.json"
    payload = json.loads(answers.read_text(encoding="utf-8"))
    payload["operator.faq.q001"] = payload["operator.faq.q001"] + " (edited)"
    answers.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    catalog = tree.catalog.read_text(encoding="utf-8")
    message = tree.refuses("--platform", "operator")
    assert "--version" in message
    # And nothing was written on the way out.
    assert tree.catalog.read_text(encoding="utf-8") == catalog


def test_a_missing_answer_file_is_refused_rather_than_silently_dropped(tree: _Tree) -> None:
    """A language that is already published may not vanish by omission.

    `default_languages` discovers what has an answer file TODAY. If one went
    missing, merging the rest and preserving the old entries leaves copy that no
    longer matches its source, and the run says nothing — the worst outcome,
    because it looks like a successful run.
    """
    answers = tree.answers / "operator_answers_vi.json"
    answers.rename(answers.with_suffix(".hidden"))
    catalog = tree.catalog.read_text(encoding="utf-8")
    message = tree.refuses("--platform", "operator")
    assert "vi" in message
    assert tree.catalog.read_text(encoding="utf-8") == catalog


def test_removing_a_language_is_allowed_when_asked_for_explicitly(tree: _Tree) -> None:
    """Refusing by omission must not make a deliberate removal impossible."""
    answers = tree.answers / "operator_answers_vi.json"
    answers.rename(answers.with_suffix(".hidden"))
    assert tree.merged("--platform", "operator", "--languages", "mn")["languages"] == ["mn"]


def test_a_recommendation_only_change_also_requires_a_new_version(tree: _Tree) -> None:
    """The guard must cover BOTH versioned outputs, not just the catalog.

    Recommendations carry their own `i18n` titles, and the merge only rewrites
    them for the languages it is merging — so a change to a language OUTSIDE
    that set (or to the recommendation order) reaches the file untouched by the
    catalog comparison. Comparing only the catalog let exactly that ship under
    the old revision, which is the "guard covers one consumer" shape this
    workstream keeps meeting.
    """
    payload = json.loads(tree.recommendations.read_text(encoding="utf-8"))
    # A language the run does NOT merge: the merge leaves it alone, so the
    # edit survives into the output and the guard must notice.
    entry = payload["platforms"]["operator"][1]  # index 0 already has de
    entry.setdefault("i18n", {}).setdefault("de", {})["title"] = "nur-Empfehlung"
    tree.recommendations.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    catalog = tree.catalog.read_text(encoding="utf-8")
    message = tree.refuses("--platform", "operator", "--languages", "vi,mn")
    assert "--version" in message
    assert tree.catalog.read_text(encoding="utf-8") == catalog


def test_merge_refuses_a_language_with_no_question_source(tree: _Tree, tmp_path: Path) -> None:
    """A language the wide table has no column for needs `questions_<lang>.json`.

    The consumer table predates vi/mn/th/km, so asking the merge to add one of
    them without a question source must FAIL — merging would otherwise write an
    empty question into the catalog, which reads as a translation that exists.
    """
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "answers_zz.json").write_text(json.dumps({}), encoding="utf-8")

    result = tree.run(
        "--platform",
        "consumer",
        "--languages",
        "zz",
        "--answers-dir",
        str(answers),
        check=False,
    )

    assert result.returncode != 0
    assert "题面没有来源" in result.stderr, result.stderr


def test_merge_reads_questions_from_the_questions_dir(tree: _Tree, tmp_path: Path) -> None:
    """And when the source IS present, the merge accepts it.

    Guards the other direction: the refusal above must not be so eager that a
    language with a proper question file is rejected too.
    """
    catalog = json.loads(tree.catalog.read_text(encoding="utf-8"))
    entries = catalog["platforms"]["consumer"]
    # Build a fake language whose questions + answers exist for every entry.
    questions = {entry["question_id"]: f"Q-{entry['question_id']}" for entry in entries}
    answers = {entry["question_id"]: f"A-{entry['question_id']}" for entry in entries}

    directory = tmp_path / "i18n"
    directory.mkdir()
    (directory / "questions_zz.json").write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")
    (directory / "answers_zz.json").write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")

    result = tree.run(
        "--platform",
        "consumer",
        "--languages",
        "zz",
        "--answers-dir",
        str(directory),
        "--questions-dir",
        str(directory),
        check=False,
    )
    # The run must get PAST question resolution; whether it then complains about
    # the version bump is fine — the point is that it did not refuse the source.
    assert "题面没有来源" not in result.stderr, result.stderr
