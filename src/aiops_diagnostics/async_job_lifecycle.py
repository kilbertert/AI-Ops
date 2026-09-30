"""One definition of an asynchronous job's lifecycle (#430).

Four tables implement this rule today (`health_report_jobs`,
`standard_diagnoses`, `assistant_questions`, `runs`) and give three and a half
answers: five hand-written copies of the same expire statement, two polarity
spellings of the same claim guard, two constant families with the question table
borrowing the diagnosis one, and a startup sweep that the repository itself
already documented as wrong (constructing a store ended in-flight work).

This module is the single definition. Nothing calls it yet — it is landed first
so the four call sites can converge on one vocabulary instead of each inventing
its own, which is the shape this repository has repeatedly had to collapse.

Three rules the shape is built to keep:

* **Named profiles, not shared constants.** The four tables genuinely differ
  (`health_report_jobs` has no `cancelled`, `runs` speaks a different vocabulary
  entirely). Those differences become explicit parameters rather than four
  frozensets that never mention each other.
* **One polarity.** The claim guard is `status IN (...)`; the inverted spelling
  that `health_report_jobs` uses today is not offered.
* **The numbers do not move.** Every value here is the one the store already
  uses; this module changes where they are written down, not what they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

#: Which tier's retention a finished row gets. `expired` and `failed` share a
#: tier but not a meaning: one outran its deadline, the other stopped for a
#: cause that is recorded on the row. The tier is about how long the row is
#: worth keeping, which is the only thing the sweep needs from it.
#:
#: `LIVE` is the fourth answer and it is not a tier: a job that has not finished
#: has no retention, and giving it one would tell a caller to stamp an expiry on
#: something that is still running.
RetentionTier = Literal["completed", "failed", "immediate", "live"]

#: Why a job is being converged instead of finishing. The three are distinct on
#: purpose and the repository has already had to defend the distinction
#: (`docs/validation.md:1478-1482`): `expired` means it outran its deadline,
#: `cancelled` means the user stopped it, and a restart is neither.
Cause = Literal["deadline", "restart", "user_stop"]

#: The answer for "this row has not finished, so it has no tier".
LIVE: RetentionTier = "live"


@dataclass(frozen=True, slots=True)
class JobProfile:
    """One table's lifecycle vocabulary.

    A profile is data, not behaviour: the differences between the four tables
    are exactly the fields here, and writing them out is what stops a rule from
    being silently inherited by a table it does not fit.
    """

    name: str
    active: frozenset[str]
    #: Finished-and-kept. A row in this set has an answer the client may read.
    completed: frozenset[str]
    #: Finished because it failed or the user stopped it.
    interrupted: frozenset[str]
    #: Finished because it outran its deadline, or `None` for a table that has
    #: no expiry at all. `runs` is that table: no deadline column, no sweep, no
    #: convergence rule. Making this optional is what keeps "we did not give it
    #: one" different from "we gave it one and forgot to use it".
    expired: str | None
    deadline: timedelta
    completed_retention: timedelta
    interrupted_retention: timedelta
    #: The single status meaning "it stopped for a recorded cause", when the
    #: table has exactly one. `None` where the vocabulary has no such value
    #: (`runs` spells three different causes). Callers that mean *this* status
    #: — a retryable failure shown to the caller, say — name it rather than
    #: reaching for `interrupted`, which is a tier and also holds a user stop.
    failed: str | None = None
    #: The code a restart records on the row. Defaulted rather than spelled per
    #: table: the store's existing constant is the one the API contract already
    #: documents, and a second spelling would be a second contract.
    restart_error_code: str | None = None
    #: Whether a finished row is removed the moment it expires (the sweep marks
    #: it `expired`, which is the only "gone" this schema has).
    expired_retention_is_immediate: bool = True

    @property
    def terminal(self) -> frozenset[str]:
        """Every status this table will never change again.

        `expired` joins it only when the table has one: a run has no expiry, so
        its terminal set is exactly what the store exported before this module
        existed — adding a status the table never produces would widen a guard
        that answers "has this finished", which is not a change this ticket is
        allowed to make.
        """
        if self.expired is None:
            return self.completed | self.interrupted
        return self.completed | self.interrupted | frozenset({self.expired})

    def retention_for(self, status: str) -> timedelta | None:
        """How long a row in this status is kept, or `None` for a live row."""
        if status in self.completed:
            return self.completed_retention
        if status in self.interrupted:
            return self.interrupted_retention
        if self.expired is not None and status == self.expired:
            return timedelta(0) if self.expired_retention_is_immediate else self.completed_retention
        return None


#: The four profiles. Every number is the one already in `gateway_store`; the
#: question table's deadline and retention really are the diagnosis ones, and
#: that is stated here rather than spelled by borrowing a diagnostic name.
HEALTH_JOB = JobProfile(
    name="health_job",
    failed="failed",
    active=frozenset({"queued", "running"}),
    completed=frozenset({"completed"}),
    interrupted=frozenset({"failed"}),
    expired="expired",
    deadline=timedelta(seconds=30),
    completed_retention=timedelta(minutes=15),
    interrupted_retention=timedelta(minutes=5),
)

DIAGNOSIS = JobProfile(
    name="diagnosis",
    failed="failed",
    active=frozenset({"queued", "running"}),
    completed=frozenset({"completed", "inconclusive"}),
    interrupted=frozenset({"failed", "cancelled"}),
    expired="expired",
    deadline=timedelta(minutes=15),
    completed_retention=timedelta(minutes=15),
    interrupted_retention=timedelta(minutes=5),
)

#: A question is a general Q&A job. Its numbers match the diagnosis profile;
#: naming it separately is the point — the two can move independently, and the
#: store's `DIAGNOSIS_DEADLINE` on this table said otherwise.
QUESTION = JobProfile(
    name="question",
    failed="failed",
    restart_error_code="QA_INTERRUPTED_BY_RESTART",
    active=frozenset({"queued", "running"}),
    completed=frozenset({"completed", "inconclusive"}),
    interrupted=frozenset({"failed", "cancelled"}),
    expired="expired",
    deadline=timedelta(minutes=15),
    completed_retention=timedelta(minutes=15),
    interrupted_retention=timedelta(minutes=5),
)

#: The device path's runs. A different vocabulary (`diagnosed`, `blocked`,
#: `interrupted`) and **no expiry at all**: this table has never had a
#: convergence rule, which is a decision PRD #410 left open rather than an
#: oversight to be fixed here. `expired=None` says that, and keeps this table's
#: terminal set byte-identical to the one the store exported before.
RUN = JobProfile(
    name="run",
    active=frozenset({"queued", "running"}),
    completed=frozenset({"diagnosed", "inconclusive"}),
    interrupted=frozenset({"blocked", "interrupted", "failed"}),
    expired=None,
    deadline=timedelta(minutes=15),
    completed_retention=timedelta(minutes=15),
    interrupted_retention=timedelta(minutes=5),
    expired_retention_is_immediate=True,
)

PROFILES: dict[str, JobProfile] = {
    profile.name: profile for profile in (HEALTH_JOB, DIAGNOSIS, QUESTION, RUN)
}


def claim_guard(profile: JobProfile) -> tuple[str, tuple[str, ...]]:
    """The `WHERE` fragment a worker uses to claim a row, and its parameters.

    One polarity, one place. The store spells the same guard three ways today
    (`IN`, and an inverted `NOT IN` on the health table); a caller that needs
    "not already finished" gets it by claiming, not by writing the inverse.
    """
    ordered = tuple(sorted(profile.active))
    placeholders = ", ".join("?" for _ in ordered)
    return f"status IN ({placeholders})", ordered


#: The tables this module will render SQL for. An allowlist rather than a
#: free-form name: the renderer interpolates the table into the statement, and a
#: name that came from anywhere else would be an injection surface. Every caller
#: today passes a literal, and this keeps it that way.
_TABLES = frozenset({"health_report_jobs", "standard_diagnoses", "assistant_questions"})


@dataclass(frozen=True, slots=True)
class ExpireStatements:
    """The two statements a sweep runs, with their parameters bound positionally.

    `claim` moves a live job that outran its deadline to `expired`; `sweep` does
    the same to a finished job whose retention ran out. They are one object
    because every caller needs both, in that order, against the same table.

    ``require_deadline`` is the one difference the startup path has: it converges
    every live row because the process that held them is gone, so it has no
    deadline to compare against. It is a parameter rather than a second rendering
    so the two shapes stay visibly related — and #492 decides whether that path
    should keep doing this at all.
    """

    claim: tuple[str, tuple[object, ...]]
    sweep: tuple[str, tuple[object, ...]]


def expire_statements(
    table: str, profile: JobProfile, now: datetime, *, require_deadline: bool = True
) -> ExpireStatements:
    """Render the sweep for one table from its profile.

    Five hand-written copies of this SQL became three (`_expire_*`) plus two
    more inside the startup path, each differing only in the table name and the
    set of statuses it swept — which is to say, each differing only in what its
    profile already says.

    The parameters are returned rather than interpolated: a status set has a
    fixed size per profile, and binding it keeps the statement text identical
    across tables so a diff of two renders is a diff of the profiles.
    """
    if table not in _TABLES:
        raise ValueError(f"not a job table: {table}")
    if profile.expired is None:
        raise ValueError(f"{profile.name} has no expiry: it cannot be swept")
    ordered_active = tuple(sorted(profile.active))
    swept = tuple(sorted(profile.completed | profile.interrupted))
    active_placeholders = ", ".join("?" for _ in ordered_active)
    swept_placeholders = ", ".join("?" for _ in swept)
    deadline_clause = " AND deadline_at <= ?" if require_deadline else ""
    claim_sql = (
        f"UPDATE {table} SET status = ?, completed_at = COALESCE(completed_at, ?),"
        f" expires_at = COALESCE(expires_at, ?), updated_at = ?"
        f" WHERE status IN ({active_placeholders}){deadline_clause}"
    )
    sweep_sql = (
        f"UPDATE {table} SET status = ?, updated_at = ?"
        f" WHERE status IN ({swept_placeholders}) AND expires_at <= ?"
    )
    claim_params: tuple[object, ...] = (profile.expired, now, now, now, *ordered_active)
    if require_deadline:
        claim_params = (*claim_params, now)
    return ExpireStatements(
        claim=(claim_sql, claim_params),
        sweep=(sweep_sql, (profile.expired, now, *swept, now)),
    )


def expires_at(profile: JobProfile, status: str, finished_at: datetime) -> datetime | None:
    """When this row becomes sweepable, or `None` while it is still live."""
    retention = profile.retention_for(status)
    return None if retention is None else finished_at + retention


def converged(
    profile: JobProfile,
    *,
    status: str,
    deadline_passed: bool,
    cause: Cause,
) -> tuple[str, str | None, RetentionTier]:
    """What a job that did not finish should become.

    Returns `(status, error_code, retention_tier)`. A job still inside its
    deadline and not being restarted is left alone (`status` comes back
    unchanged, with no code) — the function answers "what should this row be
    now", and for a live row the answer is "what it is".

    The restart case is the one the repository already got wrong once:
    constructing a store used to converge every live row, so a short-lived CLI
    command (`aiops-gateway devices`) ended in-flight diagnoses. A restart is a
    real cause and gets its own verdict, but it is only ever passed in by the
    startup path — never inferred from the mere existence of a new process.
    """
    if status not in profile.active:
        return status, None, _tier_for(profile, status)
    if cause == "restart":
        # The store already names this code for the question table
        # (`ASSISTANT_QUESTION_RESTART_ERROR_CODE`). The profile carries the
        # name so the two cannot drift into two spellings of one incident.
        code = profile.restart_error_code or f"{profile.name.upper()}_INTERRUPTED_BY_RESTART"
        # A restart is neither a timeout nor a user stop; it lands on the
        # interrupted tier with a code that says which of the three it was.
        interrupted = sorted(profile.interrupted)
        target = "failed" if "failed" in interrupted else interrupted[0]
        return target, code, "failed"
    if cause == "user_stop":
        cancelled = "cancelled" if "cancelled" in profile.interrupted else "failed"
        return cancelled, None, "failed"
    if deadline_passed:
        if profile.expired is None:
            raise ValueError(f"{profile.name} has no expiry: it cannot be converged by deadline")
        return profile.expired, None, "immediate"
    # A live row gets no tier: it has not finished, so "how long do we keep it"
    # has no answer yet. Returning the completed tier would tell a future caller
    # to stamp a retention deadline on a job that is still running.
    return status, None, _tier_for(profile, status) if status in profile.terminal else LIVE


def _tier_for(profile: JobProfile, status: str) -> RetentionTier:
    if status == profile.expired:
        return "immediate"
    return "failed" if status in profile.interrupted else "completed"
