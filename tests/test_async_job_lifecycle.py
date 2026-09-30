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
    """The same rule for the status words, scoped to the job tables.

    `{"queued", "running"}` was spelled out at four sites; `{"failed",
    "cancelled"}` at two. All of them now name a profile's field.

    Scoped to the words that MEAN a job state (queued/running/expired and the
    per-table terminal values) and excluding `metrics_store`, which keeps its
    own `OUTCOME_TYPES` vocabulary — `completed`/`failed` there describe a
    metric row's outcome, not a job's status. Two vocabularies that happen to
    share words are not one definition, and merging them would be the opposite
    mistake.
    """
    import re
    from pathlib import Path

    package = Path(__file__).parents[1] / "src" / "aiops_diagnostics"
    job_words = ("queued", "running", "expired", "inconclusive", "cancelled")
    # Matched anywhere inside the literal, not only as its first element: a copy
    # that happens to list `completed` first would otherwise slip past — the same
    # "agrees today" shape this ticket is about.
    #
    # Scoped to the shapes that ARE a status set: a `frozenset({...})`, or a set
    # on the right of `status ... in`. A bare dict that happens to carry a
    # `"running"` value (a phase, a log field) is not a status set, and matching
    # it would make this guard cry wolf until someone deleted it.
    words = "|".join(job_words)
    literal_set = re.compile(
        r"frozenset\(\{[^}\n]*[" + "'\"" + r"](?:" + words + r")[" + "'\"" + r"][^}\n]*\}\)"
        r"|in\s*\{[^}\n]*[" + "'\"" + r"](?:" + words + r")[" + "'\"" + r"][^}\n]*\}"
    )
    #: Files that legitimately hold a copy. `metrics_store` keeps the metric-row
    #: vocabulary (same words, different meaning). `gateway_client` polls a REMOTE
    #: gateway and cannot import the definition — its copy is named
    #: `_TERMINAL_RUN_STATUSES` there and commented as a boundary.
    owners = {"async_job_lifecycle.py", "metrics_store.py", "gateway_client.py"}
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        if path.name in owners:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if literal_set.search(line):
                offenders.append(f"{path.name}:{number}")
    # The named copies must still say why they exist, or they are just copies.
    client = (package / "gateway_client.py").read_text(encoding="utf-8")
    assert "cross-process copy is unavoidable" in client
    assert offenders == [], "; ".join(offenders)


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
    diagnosis response treats `{"failed", "expired"}` as retryable. Answering
    that from the tier would tell a user who stopped their own diagnosis to
    retry it — so the response names the status, and this pins that.

    Asserted through the store's exported set rather than the response, because
    the property is about the vocabulary: a table with a user stop must keep
    that stop out of its `failed` field.
    """
    assert DIAGNOSIS.failed == "failed"
    assert DIAGNOSIS.failed in DIAGNOSIS.interrupted
    assert "cancelled" in DIAGNOSIS.interrupted
    assert DIAGNOSIS.failed != "cancelled"
    # And the health table, which has no user stop, still names its one failure.
    assert HEALTH_JOB.failed == "failed"
