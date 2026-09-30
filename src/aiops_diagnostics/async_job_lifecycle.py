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
RetentionTier = Literal["completed", "failed", "immediate"]

#: Why a job is being converged instead of finishing. The three are distinct on
#: purpose and the repository has already had to defend the distinction
#: (`docs/validation.md:1478-1482`): `expired` means it outran its deadline,
#: `cancelled` means the user stopped it, and a restart is neither.
Cause = Literal["deadline", "restart", "user_stop"]


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
    #: Finished because it outran its deadline. Kept on the shortest tier.
    expired: str
    deadline: timedelta
    completed_retention: timedelta
    interrupted_retention: timedelta
    #: Whether a finished row is removed the moment it expires (the sweep marks
    #: it `expired`, which is the only "gone" this schema has).
    expired_retention_is_immediate: bool = True

    @property
    def terminal(self) -> frozenset[str]:
        return self.completed | self.interrupted | frozenset({self.expired})

    def retention_for(self, status: str) -> timedelta | None:
        """How long a row in this status is kept, or `None` for a live row."""
        if status in self.completed:
            return self.completed_retention
        if status in self.interrupted:
            return self.interrupted_retention
        if status == self.expired:
            return timedelta(0) if self.expired_retention_is_immediate else self.completed_retention
        return None


#: The four profiles. Every number is the one already in `gateway_store`; the
#: question table's deadline and retention really are the diagnosis ones, and
#: that is stated here rather than spelled by borrowing a diagnostic name.
HEALTH_JOB = JobProfile(
    name="health_job",
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
    active=frozenset({"queued", "running"}),
    completed=frozenset({"completed", "inconclusive"}),
    interrupted=frozenset({"failed", "cancelled"}),
    expired="expired",
    deadline=timedelta(minutes=15),
    completed_retention=timedelta(minutes=15),
    interrupted_retention=timedelta(minutes=5),
)

#: The device path's runs. A different vocabulary (`diagnosed`, `blocked`,
#: `interrupted`) and no deadline column at all: this table has never had a
#: convergence rule, which is a decision PRD #410 left open rather than an
#: oversight to be fixed here.
RUN = JobProfile(
    name="run",
    active=frozenset({"queued", "running"}),
    completed=frozenset({"diagnosed", "inconclusive"}),
    interrupted=frozenset({"blocked", "interrupted", "failed"}),
    expired="expired",
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
        code = f"{profile.name.upper()}_INTERRUPTED_BY_RESTART"
        # A restart is neither a timeout nor a user stop; it lands on the
        # interrupted tier with a code that says which of the three it was.
        interrupted = sorted(profile.interrupted)
        target = "failed" if "failed" in interrupted else interrupted[0]
        return target, code, "failed"
    if cause == "user_stop":
        cancelled = "cancelled" if "cancelled" in profile.interrupted else "failed"
        return cancelled, None, "failed"
    if deadline_passed:
        return profile.expired, None, "immediate"
    return status, None, "completed"


def _tier_for(profile: JobProfile, status: str) -> RetentionTier:
    if status == profile.expired:
        return "immediate"
    return "failed" if status in profile.interrupted else "completed"
