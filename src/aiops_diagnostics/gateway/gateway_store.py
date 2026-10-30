from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.async_job_lifecycle import (
    DIAGNOSIS,
    HEALTH_JOB,
    QUESTION,
    expire_statements,
)
from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.common.redaction import redact_text
from aiops_diagnostics.identity.query_scope import MAX_SCOPE_IDS

_LOGGER = logging.getLogger("aiops.gateway_store")

SAFE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# The numbers and status words live in ONE place (#490): `async_job_lifecycle`
# owns the lifecycle and these names are its values under the names this module
# has always exported. Deriving rather than restating is the point — a second
# literal that agrees today is the shape that drifts, and this table's constants
# and the question table's were already borrowing one another's names.
ACTIVE_HEALTH_JOB_STATUSES = HEALTH_JOB.active
TERMINAL_HEALTH_JOB_STATUSES = HEALTH_JOB.terminal
HEALTH_JOB_COMPLETED_RETENTION = HEALTH_JOB.completed_retention
HEALTH_JOB_FAILED_RETENTION = HEALTH_JOB.interrupted_retention
HEALTH_JOB_DEADLINE = HEALTH_JOB.deadline
ACTIVE_DIAGNOSIS_STATUSES = DIAGNOSIS.active
# `cancelled` is the user-stop terminal status (PRD #346). assistant_questions
# reuses this set, so the value shows up in the diagnosis enum as well — one
# shared status set instead of two. Diagnoses have no cancel entry point of
# their own, so nothing here produces it yet.
TERMINAL_DIAGNOSIS_STATUSES = DIAGNOSIS.terminal
DIAGNOSIS_COMPLETED_RETENTION = DIAGNOSIS.completed_retention
DIAGNOSIS_FAILED_RETENTION = DIAGNOSIS.interrupted_retention
# A cancelled job is kept on the short (failed) tier: the client already holds
# the answer surface, and the row only has to outlive the refresh window. Not
# "never expires" (unbounded rows) and not "expires now" — the user is still
# looking at 「已停止」 when they refresh.
DIAGNOSIS_FAILED_RETENTION_STATUSES = DIAGNOSIS.interrupted
# The diagnosis deadline must cover real agent runs, which the API contract
# documents as tens of seconds to minutes (observed: ~7 minutes on the real
# 120-world link). A short deadline marks still-running diagnoses as expired
# before the worker can record its result.
DIAGNOSIS_DEADLINE = DIAGNOSIS.deadline
# A question still `queued`/`running` when the gateway restarts is converged to
# `failed`, not to `expired` (that value means the job outran its deadline) and
# not to `cancelled` (that value means the user asked it to stop). Neither is
# what happened: the process holding the worker died. The code and message on
# the row carry the cause, so an operator reading a failed answer can tell a
# deploy from a timeout and from a user stop.
HEALTH_JOB_RESTART_ERROR_CODE = "REPORT_INTERRUPTED_BY_RESTART"
HEALTH_JOB_RESTART_ERROR_MESSAGE = "the gateway restarted while this report was still being computed"
DIAGNOSIS_RESTART_ERROR_CODE = "DIAGNOSIS_INTERRUPTED_BY_RESTART"
DIAGNOSIS_RESTART_ERROR_MESSAGE = "the gateway restarted while this diagnosis was still running"
ASSISTANT_QUESTION_RESTART_ERROR_CODE = "QA_INTERRUPTED_BY_RESTART"
ASSISTANT_QUESTION_RESTART_ERROR_MESSAGE = (
    "the gateway restarted while this question was still being answered"
)


#: The three asynchronous job tables, in the order their deadlines run. Named
#: here rather than imported from `async_job_lifecycle` so the sweep helper and
#: the counting helper cannot drift over which tables are "jobs".
JOB_TABLES = ("health_report_jobs", "standard_diagnoses", "assistant_questions")


