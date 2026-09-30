"""Unit tests for the job-lifecycle module (#490).

The module has no production caller yet — that is deliberate, and it is what
lets the four call sites converge on one vocabulary instead of each inventing
its own. So these tests are the only thing standing behind it, and they are
written against the properties the four tables must keep:

* every number is the one the store already used (this module moved *where*
  they are written, not what they are);
* the four profiles differ where the four tables really differ — a shared
  constant would have hidden that;
* one claim-guard polarity;
* the three convergence causes stay distinguishable, which the repository has
  already had to defend once.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta

import pytest

from aiops_diagnostics.async_job_lifecycle import (
    DIAGNOSIS,
    HEALTH_JOB,
    LIVE,
    PROFILES,
    QUESTION,
    RUN,
    claim_guard,
    converged,
    expires_at,
)


def test_the_numbers_are_the_ones_the_store_uses() -> None:
    """The contract of this change: same numbers, one place.

    Pinned against literals on purpose. If a number moves, that is a decision
    about retention or deadlines — several of which are documented in
    `docs/validation.md` — and it should fail here rather than pass unnoticed.
    """
    assert HEALTH_JOB.deadline == timedelta(seconds=30)
    assert DIAGNOSIS.deadline == timedelta(minutes=15)
    assert QUESTION.deadline == timedelta(minutes=15)
    assert (HEALTH_JOB.completed_retention, HEALTH_JOB.interrupted_retention) == (
        timedelta(minutes=15),
        timedelta(minutes=5),
    )
    assert (DIAGNOSIS.completed_retention, DIAGNOSIS.interrupted_retention) == (
        timedelta(minutes=15),
        timedelta(minutes=5),
    )


def test_the_profiles_differ_where_the_tables_differ() -> None:
    """Explicit parameters, not one inherited frozenset.

    The health table has no `cancelled`; a question is not a diagnosis; a run
    speaks a different vocabulary entirely. Collapsing any of these into a
    shared set would be the defect this module exists to prevent.
    """
    assert "cancelled" not in HEALTH_JOB.terminal
    assert "cancelled" in DIAGNOSIS.terminal
    assert "cancelled" in QUESTION.terminal
    assert "diagnosed" in RUN.completed
    assert "diagnosed" not in DIAGNOSIS.terminal
    assert len({id(profile.active) for profile in PROFILES.values()}) == len(PROFILES)


def test_a_question_does_not_borrow_the_diagnosis_name() -> None:
    """The store spells a question's deadline `DIAGNOSIS_DEADLINE`.

    The values match, and that is fine; what this asserts is that the *name* is
    its own, so the two can move apart without one silently following the other.
    """
    assert QUESTION is not DIAGNOSIS
    assert QUESTION.name == "question"


def test_the_claim_guard_has_one_polarity() -> None:
    """`status IN (...)`, sorted — never the inverted spelling.

    The health table writes `NOT IN (completed, failed, expired)` today; a
    second polarity means a second terminal set to keep in step, which is how
    the two sets drifted.
    """
    for profile in PROFILES.values():
        fragment, params = claim_guard(profile)
        assert fragment == "status IN (?, ?)", (profile.name, fragment)
        assert params == ("queued", "running")
        assert "NOT IN" not in fragment


def test_an_expired_job_moves_to_the_immediate_tier() -> None:
    """`expired` is the only "gone" this schema has, and it is the short tier."""
    assert expires_at(HEALTH_JOB, "expired", _at()) == _at()
    assert expires_at(DIAGNOSIS, "failed", _at()) == _at() + timedelta(minutes=5)
    assert expires_at(DIAGNOSIS, "completed", _at()) == _at() + timedelta(minutes=15)


def test_a_live_job_has_no_expiry() -> None:
    assert expires_at(DIAGNOSIS, "running", _at()) is None
    assert expires_at(HEALTH_JOB, "queued", _at()) is None


def test_the_three_causes_stay_distinguishable() -> None:
    """Restart, deadline and user stop are three different verdicts.

    `docs/validation.md:1478-1482` records why this matters: `expired` means the
    job outran its deadline, `cancelled` means the user stopped it, and a
    restart is neither — the process holding the worker died. Merging them
    would tell a user their question timed out when a deploy killed it.
    """
    restart = converged(DIAGNOSIS, status="running", deadline_passed=False, cause="restart")
    assert restart[0] == "failed"
    assert restart[1] == "DIAGNOSIS_INTERRUPTED_BY_RESTART"
    assert restart[2] == "failed"

    deadline = converged(DIAGNOSIS, status="running", deadline_passed=True, cause="deadline")
    assert deadline == ("expired", None, "immediate")

    stop = converged(DIAGNOSIS, status="running", deadline_passed=False, cause="user_stop")
    assert stop[0] == "cancelled"
    assert stop[0] not in {"expired", "failed"}


def test_a_live_job_inside_its_deadline_is_left_alone() -> None:
    """The verdict for "nothing has happened yet" is the status it already has.

    This is the property the startup sweep violated: it converged every live row
    because the store had been constructed.
    """
    # `live`, not a retention tier: a job that has not finished has no retention,
    # and returning one would tell a caller to stamp an expiry on a running row.
    assert converged(DIAGNOSIS, status="running", deadline_passed=False, cause="deadline") == (
        "running",
        None,
        LIVE,
    )


def test_a_restart_never_uses_expired_or_cancelled() -> None:
    """Across all four profiles, for the reason the three causes exist."""
    for profile in PROFILES.values():
        status, code, _ = converged(
            profile, status=sorted(profile.active)[0], deadline_passed=False, cause="restart"
        )
        assert status not in {profile.expired, "cancelled"}, (profile.name, status)
        assert code is not None and code.endswith("_INTERRUPTED_BY_RESTART")


def _at() -> datetime:
    return datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)


def test_an_already_finished_row_keeps_its_status() -> None:
    """Convergence answers "what should this row be", not "restart everything"."""
    for status in sorted(DIAGNOSIS.terminal):
        assert converged(DIAGNOSIS, status=status, deadline_passed=True, cause="restart")[0] == status


def test_the_numbers_are_written_down_only_in_this_module() -> None:
    """One definition, checked across the package.

    A second literal that agrees today is the shape this ticket is about: the
    store's health-job deadline and the question table's borrowing of the
    diagnosis name were both exactly that. The check is on the *values*, not on
    a name, so re-spelling them under a new constant still fails.
    """
    import re
    from pathlib import Path

    package = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
    #: The lifecycle's own values, as they appear in source.
    patterns = [
        re.compile(r"timedelta\(seconds=30\)"),
        re.compile(r"timedelta\(minutes=15\)"),
        re.compile(r"timedelta\(minutes=5\)"),
    ]
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        if path.name == "async_job_lifecycle.py":
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(f"{path.name}: {pattern.pattern}")
    assert offenders == [], "; ".join(offenders)


def test_the_status_sets_are_written_down_only_in_this_module() -> None:
    """The same rule for the status words, read from the SYNTAX.

    Parsed rather than grepped: a set split across lines (`frozenset({` on one
    line, its members on the next) is invisible to a line scan, and that is
    exactly how the one legitimate copy in this package is written. The first
    version of this guard missed it — a guard that reads text to answer a
    question about structure is the same "agrees today" shape this ticket is
    about.

    A set literal is a status set when every element is one of the job words.
    Anything else is some other vocabulary that happens to share a word.
    """
    from pathlib import Path

    package = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
    job_words = {
        "queued",
        "running",
        "expired",
        "inconclusive",
        "cancelled",
        "diagnosed",
        "blocked",
        "interrupted",
    }
    #: Files that legitimately hold a copy. `metrics_store` keeps the metric-row
    #: vocabulary (same words, different meaning). `gateway_client` polls a REMOTE
    #: gateway and cannot import the definition — its copy is named
    #: `_TERMINAL_RUN_STATUSES` there and commented as a boundary.
    owners = {"async_job_lifecycle.py", "metrics_store.py", "gateway_client.py"}
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        if path.name in owners:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for literal in _set_literals(node):
                words = {
                    element.value
                    for element in literal.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                }
                if words and words <= job_words:
                    offenders.append(f"{path.name}:{literal.lineno} {sorted(words)}")
    # The named copies must still say why they exist, or they are just copies.
    client = (package / "gateway_client.py").read_text(encoding="utf-8")
    assert "cross-process copy is unavoidable" in client
    assert offenders == [], "; ".join(offenders)


def _set_literals(node: ast.AST) -> list[ast.Set]:
    """Every `{...}` set literal reachable from ``node``."""
    found: list[ast.Set] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Set):
            found.append(inner)
        elif isinstance(inner, ast.Call):
            found.extend(argument for argument in inner.args if isinstance(argument, ast.Set))
    return found


def test_a_table_without_an_expiry_is_not_given_one() -> None:
    """`runs` has no expiry, and inferring one would widen a terminal guard.

    `TERMINAL_RUN_STATUSES` is what `update_run` uses to decide `completed_at`.
    Adding `expired` to it — which the first version of this profile did, by
    giving every table the same expiry word — would treat a status this table
    never produces as "finished". The profile says `None` instead, and this
    asserts the store's exported set is unchanged.
    """
    from aiops_diagnostics.gateway_store import TERMINAL_RUN_STATUSES

    assert RUN.expired is None
    assert (
        frozenset({"diagnosed", "inconclusive", "blocked", "interrupted", "failed"}) == TERMINAL_RUN_STATUSES
    )
    assert "expired" not in TERMINAL_RUN_STATUSES
    with pytest.raises(ValueError):
        converged(RUN, status="running", deadline_passed=True, cause="deadline")


def test_the_question_restart_code_is_the_one_the_contract_documents() -> None:
    """One spelling of one incident.

    The store already exports `ASSISTANT_QUESTION_RESTART_ERROR_CODE` and the
    frontend contract documents it; a code derived from the profile name
    (`QUESTION_INTERRUPTED_BY_RESTART`) would be a second contract for the same
    event.
    """
    from aiops_diagnostics.gateway_store import ASSISTANT_QUESTION_RESTART_ERROR_CODE

    _, code, _ = converged(QUESTION, status="running", deadline_passed=False, cause="restart")
    assert code == ASSISTANT_QUESTION_RESTART_ERROR_CODE


def test_a_user_stop_is_not_a_retryable_failure() -> None:
    """`failed` and `interrupted` are different sets, and this is why.

    The lifecycle has two failure-ish tiers: `failed` (one status, a recorded
    cause) and `interrupted` (a superset that also holds a user stop). The
    diagnosis and question responses treat `{failed, expired}` as retryable.
    Answering that from the tier would tell a user who stopped their own
    generation to retry it — so the responses name the status, and this pins the
    distinction they depend on.
    """
    assert DIAGNOSIS.failed == "failed"
    assert DIAGNOSIS.failed in DIAGNOSIS.interrupted
    assert "cancelled" in DIAGNOSIS.interrupted
    assert DIAGNOSIS.failed != "cancelled"
    # The health table has no user stop, and still names its one failure.
    assert HEALTH_JOB.failed == "failed"
    # `runs` spells three causes instead of one, so it names none of them here.
    assert RUN.failed is None


def test_one_rendering_covers_every_table() -> None:
    """The statement text differs between tables only by the table name.

    This is what "five copies became one" means concretely: the sweep SQL for
    `standard_diagnoses` and for `assistant_questions` is byte-identical once
    the table name is substituted, and their parameters differ only in the
    status lists their profiles declare.
    """
    from aiops_diagnostics.async_job_lifecycle import expire_statements

    now = "2026-09-30T12:00:00+00:00"
    shapes = []
    for table, profile in (
        ("standard_diagnoses", DIAGNOSIS),
        ("assistant_questions", QUESTION),
    ):
        statements = expire_statements(table, profile, now)
        shapes.append(
            (
                statements.claim[0].replace(table, "<table>"),
                statements.sweep[0].replace(table, "<table>"),
            )
        )
    assert shapes[0] == shapes[1], shapes


def test_the_health_table_sweeps_a_different_status_set() -> None:
    """A difference between tables is now a difference between profiles.

    The health table has no `cancelled`, so its sweep names three statuses where
    the other two name four. That is the whole of the difference, and it is
    readable off the profile rather than off a hand-edited SQL string.
    """
    from aiops_diagnostics.async_job_lifecycle import expire_statements

    health = expire_statements("health_report_jobs", HEALTH_JOB, "NOW")
    diagnosis = expire_statements("standard_diagnoses", DIAGNOSIS, "NOW")
    assert "cancelled" in diagnosis.sweep[1]
    assert "cancelled" not in health.sweep[1]
    assert health.sweep[1][2:] == ("completed", "failed", "NOW")


def test_the_startup_shape_is_the_sweep_without_a_deadline_clause() -> None:
    """One rendering, two shapes, and the difference is a parameter.

    The startup path converges every live row (the process that held them is
    gone); the sweep compares against a deadline. Keeping that as a parameter
    rather than a second copy is what makes the difference visible — and it is
    the thing #492 decides about.
    """
    from aiops_diagnostics.async_job_lifecycle import expire_statements

    now = "NOW"
    startup = expire_statements("standard_diagnoses", DIAGNOSIS, now, require_deadline=False)
    sweep = expire_statements("standard_diagnoses", DIAGNOSIS, now)
    assert "deadline_at" not in startup.claim[0]
    assert "deadline_at" in sweep.claim[0]
    assert startup.claim[1] == sweep.claim[1][:-1]


def test_a_table_the_module_does_not_own_is_refused() -> None:
    """The table name is interpolated, so it is an allowlist, not a parameter.

    A name from anywhere else would be an injection surface; every caller today
    passes a literal and this keeps it that way.
    """
    from aiops_diagnostics.async_job_lifecycle import expire_statements

    with pytest.raises(ValueError):
        expire_statements("runs; DROP TABLE runs", DIAGNOSIS, "NOW")
    with pytest.raises(ValueError):
        expire_statements("runs", RUN, "NOW")  # a real table, but not swept here


def test_a_profile_without_an_expiry_cannot_be_swept() -> None:
    from aiops_diagnostics.async_job_lifecycle import expire_statements

    with pytest.raises(ValueError):
        expire_statements("health_report_jobs", RUN, "NOW")


def test_the_sweep_sql_is_written_down_only_in_this_module() -> None:
    """No hand-written expire statement survives outside the renderer.

    Five copies lived here before: three `_expire_*` methods and two more inside
    the startup path. The defect was never that any one of them was wrong — it
    is that each was covered by its own callers' tests, so changing one left the
    others green. That is invisible to a behaviour test and visible here.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        if path.name == "async_job_lifecycle.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = " ".join(node.value.upper().split())
            # A sweep is an UPDATE whose SET clause writes `expired` as a
            # LITERAL. Narrow on purpose: an UPDATE that only mentions `expired`
            # in its WHERE clause (the per-row claim guard, which binds its
            # status) is not a sweep, and matching it would make this guard cry
            # wolf until someone deleted it.
            if "UPDATE " in text and "SET STATUS = 'EXPIRED'" in text.replace("STATUS ='", "STATUS = '"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], "; ".join(offenders)


