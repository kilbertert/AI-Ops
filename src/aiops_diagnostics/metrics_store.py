"""Redacted agent-run metrics (T7/#174).

One row per completed assistant-route interaction (faq / qa / diagnosis /
debug-run), written at the same completion points that already persist job
state. The row carries ONLY: tenant, route type, agent/version, conversation
id, retrieval status, search and media counts, latency, tokens, and a failure
code — never the question text, answer text, prompt, knowledge-base content,
media URLs, tokens, or storage paths (long-term monitoring stays redacted by
construction; the raw QA text lives separately in ``assistant_questions``
with its own retention).

Retention: rows are pruned after ``METRICS_RETENTION_DAYS`` (30) so the
aggregate surface stays bounded.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file

METRICS_RETENTION_DAYS = 30

ROUTE_TYPES = frozenset({"faq", "qa", "diagnosis", "debug"})
RETRIEVAL_STATUSES = frozenset({"", "found", "not_found", "unavailable", "limited"})
OUTCOME_TYPES = frozenset({"completed", "failed", "cancelled", "busy"})

_SAFE_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.#:-]{0,127}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class MetricsError(RuntimeError):
    code = "METRICS_ERROR"


class MetricsValidationError(MetricsError):
    code = "METRICS_INVALID"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


_EXPIRE_SWEEP = {"at": datetime(1970, 1, 1, tzinfo=UTC)}


def _last_expire() -> datetime:
    return _EXPIRE_SWEEP["at"]


def _mark_expire(now: datetime) -> None:
    _EXPIRE_SWEEP["at"] = now


class MetricsStore:
    """agent_run_metrics table sharing the gateway SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS agent_run_metrics (
                    metric_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    route_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    agent_id TEXT,
                    agent_version_key TEXT,
                    conversation_id TEXT,
                    retrieval_status TEXT NOT NULL DEFAULT '',
                    searches INTEGER NOT NULL DEFAULT 0,
                    media_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_metrics_tenant_created
                    ON agent_run_metrics(tenant_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_metrics_agent
                    ON agent_run_metrics(agent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_metrics_created
                    ON agent_run_metrics(created_at);
                """
            )
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            if write:
                connection.commit()
        finally:
            connection.close()

    # ── writes ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        tenant_id: str,
        route_type: str,
        outcome: str,
        agent_id: str | None = None,
        agent_version_key: str | None = None,
        conversation_id: str | None = None,
        retrieval_status: str = "",
        searches: int = 0,
        media_count: int = 0,
        duration_ms: int | None = None,
        token_count: int = 0,
        error_code: str | None = None,
        created_at: datetime | None = None,
    ) -> str:
        """Append one redacted run row; returns its metric id.

        All inputs are validated against tight patterns; unknown values are
        rejected (never silently coerced) because aggregates feed admin views.
        """
        if route_type not in ROUTE_TYPES:
            raise MetricsValidationError("route_type is invalid")
        if outcome not in OUTCOME_TYPES:
            raise MetricsValidationError("outcome is invalid")
        if retrieval_status not in RETRIEVAL_STATUSES:
            raise MetricsValidationError("retrieval_status is invalid")
        if not tenant_id or not _SAFE_TENANT.fullmatch(tenant_id):
            raise MetricsValidationError("tenant_id is invalid")
        for value, name in (
            (agent_id, "agent_id"),
            (agent_version_key, "agent_version_key"),
            (conversation_id, "conversation_id"),
        ):
            if value is not None and (not value or not _SAFE_ID.fullmatch(value)):
                raise MetricsValidationError(f"{name} is invalid")
        if error_code is not None and not _SAFE_CODE.fullmatch(error_code):
            raise MetricsValidationError("error_code is invalid")
        if searches < 0 or media_count < 0 or token_count < 0:
            raise MetricsValidationError("counts must be non-negative")
        if duration_ms is not None and duration_ms < 0:
            raise MetricsValidationError("duration_ms must be non-negative")
        now = created_at or _utc_now()
        metric_id = "mtr_" + uuid.uuid4().hex
        # ponytail: throttle expiry to one sweep per 10 minutes per process —
        # a DELETE on every insert was a per-write full scan under load.
        if now - _last_expire() > timedelta(minutes=10):
            with self._connection(write=True) as connection:
                self._expire(connection, now)
            _mark_expire(now)
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO agent_run_metrics (
                    metric_id, tenant_id, route_type, outcome, agent_id,
                    agent_version_key, conversation_id, retrieval_status,
                    searches, media_count, duration_ms, token_count,
                    error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric_id,
                    tenant_id,
                    route_type,
                    outcome,
                    agent_id,
                    agent_version_key,
                    conversation_id,
                    retrieval_status,
                    searches,
                    media_count,
                    duration_ms,
                    token_count,
                    error_code,
                    _iso(now),
                ),
            )
        return metric_id

    def _expire(self, connection: sqlite3.Connection, now: datetime) -> None:
        cutoff = _iso(now - timedelta(days=METRICS_RETENTION_DAYS))
        connection.execute("DELETE FROM agent_run_metrics WHERE created_at < ?", (cutoff,))

    # ── reads ────────────────────────────────────────────────────────

    def summary(
        self, tenant_id: str, *, agent_id: str | None = None, limit_hours: int = 720
    ) -> dict[str, Any]:
        """Aggregate redacted counts for one tenant (never cross-tenant)."""
        if not tenant_id or not _SAFE_TENANT.fullmatch(tenant_id):
            raise MetricsValidationError("tenant_id is invalid")
        if limit_hours < 1 or limit_hours > 24 * METRICS_RETENTION_DAYS:
            raise MetricsValidationError("limit_hours is invalid")
        cutoff = _iso(_utc_now() - timedelta(hours=limit_hours))
        conditions = ["tenant_id = ?", "created_at >= ?"]
        params: list[Any] = [tenant_id, cutoff]
        if agent_id is not None:
            if not _SAFE_ID.fullmatch(agent_id):
                raise MetricsValidationError("agent_id is invalid")
            conditions.append("agent_id = ?")
            params.append(agent_id)
        where = " AND ".join(conditions)

        def _rows(query: str, extra: list[Any] | None = None) -> list[sqlite3.Row]:
            with self._connection() as connection:
                return connection.execute(query, params + (extra or [])).fetchall()

        totals = _rows(
            f"""
            SELECT COUNT(*) AS runs,
                   SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN outcome = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                   SUM(CASE WHEN outcome = 'busy' THEN 1 ELSE 0 END) AS busy,
                   AVG(duration_ms) AS avg_duration_ms,
                   MAX(duration_ms) AS max_duration_ms,
                   SUM(token_count) AS tokens,
                   SUM(searches) AS searches,
                   SUM(media_count) AS media
            FROM agent_run_metrics WHERE {where}
            """
        )[0]
        by_route = _rows(
            f"""
            SELECT route_type, COUNT(*) AS runs,
                   SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM agent_run_metrics WHERE {where}
            GROUP BY route_type ORDER BY route_type
            """
        )
        by_retrieval = _rows(
            f"""
            SELECT retrieval_status, COUNT(*) AS runs
            FROM agent_run_metrics
            WHERE {where} AND route_type IN ('qa', 'debug')
            GROUP BY retrieval_status ORDER BY retrieval_status
            """
        )
        by_error = _rows(
            f"""
            SELECT COALESCE(error_code, '') AS error_code, COUNT(*) AS runs
            FROM agent_run_metrics WHERE {where} AND outcome != 'completed'
            GROUP BY error_code ORDER BY runs DESC LIMIT 20
            """
        )
        return {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "window_hours": limit_hours,
            "totals": {
                "runs": int(totals["runs"] or 0),
                "completed": int(totals["completed"] or 0),
                "failed": int(totals["failed"] or 0),
                "cancelled": int(totals["cancelled"] or 0),
                "busy": int(totals["busy"] or 0),
                "avg_duration_ms": round(float(totals["avg_duration_ms"]))
                if totals["avg_duration_ms"] is not None
                else None,
                "max_duration_ms": int(totals["max_duration_ms"])
                if totals["max_duration_ms"] is not None
                else None,
                "tokens": int(totals["tokens"] or 0),
                "searches": int(totals["searches"] or 0),
                "media": int(totals["media"] or 0),
            },
            "by_route": [dict(row) for row in by_route],
            "by_retrieval": [dict(row) for row in by_retrieval],
            "by_error": [dict(row) for row in by_error],
        }

    def list_runs(
        self,
        tenant_id: str,
        *,
        agent_id: str | None = None,
        route_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Recent redacted run rows for one tenant (admin detail view)."""
        if route_type is not None and route_type not in ROUTE_TYPES:
            raise MetricsValidationError("route_type is invalid")
        if not 1 <= limit <= 200:
            raise MetricsValidationError("limit is invalid")
        if not tenant_id or not _SAFE_TENANT.fullmatch(tenant_id):
            raise MetricsValidationError("tenant_id is invalid")
        conditions = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if agent_id is not None:
            if not _SAFE_ID.fullmatch(agent_id):
                raise MetricsValidationError("agent_id is invalid")
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if route_type is not None:
            conditions.append("route_type = ?")
            params.append(route_type)
        where = " AND ".join(conditions)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT metric_id, route_type, outcome, agent_id, agent_version_key,
                       conversation_id, retrieval_status, searches, media_count,
                       duration_ms, token_count, error_code, created_at
                FROM agent_run_metrics WHERE {where}
                ORDER BY created_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]