class GatewayStore:
    """Single-node durable store with an interface that can later move to PostgreSQL."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def create_or_reuse_health_job(
        self,
        scope_fingerprint: str,
        order_no: str,
        rule_version: str,
        *,
        language: str = "zh",
        tenant_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        scope_fingerprint = _scope(scope_fingerprint, "scope_fingerprint")
        order_no = _scope(order_no, "order_no")
        rule_version = _scope(rule_version, "rule_version")
        now = _utc_now()
        with self._connection(write=True) as connection:
            self._expire_health_jobs(connection, now)
            row = connection.execute(
                """
                SELECT * FROM health_report_jobs
                WHERE scope_fingerprint = ? AND order_no = ? AND rule_version = ?
                  AND language = ?
                  AND status IN ('queued', 'running', 'completed')
                ORDER BY created_at DESC LIMIT 1
                """,
                (scope_fingerprint, order_no, rule_version, _language(language)),
            ).fetchone()
            if row is not None:
                return _health_job_from_row(row), False

            job_id = "hrj_" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO health_report_jobs (
                    job_id, scope_fingerprint, order_no, rule_version, status, language,
                    created_at, updated_at, deadline_at, tenant_id
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    scope_fingerprint,
                    order_no,
                    rule_version,
                    _language(language),
                    _iso(now),
                    _iso(now),
                    _iso(now + HEALTH_JOB_DEADLINE),
                    tenant_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM health_report_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _health_job_from_row(row), True

    def update_health_job(
        self,
        job_id: str,
        *,
        status: str,
        report: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        if status not in ACTIVE_HEALTH_JOB_STATUSES | TERMINAL_HEALTH_JOB_STATUSES:
            raise ValueError("invalid health-report job status")
        now = _utc_now()
        completed_at = now if status in TERMINAL_HEALTH_JOB_STATUSES else None
        expires_at = (
            now + HEALTH_JOB_COMPLETED_RETENTION
            if status == "completed"
            else now + HEALTH_JOB_FAILED_RETENTION
            if status == "failed"
            else now
            if status == "expired"
            else None
        )
        with self._connection(write=True) as connection:
            self._expire_health_jobs(connection, now)
            updated = connection.execute(
                """
                UPDATE health_report_jobs SET
                    status = ?, report_json = COALESCE(?, report_json),
                    error_code = COALESCE(?, error_code),
                    error_message = COALESCE(?, error_message),
                    started_at = COALESCE(started_at, ?),
                    completed_at = COALESCE(completed_at, ?),
                    expires_at = COALESCE(?, expires_at), updated_at = ?
                WHERE job_id = ? AND status NOT IN ('completed', 'failed', 'expired')
                """,
                (
                    status,
                    json.dumps(report, ensure_ascii=False) if report is not None else None,
                    error_code,
                    redact_text(error_message or "")[:1000] if error_message else None,
                    _iso(now) if status == "running" else None,
                    _iso(completed_at) if completed_at else None,
                    _iso(expires_at) if expires_at else None,
                    _iso(now),
                    job_id,
                ),
            )
        return updated.rowcount == 1

    def get_health_job(self, job_id: str, scope_fingerprint: str) -> dict[str, Any] | None:
        with self._connection(write=True) as connection:
            self._expire_health_jobs(connection, _utc_now())
            row = connection.execute(
                "SELECT * FROM health_report_jobs WHERE job_id = ? AND scope_fingerprint = ?",
                (job_id, scope_fingerprint),
            ).fetchone()
        return _health_job_from_row(row) if row is not None else None

    def count_health_jobs(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM health_report_jobs").fetchone()
        return int(row["count"])

    @staticmethod
    def _expire_health_jobs(connection: sqlite3.Connection, now: datetime) -> None:
        # Rendered from the profile (#491). Every table runs the same two
        # statements; what differs between them is what their profiles declare,
        # so a difference between two tables is now a difference between two
        # profiles rather than a difference between two hand-edits.
        statements = expire_statements("health_report_jobs", HEALTH_JOB, _iso(now))
        connection.execute(*statements.claim)
        connection.execute(*statements.sweep)

    def create_analysis_job(
        self,
        scope_fingerprint: str,
        *,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        rule_version: str,
        language: str = "zh",
        tenant_id: str | None = None,
        card: str = "failure_rate",
    ) -> dict[str, Any]:
        """Create an operations-analysis job (#697/#698) on the diagnosis job table.

        It shares ``standard_diagnoses`` deliberately: the status vocabulary, the
        claim guard, the expiry sweep and restart convergence are already right
        for this job, and a second table would be a second copy of them. What
        differs is one discriminator column (``kind``) plus the scope fields.

        ``order_no`` is bound to the empty string rather than the column being
        made nullable. Relaxing it needs a table rebuild, which this store has
        never done and which would put every existing diagnosis row at risk for
        no benefit; ``NOT NULL`` is satisfied by ``''``, and the response builder
        omits the field for ``kind='analysis'``, so it never reaches the wire.
        (``question`` likewise: it is ``NOT NULL`` and meaningless here.)

        The window must be a real half-open interval. ``site_ids=None`` skips the
        narrowing entirely rather than widening it: the worker intersects the
        caller's live scope with it, so a request that names no sites reads
        exactly the sites that caller may see — never the whole table.
        """
        scope_fingerprint = _scope(scope_fingerprint, "scope_fingerprint")
        # ``None`` means "the caller's whole resolved site set" — the same
        # convention ``QueryScope.site_ids`` uses — and the caller's live scope
        # is only known in the worker, so the row stores NULL and the worker
        # narrows it. An *explicit* empty tuple is a different thing: a request
        # for nothing, refused rather than silently widened into a full scan.
        if site_ids is not None and not site_ids:
            raise ValueError("analysis requires at least one site")
        if site_ids is not None and len(site_ids) > MAX_SCOPE_IDS:
            raise ValueError(f"analysis job site count exceeds {MAX_SCOPE_IDS}")
        clean_sites = (
            tuple(_scope(site_id, "site_id") for site_id in site_ids) if site_ids is not None else None
        )
        start = _window_bound(window_start, "window_start")
        end = _window_bound(window_end, "window_end")
        if end <= start:
            raise ValueError("analysis window end must be after its start")
        rule_version = _scope(rule_version, "rule_version")
        card = _scope(card, "card")
        language = _language(language)
        now = _utc_now()
        analysis_id = ANALYSIS_ID_PREFIX + uuid.uuid4().hex
        with self._connection(write=True) as connection:
            self._expire_diagnoses(connection, now)
            connection.execute(
                """
                INSERT INTO standard_diagnoses (
                    diagnosis_id, scope_fingerprint, order_no, question, indicator_code, language,
                    internal_run_id, status, created_at, updated_at, deadline_at,
                    kind, site_ids_json, window_start, window_end, rule_version, card, tenant_id
                ) VALUES (?, ?, '', '', NULL, ?, NULL, 'queued', ?, ?, ?, 'analysis', ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_id,
                    scope_fingerprint,
                    language,
                    _iso(now),
                    _iso(now),
                    _iso(now + DIAGNOSIS_DEADLINE),
                    json.dumps(list(clean_sites), ensure_ascii=False) if clean_sites is not None else None,
                    _iso(start),
                    _iso(end),
                    rule_version,
                    card,
                    tenant_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM standard_diagnoses WHERE diagnosis_id = ?",
                (analysis_id,),
            ).fetchone()
        return _standard_diagnosis_from_row(row)

    def record_publish_audit(
        self,
        *,
        app_id: str,
        action: str,
        tenant_id: str = "",
        business_entry: str = "",
        agent_name: str = "",
        company_user_id: str = "",
        company_username: str = "",
        dify_account_id: str = "",
        version_no: int | None = None,
        reason_code: str = "",
    ) -> None:
        """Append one publish attempt to the ledger (#772 D / #780).

        Append-only in the same sense the trial audit is appended: nothing here
        is updated or deleted, so "what happened, in order" survives whatever
        the registry later says the binding is.

        **Never raises into the publish path.** The ledger is observability, not
        an authorization input: a disk error must not turn a legitimate publish
        into a failure (it warns instead, and the surrounding code may still
        refuse for its own reasons). ``company_user_id`` empty means the
        initiator was unknown — that is recorded, not refused.
        """
        try:
            with self._connection(write=True) as connection:
                connection.execute(
                    """
                    INSERT INTO publish_audit_ledger (
                        at, app_id, tenant_id, business_entry, agent_name,
                        company_user_id, company_username, dify_account_id,
                        version_no, action, reason_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _iso(_utc_now()),
                        app_id,
                        tenant_id,
                        business_entry,
                        agent_name,
                        company_user_id,
                        company_username,
                        dify_account_id,
                        version_no,
                        action,
                        reason_code,
                    ),
                )
        except sqlite3.Error:  # pragma: no cover - 磁盘问题不该挡发布
            _LOGGER.warning("publish audit write failed for app=%s action=%s", app_id, action)

    # ── 一次性跳转票（#985） ──────────────────────────────────────────────

    def create_hop_ticket(
        self,
        *,
        tenant_id: str,
        environment: str,
        workspace_id: str = "",
        ttl_seconds: float = 60.0,
    ) -> str:
        """发一张一次性票，返回它的值。

        票本身是不透明随机串（``token_urlsafe``），**不含租户、不含会话** —— 它在库里
        才与租户关联。这样即使票出现在日志或 Referer 里，读它的人也换不出别人的东西：
        兑换那一步仍然要求一次有效的 RAGFlow 登录，而登录用的是租户自己的凭据。
        """
        ticket = secrets.token_urlsafe(32)
        now = _utc_now()
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO knowledge_hop_tickets"
                " (ticket, tenant_id, environment, workspace_id, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ticket,
                    tenant_id,
                    environment,
                    workspace_id,
                    _iso(now),
                    _iso(now + timedelta(seconds=ttl_seconds)),
                ),
            )
        return ticket

    def consume_hop_ticket(self, ticket: str, *, environment: str) -> dict[str, Any] | None:
        """兑换一张票：**成功或失败都算消耗**。

        * 不存在 / 已过期 / 已用过 ⇒ ``None``。三种都不区分 —— 区分会给枚举票的
          人一个"这张存在但过期了"的信号，而那与"这张能用"只差几秒。
        * 环境不符 ⇒ ``None``。同一个票值在另一个环境里是另一个问题。
        * 兑换成功后立即把它标记为已用（同一条 UPDATE 里带 ``used_at IS NULL``），
          所以并发两次只有一次成功。

        过期的行不在这里删；``prune_hop_tickets`` 由调用方按需调用。删除不是正确性
        的一部分（查询已经按过期判断），放在这里只会让一次失败把"能不能进"变成 500。
        """
        if not ticket:
            return None
        now = _utc_now()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT tenant_id, environment, workspace_id, expires_at, used_at"
                " FROM knowledge_hop_tickets WHERE ticket = ?",
                (ticket,),
            ).fetchone()
            if row is None or str(row["environment"]) != environment:
                return None
            if str(row["used_at"] or "") or datetime.fromisoformat(str(row["expires_at"])) <= now:
                return None
            updated = connection.execute(
                "UPDATE knowledge_hop_tickets SET used_at = ? WHERE ticket = ? AND used_at IS NULL",
                (_iso(now), ticket),
            )
            if updated.rowcount != 1:
                return None
        return {
            "tenant_id": str(row["tenant_id"]),
            "workspace_id": str(row["workspace_id"] or ""),
        }

    def prune_hop_tickets(self, *, older_than_seconds: float = 600.0) -> int:
        """删掉很久以前的行。票只在秒级有用，留一天的行只是垃圾。"""
        cutoff = _iso(_utc_now() - timedelta(seconds=older_than_seconds))
        with self._connection(write=True) as connection:
            deleted = connection.execute("DELETE FROM knowledge_hop_tickets WHERE expires_at < ?", (cutoff,))
        return int(deleted.rowcount or 0)

    def record_knowledge_access(
        self,
        *,
        tenant_id: str,
        environment: str,
        outcome: str,
        company_user_id: str = "",
        ragflow_email: str = "",
        reason_code: str = "",
    ) -> None:
        """Append one console hop to the knowledge access ledger (#926).

        ``outcome`` is ``granted`` or a stable refusal code. Refusals are recorded
        too: "who tried" and "what happened" are two different questions, and the
        first one is exactly what the shared-owner design otherwise loses.

        Never raises — same rule as the publish ledger, and the reason is the same:
        an audit write must not be able to turn a permitted action into a failure.
        """
        try:
            with self._connection(write=True) as connection:
                connection.execute(
                    """
                    INSERT INTO knowledge_access_ledger (
                        at, tenant_id, environment, company_user_id, ragflow_email, outcome, reason_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _iso(_utc_now()),
                        tenant_id,
                        environment,
                        company_user_id,
                        ragflow_email,
                        outcome,
                        reason_code,
                    ),
                )
        except sqlite3.Error:  # pragma: no cover - 磁盘问题不该挡一次跳转
            _LOGGER.warning("knowledge access audit write failed tenant=%s outcome=%s", tenant_id, outcome)

    def knowledge_access_entries(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if tenant_id is None:
                rows = connection.execute("SELECT * FROM knowledge_access_ledger ORDER BY id DESC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM knowledge_access_ledger WHERE tenant_id = ? ORDER BY id DESC",
                    (tenant_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def publish_audit_entries(self, *, app_id: str | None = None) -> list[dict[str, Any]]:
        """Read the ledger, newest first (for tests and operator queries)."""
        with self._connection() as connection:
            if app_id is None:
                rows = connection.execute("SELECT * FROM publish_audit_ledger ORDER BY id DESC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM publish_audit_ledger WHERE app_id = ? ORDER BY id DESC",
                    (app_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def create_trial_job(
        self,
        scope_fingerprint: str,
        *,
        order_no: str,
        draft_fingerprint: str,
        initiator: str,
        question: str = "",
        language: str = "zh",
        tenant_id: str | None = None,
        workflow_trial: bool = False,
        node_trial: bool = False,
    ) -> dict[str, Any]:
        """Create a **试运行** job (#772 F / #781). Shares ``standard_diagnoses``.

        Same reasoning as ``create_analysis_job``: the status vocabulary, the
        claim guard, the expiry sweep and restart convergence are already right,
        so a second table would be a second copy of them. What the trial adds is
        one discriminator value (``kind='trial'``) plus an audit column.

        **It produces no agent version and touches no routing.** The row is a
        record of one execution so an auditor can answer "who ran which draft
        against which order"; nothing reads it to decide what production serves.
        ``draft_fingerprint`` is a hash, not the prompt: the审计 answers "was it
        the same draft" without copying operator text into a second store.
        """
        scope_fingerprint = _scope(scope_fingerprint, "scope_fingerprint")
        order_no = _scope(order_no, "order_no")
        draft_fingerprint = _scope(draft_fingerprint, "draft_fingerprint")
        initiator = _scope(initiator, "initiator")
        tenant_id = _optional_scope(tenant_id, "tenant_id")
        language = _language(language)
        now = _utc_now()
        trial_id = TRIAL_ID_PREFIX + uuid.uuid4().hex
        with self._connection(write=True) as connection:
            self._expire_diagnoses(connection, now)
            connection.execute(
                """
                INSERT INTO standard_diagnoses (
                    diagnosis_id, scope_fingerprint, order_no, question, indicator_code, language,
                    internal_run_id, status, created_at, updated_at, deadline_at,
                    kind, trial_draft_fingerprint, trial_initiator, tenant_id
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'queued', ?, ?, ?, 'trial', ?, ?, ?)
                """,
                (
                    trial_id,
                    scope_fingerprint,
                    order_no,
                    redact_text(question),
                    language,
                    "node-sop-trial" if node_trial else "dify-workflow-trial" if workflow_trial else None,
                    _iso(now),
                    _iso(now),
                    _iso(now + DIAGNOSIS_DEADLINE),
                    draft_fingerprint,
                    initiator,
                    tenant_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM standard_diagnoses WHERE diagnosis_id = ?",
                (trial_id,),
            ).fetchone()
        return _standard_diagnosis_from_row(row)

    def update_standard_diagnosis(
        self,
        diagnosis_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        if status not in ACTIVE_DIAGNOSIS_STATUSES | TERMINAL_DIAGNOSIS_STATUSES:
            raise ValueError("invalid standard-diagnosis status")
        now = _utc_now()
        completed_at = now if status in TERMINAL_DIAGNOSIS_STATUSES else None
        expires_at = (
            now + DIAGNOSIS_COMPLETED_RETENTION
            if status in DIAGNOSIS.completed
            else now + DIAGNOSIS_FAILED_RETENTION
            if status in DIAGNOSIS_FAILED_RETENTION_STATUSES
            else now
            if status == "expired"
            else None
        )
        with self._connection(write=True) as connection:
            self._expire_diagnoses(connection, now)
            updated = connection.execute(
                """
                UPDATE standard_diagnoses SET
                    status = ?, result_json = COALESCE(?, result_json),
                    error_code = COALESCE(?, error_code),
                    error_message = COALESCE(?, error_message),
                    started_at = COALESCE(started_at, ?),
                    completed_at = COALESCE(completed_at, ?),
                    expires_at = COALESCE(?, expires_at), updated_at = ?
                WHERE diagnosis_id = ?
                  AND status IN ('queued', 'running')
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_code,
                    redact_text(error_message or "")[:1000] if error_message else None,
                    _iso(now) if status == "running" else None,
                    _iso(completed_at) if completed_at else None,
                    _iso(expires_at) if expires_at else None,
                    _iso(now),
                    diagnosis_id,
                ),
            )
        return updated.rowcount == 1

    def get_standard_diagnosis(
        self,
        diagnosis_id: str,
        scope_fingerprint: str,
    ) -> dict[str, Any] | None:
        if not diagnosis_id.startswith(("dx_", ANALYSIS_ID_PREFIX, TRIAL_ID_PREFIX)):
            return None
        with self._connection(write=True) as connection:
            self._expire_diagnoses(connection, _utc_now())
            row = connection.execute(
                """
                SELECT * FROM standard_diagnoses
                WHERE diagnosis_id = ? AND scope_fingerprint = ?
                """,
                (diagnosis_id, scope_fingerprint),
            ).fetchone()
        return _standard_diagnosis_from_row(row) if row is not None else None

    def list_standard_diagnoses(
        self,
        scope_fingerprint: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._connection(write=True) as connection:
            self._expire_diagnoses(connection, _utc_now())
            rows = connection.execute(
                """
                SELECT diagnosis_id, order_no, question, indicator_code, status,
                       created_at, updated_at
                FROM standard_diagnoses
                WHERE scope_fingerprint = ? AND kind = 'order'
                ORDER BY created_at DESC LIMIT ?
                """,
                (scope_fingerprint, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_standard_diagnoses(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM standard_diagnoses").fetchone()
        return int(row["count"])

    # ------------------------------------------------------------------
    # assistant_questions (general-question jobs, T3/#153)
    # ------------------------------------------------------------------

    def create_assistant_question(
        self,
        scope_fingerprint: str,
        question: str,
        *,
        internal_run_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        scope_fingerprint = _scope(scope_fingerprint, "scope_fingerprint")
        question = _question(question, "question")
        tenant_id = _optional_scope(tenant_id, "tenant_id")
        now = _utc_now()
        qa_id = "qa_" + uuid.uuid4().hex
        with self._connection(write=True) as connection:
            self._expire_assistant_questions(connection, now)
            connection.execute(
                """
                INSERT INTO assistant_questions (
                    qa_id, scope_fingerprint, question, internal_run_id,
                    status, created_at, updated_at, deadline_at, tenant_id
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    qa_id,
                    scope_fingerprint,
                    redact_text(question),
                    internal_run_id,
                    _iso(now),
                    _iso(now),
                    _iso(now + DIAGNOSIS_DEADLINE),
                    tenant_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM assistant_questions WHERE qa_id = ?",
                (qa_id,),
            ).fetchone()
        return _assistant_question_from_row(row)

    def update_assistant_question(
        self,
        qa_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        if status not in ACTIVE_DIAGNOSIS_STATUSES | TERMINAL_DIAGNOSIS_STATUSES:
            raise ValueError("invalid assistant-question status")
        now = _utc_now()
        completed_at = now if status in TERMINAL_DIAGNOSIS_STATUSES else None
        expires_at = (
            now + DIAGNOSIS_COMPLETED_RETENTION
            if status in DIAGNOSIS.completed
            else now + DIAGNOSIS_FAILED_RETENTION
            if status in DIAGNOSIS_FAILED_RETENTION_STATUSES
            else now
            if status == "expired"
            else None
        )
        with self._connection(write=True) as connection:
            self._expire_assistant_questions(connection, now)
            updated = connection.execute(
                """
                UPDATE assistant_questions SET
                    status = ?, result_json = COALESCE(?, result_json),
                    error_code = COALESCE(?, error_code),
                    error_message = COALESCE(?, error_message),
                    started_at = COALESCE(started_at, ?),
                    completed_at = COALESCE(completed_at, ?),
                    expires_at = COALESCE(?, expires_at), updated_at = ?
                WHERE qa_id = ? AND status IN ('queued', 'running')
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_code,
                    redact_text(error_message or "")[:1000] if error_message else None,
                    _iso(now) if status == "running" else None,
                    _iso(completed_at) if completed_at else None,
                    _iso(expires_at) if expires_at else None,
                    _iso(now),
                    qa_id,
                ),
            )
        return updated.rowcount > 0

    def get_assistant_question(
        self,
        qa_id: str,
        scope_fingerprint: str,
    ) -> dict[str, Any] | None:
        with self._connection(write=True) as connection:
            self._expire_assistant_questions(connection, _utc_now())
            row = connection.execute(
                """
                SELECT * FROM assistant_questions
                WHERE qa_id = ? AND scope_fingerprint = ?
                """,
                (qa_id, scope_fingerprint),
            ).fetchone()
        return _assistant_question_from_row(row) if row is not None else None

    # ------------------------------------------------------------------
    # Dify knowledge sources: which RAGFlow datasets one `knowledge_id` reaches.
    #
    # Read live on every retrieval, never cached in the process: an operator who
    # unticks a box expects the next question to stop reaching that library, not
    # the next deploy to.
    # ------------------------------------------------------------------

    def list_knowledge_id_bindings(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Every registered knowledge source, newest first.

        ``knowledge_base_ids`` comes back as a tuple so callers cannot
        accidentally treat the `|`-joined storage form as the value.
        """
        with self._connection() as connection:
            if tenant_id is None:
                rows = connection.execute(
                    """
                    SELECT knowledge_id, tenant_id, knowledge_base_ids, display_name,
                           created_by, created_at
                    FROM dify_knowledge_bindings ORDER BY created_at DESC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT knowledge_id, tenant_id, knowledge_base_ids, display_name,
                           created_by, created_at
                    FROM dify_knowledge_bindings WHERE tenant_id = ? ORDER BY created_at DESC
                    """,
                    (tenant_id,),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["knowledge_base_ids"] = tuple(
                part for part in str(item["knowledge_base_ids"]).split("|") if part
            )
            result.append(item)
        return result

    def get_knowledge_id_binding(self, knowledge_id: str) -> dict[str, Any] | None:
        for row in self.list_knowledge_id_bindings():
            if row["knowledge_id"] == knowledge_id:
                return row
        return None

    def set_knowledge_id_binding(
        self,
        knowledge_id: str,
        tenant_id: str,
        knowledge_base_ids: tuple[str, ...],
        *,
        display_name: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        """Create or replace one knowledge source.

        Replace, not merge: the operator's form shows the complete set of
        ticked boxes, so the submitted list IS the intended state. Merging
        would make "untick a library" impossible through this path.
        """
        if not knowledge_base_ids:
            raise ValueError("a knowledge source needs at least one knowledge base")
        now = _utc_now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO dify_knowledge_bindings
                    (knowledge_id, tenant_id, knowledge_base_ids, display_name, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(knowledge_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    knowledge_base_ids = excluded.knowledge_base_ids,
                    display_name = excluded.display_name,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at
                """,
                (
                    knowledge_id,
                    tenant_id,
                    "|".join(knowledge_base_ids),
                    display_name,
                    created_by,
                    _iso(now),
                ),
            )
        return {
            "knowledge_id": knowledge_id,
            "tenant_id": tenant_id,
            "knowledge_base_ids": tuple(knowledge_base_ids),
            "display_name": display_name,
        }

    def remove_knowledge_id_binding(self, knowledge_id: str) -> bool:
        with self._connection(write=True) as connection:
            removed = connection.execute(
                "DELETE FROM dify_knowledge_bindings WHERE knowledge_id = ?", (knowledge_id,)
            )
        return removed.rowcount > 0

    # ---- 一租户一 RAGFlow 租户（#924） ------------------------------------

    def get_ragflow_tenant(self, tenant_id: str, environment: str) -> dict[str, Any] | None:
        """This tenant's RAGFlow side, or None when it was never onboarded.

        ``credential_ciphertext`` comes back as stored — callers decrypt, and the
        plaintext never leaves that one call. ``model_checked_at`` empty means
        onboarding recorded a row but never proved the model works: a half-built
        tenant, which the onboarding command repairs rather than skipping.
        """
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT tenant_id, environment, ragflow_email, credential_ciphertext,
                       model_checked_at, created_at, updated_at
                FROM ragflow_tenants WHERE tenant_id = ? AND environment = ?
                """,
                (tenant_id, environment),
            ).fetchone()
        return dict(row) if row else None

    def record_ragflow_tenant(
        self,
        tenant_id: str,
        environment: str,
        *,
        ragflow_email: str,
        credential_ciphertext: str,
        model_checked: bool,
    ) -> dict[str, Any]:
        """Upsert one tenant's RAGFlow side. Idempotent by design (#924).

        ``model_checked`` is a separate fact from "the row exists": a run that
        registered the account but failed the model check must leave the row
        without a check timestamp, so the next run knows to repair instead of
        concluding "already onboarded" and walking away.
        """
        now = _iso(_utc_now())
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT created_at, model_checked_at FROM ragflow_tenants"
                " WHERE tenant_id = ? AND environment = ?",
                (tenant_id, environment),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            checked_at = now if model_checked else (str(existing["model_checked_at"]) if existing else "")
            connection.execute(
                """
                INSERT INTO ragflow_tenants
                    (tenant_id, environment, ragflow_email, credential_ciphertext,
                     model_checked_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, environment) DO UPDATE SET
                    ragflow_email = excluded.ragflow_email,
                    credential_ciphertext = excluded.credential_ciphertext,
                    model_checked_at = excluded.model_checked_at,
                    updated_at = excluded.updated_at
                """,
                (tenant_id, environment, ragflow_email, credential_ciphertext, checked_at, created_at, now),
            )
        return {
            "tenant_id": tenant_id,
            "environment": environment,
            "ragflow_email": ragflow_email,
            "model_checked_at": checked_at,
        }

    def list_ragflow_tenants(self, environment: str) -> list[dict[str, Any]]:
        """Every onboarded tenant in one environment, for the triple check (#927).

        Deliberately returns the ciphertext column name but never its value: a
        listing that carries secrets is a listing that ends up in a log.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT tenant_id, environment, ragflow_email, model_checked_at,
                       created_at, updated_at
                FROM ragflow_tenants WHERE environment = ? ORDER BY created_at
                """,
                (environment,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_assistant_questions(
        self,
        scope_fingerprint: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._connection(write=True) as connection:
            self._expire_assistant_questions(connection, _utc_now())
            rows = connection.execute(
                """
                SELECT qa_id, question, status, created_at, updated_at
                FROM assistant_questions
                WHERE scope_fingerprint = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (scope_fingerprint, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_assistant_questions(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM assistant_questions").fetchone()
        return int(row["count"])

    def recover_interrupted_jobs(self) -> None:
        """Converge the jobs a previous gateway process left in flight (#492).

        A restart kills the workers but not the rows, and the only other exit is
        each job's deadline — so without this a caller waits out the whole
        deadline on work nobody is doing. Runs on the gateway's boot path, where
        the restart actually happened.

        **Not in `__init__`.** That is where the other two tables used to do it,
        and it was wrong: construction happens on every short-lived CLI command
        (`aiops-gateway devices`, `issue-enrollment`, `revoke-device`), so
        listing devices ended a diagnosis that was still being worked on and the
        caller read a result-less `expired`. The verdict here is the honest one
        — `failed` with a code saying the process holding it died — and it uses
        the same three-way distinction the question table already used:
        `expired` means the job outran its deadline, `cancelled` means the user
        stopped it, and a restart is neither.

        Each write goes through the table's own update path, so its claim-guard
        and retention tier apply: a row another path drove terminal between the
        scan and the write is left alone, which is the same quiet exit the
        workers take on a refused write.
        """
        with self._connection() as connection:
            jobs = {
                "health_report_jobs": [
                    str(row["job_id"])
                    for row in connection.execute(
                        "SELECT job_id FROM health_report_jobs WHERE status IN ('queued', 'running')"
                    ).fetchall()
                ],
                "standard_diagnoses": [
                    str(row["diagnosis_id"])
                    for row in connection.execute(
                        "SELECT diagnosis_id FROM standard_diagnoses WHERE status IN ('queued', 'running')"
                    ).fetchall()
                ],
            }
        for job_id in jobs["health_report_jobs"]:
            self.update_health_job(
                job_id,
                status="failed",
                error_code=HEALTH_JOB_RESTART_ERROR_CODE,
                error_message=HEALTH_JOB_RESTART_ERROR_MESSAGE,
            )
        for diagnosis_id in jobs["standard_diagnoses"]:
            self.update_standard_diagnosis(
                diagnosis_id,
                status="failed",
                error_code=DIAGNOSIS_RESTART_ERROR_CODE,
                error_message=DIAGNOSIS_RESTART_ERROR_MESSAGE,
            )
        self.recover_assistant_questions()
        # The conversations those jobs belonged to are still holding their
        # generation slots, and nothing will complete those turns: the worker
        # that owned them died with the process. Freeing them here is the
        # difference between "retry now" and "retry in two minutes" for the
        # caller whose generation the deploy killed (#492).
        from aiops_diagnostics.conversation.conversation_store import ConversationStore

        ConversationStore(self.path).recover_interrupted_turns()

    def recover_assistant_questions(self) -> None:
        """Converge the questions a previous gateway process left in flight.

        A restart kills the workers but not the rows, and the only other exit is
        the 15-minute deadline — so without this a caller waits out the whole
        deadline on a question nobody is answering. Runs on the gateway's boot
        path before it serves a request, so no caller ever polls a `running`
        job whose worker is gone.

        The write goes through ``update_assistant_question`` rather than its own
        SQL: that path already applies the failed retention tier and its
        claim-guard, so a row another path drove terminal between the scan and
        the write is left alone (``False``, nothing to converge — the same quiet
        exit the worker takes on a refused write). A row whose deadline already
        passed is swept to `expired` by that path's own expiry first, which is
        the accurate verdict: it outran its budget, the restart only noticed.

        Reached through ``recover_interrupted_jobs()``, which the boot path calls
        for all three asynchronous tables. It is kept as its own method because
        this table's convergence predates the other two (T3/#356) and has its own
        recovery test; the shared entry point is what keeps them together.

        This method is deliberately NOT reachable from ``GatewayStore.__init__``:
        construction runs on short-lived CLI commands (``aiops-gateway devices``)
        too, so a row would be told "the gateway restarted" by a process that
        only listed devices. That mistake is what the other two tables used to
        make, and #492 moved them here.
        """
        with self._connection() as connection:
            in_flight = [
                str(row["qa_id"])
                for row in connection.execute(
                    "SELECT qa_id FROM assistant_questions WHERE status IN ('queued', 'running')"
                ).fetchall()
            ]
        for qa_id in in_flight:
            self.update_assistant_question(
                qa_id,
                status="failed",
                error_code=ASSISTANT_QUESTION_RESTART_ERROR_CODE,
                error_message=ASSISTANT_QUESTION_RESTART_ERROR_MESSAGE,
            )

    @staticmethod
    def _expire_diagnoses(connection: sqlite3.Connection, now: datetime) -> None:
        statements = expire_statements("standard_diagnoses", DIAGNOSIS, _iso(now))
        connection.execute(*statements.claim)
        connection.execute(*statements.sweep)

    @staticmethod
    def _expire_assistant_questions(connection: sqlite3.Connection, now: datetime) -> None:
        statements = expire_statements("assistant_questions", QUESTION, _iso(now))
        connection.execute(*statements.claim)
        connection.execute(*statements.sweep)

    def count_expired_jobs_by_tenant(self, *, since: str | None = None) -> dict[str, int]:
        """Jobs that ended by EXPIRY, counted per tenant (#725 follow-up).

        This exists because the expiry sweep writes no metric row at all: it runs
        as raw SQL inside `_expire_*`, so a job that ran out its deadline used to
        leave **no trace in the tenant-scoped metrics** — `expired` was invisible
        and "everything looks completed" was the only reading available. A metric
        row per expiry would have meant teaching a pure store layer to write
        metrics; counting the tagged rows answers the same question without that
        coupling, and it answers it from the **job** rather than from a second
        record of the job.

        ``tenant_id`` is nullable and **not backfilled** (see the migration):
        rows written before that column existed report as ``""``, so "unknown"
        stays visibly unknown instead of being folded into a real tenant.
        """
        cutoff = since or "1970-01-01T00:00:00+00:00"
        counts: dict[str, int] = {}
        for table in JOB_TABLES:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT tenant_id, COUNT(*) AS n FROM {table} "
                    "WHERE status = 'expired' AND created_at >= ? GROUP BY tenant_id",
                    (cutoff,),
                ).fetchall()
            for row in rows:
                key = str(row["tenant_id"]) if row["tenant_id"] else ""
                counts[key] = counts.get(key, 0) + int(row["n"])
        return counts

    # NOTE (#492): `runs` is deliberately absent from `recover_interrupted_jobs`.
    # The device path is scheduled for retirement and this table has no deadline
    # column, so giving it a convergence rule is a product decision that nobody
    # has made — PRD #410 left it explicit. Today a `queued`/`running` run whose
    # process died stays that way until `update_run` is called with a terminal
    # status; it is never swept. Recorded here because "the other three are
    # converged at boot" is the shape that makes an omission look like an
    # oversight rather than a decision.

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS health_report_jobs (
                    job_id TEXT PRIMARY KEY,
                    scope_fingerprint TEXT NOT NULL,
                    order_no TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT 'zh',
                    report_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_health_jobs_reuse
                    ON health_report_jobs(scope_fingerprint, order_no, rule_version, created_at DESC);
                CREATE TABLE IF NOT EXISTS standard_diagnoses (
                    diagnosis_id TEXT PRIMARY KEY,
                    scope_fingerprint TEXT NOT NULL,
                    order_no TEXT NOT NULL,
                    question TEXT NOT NULL,
                    indicator_code TEXT,
                    language TEXT NOT NULL DEFAULT 'zh',
                    internal_run_id TEXT,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    expires_at TEXT
                );
                
                CREATE INDEX IF NOT EXISTS idx_standard_diagnoses_scope_created
                    ON standard_diagnoses(scope_fingerprint, created_at DESC);
                CREATE TABLE IF NOT EXISTS assistant_questions (
                    qa_id TEXT PRIMARY KEY,
                    scope_fingerprint TEXT NOT NULL,
                    question TEXT NOT NULL,
                    internal_run_id TEXT,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_assistant_questions_scope_created
                    ON assistant_questions(scope_fingerprint, created_at DESC);
                -- 发布审计账本（#772 D / #780）：**追加写**，一行一次发布尝试。
                --
                -- 它与 ``standard_diagnoses`` 是两件事：那张表记"一次智能体版本的
                -- 终态"，这张记"谁在何时把哪一版发上了生产（含被拒的尝试）"。合表会
                -- 让被拒的尝试无处安放（它不产生版本），而那正是要审计的东西之一。
                --
                -- 发起人三列**不是授权依据**：授权在桥那一侧完成（#780），这里只记
                -- "是谁发的"。所以缺发起人时照常发布，只把它记成未知。
                CREATE TABLE IF NOT EXISTS publish_audit_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    business_entry TEXT NOT NULL DEFAULT '',
                    agent_name TEXT NOT NULL DEFAULT '',
                    company_user_id TEXT NOT NULL DEFAULT '',
                    company_username TEXT NOT NULL DEFAULT '',
                    dify_account_id TEXT NOT NULL DEFAULT '',
                    version_no INTEGER,
                    action TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_publish_audit_at
                    ON publish_audit_ledger(at DESC);
                CREATE INDEX IF NOT EXISTS idx_publish_audit_app
                    ON publish_audit_ledger(app_id, at DESC);
                -- 知识库控制台的访问账本（#926 / PRD #923）。
                --
                -- **为什么需要它**：登录代理用「租户 owner」身份进 RAGFlow，所以 RAGFlow
                -- 那一侧看到的所有操作者都是同一个 owner，分不出是谁建的库。这个决定是
                -- 有意的（每租户一个 RAGFlow 租户），代价就用这张表补：谁在什么时候、
                -- 以哪个租户的身份进去过，记在我们这一侧 —— 与发布审计同一条思路。
                --
                -- 追加写、不更新、不删除。它是可观测性，不是授权输入：写失败只告警，
                -- 绝不把一次合法跳转变成失败（否则观测把自己变成了故障源）。
                CREATE TABLE IF NOT EXISTS knowledge_access_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    company_user_id TEXT NOT NULL DEFAULT '',
                    ragflow_email TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_access_tenant
                    ON knowledge_access_ledger(tenant_id, at DESC);

                -- 一次性跳转票（#985）。
                --
                -- **为什么需要它**：cookie 只能由它所属的域设置。桥在
                -- `dify.ranlei.work` 上发 `Set-Cookie: Domain=.mall.qushiyun.com` 会被
                -- 浏览器**拒绝**（设置方不在那个域下）。所以会话必须由 `kb.mall` 那一跳
                -- 自己种下去 —— 两次导航之间要传的东西就是这张票。
                --
                -- 存库不存内存：网关是多进程的，内存票会随机失效，而那表现为"有时能进、
                -- 有时不能"，是最难排查的一种失败。
                --
                -- 票里**不含会话**：会话在兑换那一刻才去换，票只是一个"这个浏览器刚刚
                -- 通过了身份"的一次性凭据。这样即使票被读到，也换不出别人的东西 ——
                -- 兑换仍然要求一次有效的 RAGFlow 登录。
                CREATE TABLE IF NOT EXISTS knowledge_hop_tickets (
                    ticket TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_hop_expiry
                    ON knowledge_hop_tickets(expires_at);

                -- 运营在界面上点的「知识源」：一条 = Dify 里一个外部知识库。
                --
                -- 为什么不是 env 里的 DIFY_KNOWLEDGE_BINDINGS：那是一行必须手写的
                -- `knowledge_id:tenant:kb1|kb2`，只有工程师能填，填错静默失效；运营
                -- 需要在界面上点选，点击必须落在一个可写、可读回的地方。
                --
                -- 形状与 env 登记表**逐字段对应**（knowledge_id / tenant_id /
                -- 一个或多个 dataset id），所以两者可以合并成同一张映射表，
                -- 迁移也只是把已有的 env 行抄进来。
                --
                -- knowledge_base_ids 存成 `|` 分隔：与 env 语法一致，读的人不需要
                -- 再学一种编码；这里没有查询需求，JSON 的额外表达力用不上。
                CREATE TABLE IF NOT EXISTS dify_knowledge_bindings (
                    knowledge_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    knowledge_base_ids TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dify_knowledge_tenant
                    ON dify_knowledge_bindings(tenant_id, created_at DESC);

                -- 一租户一 RAGFlow 租户（#924，PRD #923 的 1:1:1）。
                --
                -- 这里只存**我们这一侧**记的东西：RAGFlow 的登录邮箱、凭据（密文）、
                -- 以及最近一次校验模型可用性的结果。映射的权威仍然在 kb-service 的
                -- `tenancy.db` —— 那张表才是 kb-service 读的；这张表存在的理由是
                -- 「开户要有幂等位」与「凭据要有可轮换的落点」，两者 kb-service 都不管。
                --
                -- 凭据以密文存储（`enc:v1:` 前缀，密钥来自部署环境，不入库）；明文
                -- **任何时候**都不写这一列，也不进日志。
                --
                -- `model_checked_at` 为空 = 从没校验成功过。开户时校验失败**不写这一列**，
                -- 所以「有行但没校验过」就是半成品，重跑开户会去补。
                CREATE TABLE IF NOT EXISTS ragflow_tenants (
                    tenant_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    ragflow_email TEXT NOT NULL,
                    credential_ciphertext TEXT NOT NULL DEFAULT '',
                    model_checked_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, environment)
                );
                """
            )
            diagnosis_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(standard_diagnoses)").fetchall()
            }
            if "language" not in diagnosis_columns:
                connection.execute(
                    "ALTER TABLE standard_diagnoses ADD COLUMN language TEXT NOT NULL DEFAULT 'zh'"
                )
            # #697: the operations-analysis surface reuses this table (the status
            # vocabulary, claim guard and expiry sweep are already right for it)
            # and needs a discriminator plus its own scope fields. The default
            # makes every existing row and every existing INSERT keep meaning
            # exactly what it meant before.
            for name, definition in (
                ("kind", "TEXT NOT NULL DEFAULT 'order'"),
                ("site_ids_json", "TEXT"),
                ("window_start", "TEXT"),
                ("window_end", "TEXT"),
                ("rule_version", "TEXT"),
                ("card", "TEXT"),
                # #781 试运行：kinds='trial' 行用这两列记审计（发起人与草稿摘要哈希）。
                # 默认 NULL 让存量行与存量 INSERT 的含义逐字不变。
                ("trial_draft_fingerprint", "TEXT"),
                ("trial_initiator", "TEXT"),
            ):
                if name not in diagnosis_columns:
                    connection.execute(f"ALTER TABLE standard_diagnoses ADD COLUMN {name} {definition}")
            # Created HERE, not in the CREATE TABLE script above: that script runs
            # before the ALTER above adds the column, so on a database predating
            # this change the index would name a column that does not exist yet
            # and opening the store would fail on the gateway's own boot path.
            # (The same trap the health-job language index documents below.)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_standard_diagnoses_scope_kind_created "
                "ON standard_diagnoses(scope_fingerprint, kind, created_at DESC)"
            )
            health_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(health_report_jobs)").fetchall()
            }
            if "language" not in health_columns:
                connection.execute(
                    "ALTER TABLE health_report_jobs ADD COLUMN language TEXT NOT NULL DEFAULT 'zh'"
                )
            # Reuse is per-language: a report's prose is generated once, in the
            # language its job was created in, so reusing another language's row
            # would hand a reader a report in a language they did not ask for.
            # The index is created HERE, not in the CREATE TABLE script above:
            # it names a column that script does not define, so on a database
            # predating this change the index would be created before the ALTER
            # adds the column — opening the store would fail on the gateway's
            # own boot path. (Caught before merge; a fresh database cannot
            # reproduce it.)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_health_jobs_reuse_language "
                "ON health_report_jobs(scope_fingerprint, order_no, rule_version, language, created_at DESC)"
            )
            # #725 follow-up: the three job tables carry only `scope_fingerprint`,
            # which is an **irreversible digest** — so a job that ended by expiry
            # or by restart could not be attributed to a tenant, and `expired`
            # never appeared in the tenant-scoped metrics at all. The digest
            # proves "this row ran under that range"; the tenant id is the part
            # `tenant_id = ?` filtering needs. Nullable, and **not backfilled**:
            # "unknown" is the honest value for rows written before this column
            # existed, and a wrong tenant is worse than a missing one.
            for table in ("standard_diagnoses", "health_report_jobs", "assistant_questions"):
                table_columns = {
                    str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if "tenant_id" not in table_columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT")
            # Constructing a store converges NOTHING (#492). This path used to
            # end every `queued`/`running` health job and diagnosis without a
            # deadline condition, and `__init__` runs on every construction —
            # short-lived CLI commands included. `aiops-gateway devices` ended a
            # diagnosis that was still being worked on, and the caller then read
            # a result-less `expired`. Restart convergence belongs on the boot
            # path, where the restart actually happened.
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()


def _health_job_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_report = result.pop("report_json", None)
    result["report"] = json.loads(raw_report) if raw_report else None
    return result


def _standard_diagnosis_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_result = result.pop("result_json", None)
    result["result"] = json.loads(raw_result) if raw_result else None
    return result


def _assistant_question_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_result = result.pop("result_json", None)
    result["result"] = json.loads(raw_result) if raw_result else None
    return result


def _window_bound(value: str, name: str) -> datetime:
    """Parse one ISO-8601 window bound, or refuse it.

    The bound is parsed here rather than trusted as text: the SQL compares it
    against a datetime column, and a string that merely looks like a date would
    be compared as one and silently match nothing (or everything, depending on
    the engine's coercion). Naive input is read as UTC — the store's own clock,
    and the only reading that keeps ``start < end`` meaningful.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} is not an ISO-8601 timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _scope(value: str, name: str) -> str:
    candidate = value.strip()
    if not SAFE_SCOPE.fullmatch(candidate):
        raise ValueError(f"{name} contains unsupported characters")
    return candidate


def _optional_scope(value: str | None, name: str) -> str | None:
    return _scope(value, name) if value and value.strip() else None


#: 运维分析作业的 id 前缀（#697）。与 ``dx_`` 并列在诊断作业表上，靠它区分种类。
ANALYSIS_ID_PREFIX = "an_"
#: 试运行作业的 id 前缀（#781）。与 ``an_`` / ``dx_`` 一样带前缀，是为了让
#: ``get_standard_diagnosis`` 能在不查库的前提下把明显不是本表的 id 判掉。
TRIAL_ID_PREFIX = "tr_"

QUESTION_MAX_LEN = 4000
INDICATOR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _question(value: str, name: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError(f"{name} must not be empty")
    if len(candidate) > QUESTION_MAX_LEN:
        raise ValueError(f"{name} exceeds the {QUESTION_MAX_LEN}-character limit")
    return candidate


def _language(value: str) -> str:
    """Normalise a language tag for storage.

    A DECLARED tag is stored as declared — `zh-Hant` stays `zh-Hant`. The old
    unconditional `split("-", 1)[0]` was the second of the two places a
    Traditional request silently became Simplified: even after the resolver
    preserved the script, the row would have stored `zh`, so the diagnosis and
    the report would report Simplified for an answer written in Traditional.
    """
    candidate = (value or DEFAULT_LANGUAGE).strip().lower()
    if not re.fullmatch(r"[a-z]{2,8}(?:-[a-z0-9]{2,8})?", candidate):
        raise ValueError("language is invalid")
    for declared in SUPPORTED_LANGUAGES:
        if declared.lower() == candidate:
            return declared
    return candidate.split("-", 1)[0]


def _label(value: str, name: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or any(ord(character) < 32 for character in candidate):
        raise ValueError(f"{name} is invalid")
    return candidate


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