def _expire_statements_calls() -> dict[str, str]:
    """Every `expire_statements(table, profile, ...)` in the store, by table.

    Read from the syntax tree so both shapes are covered: the three
    `_expire_*` methods AND the startup loop, which pairs a table with a
    profile inside a tuple. The first version of this check walked the methods
    only and missed the loop — where a swap is equally invisible today, because
    the two profiles agree on every value their tables actually produce.
    """
    import ast
    from pathlib import Path

    store = Path(__file__).parents[1] / "src" / "aiops_diagnostics" / "gateway_store.py"
    tree = ast.parse(store.read_text(encoding="utf-8"), filename=str(store))
    pairs: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", "") != "expire_statements":
            continue
        # Only the literal call sites pair a table with a profile; the startup
        # loop calls with its loop variables, and those literals are read below.
        if not isinstance(node.args[0], ast.Constant):
            continue
        table = node.args[0].value
        pairs[table] = ast.unparse(node.args[1])
    # The startup path builds its pair as a loop over literals: read those too,
    # so a swap there is caught by the same assertion. `table`/`profile` are the
    # loop variables themselves — the literals are the tuple elements.
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Tuple):
            continue
        if "expire_statements" not in ast.unparse(node):
            continue
        for element in node.iter.elts:
            if not isinstance(element, ast.Tuple) or len(element.elts) != 2:
                continue
            table = ast.unparse(element.elts[0]).strip("\"'")
            profile = ast.unparse(element.elts[1])
            if table in {"table", "profile"}:
                continue
            pairs[table] = profile
    return pairs


def test_each_sweep_names_the_profile_of_its_own_table() -> None:
    """The table and the profile are two names for one thing — everywhere.

    Substituting another profile's name is behaviourally invisible on today's
    data — the health table has no `cancelled`, so sweeping it with the
    diagnosis profile changes nothing you can observe. It is still wrong: the
    sweep would then follow a profile that does not describe its table, and the
    first status either profile gains would silently apply to both.

    Every pairing in the module is read, including the startup loop's: that is
    where the first version of this check was blind.
    """
    assert _expire_statements_calls() == {
        "health_report_jobs": "HEALTH_JOB",
        "standard_diagnoses": "DIAGNOSIS",
        "assistant_questions": "QUESTION",
    }, _expire_statements_calls()
