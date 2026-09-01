from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.redaction import redact_text

SAFE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TERMINAL_RUN_STATUSES = frozenset({"diagnosed", "inconclusive", "blocked", "interrupted", "failed"})


class GatewayStoreError(RuntimeError):
    """A durable gateway operation could not be completed."""


class EnrollmentError(GatewayStoreError):
    """An enrollment code is invalid, expired, or already consumed."""


class AuthenticationError(GatewayStoreError):
    """A device token is invalid or revoked."""


class RunNotFoundError(GatewayStoreError):
    """A run does not exist in the authenticated workspace."""


@dataclass(frozen=True, slots=True)
class GatewayDevice:
    device_id: str
    workspace_id: str
    tenant_id: str | None
    name: str
    platform: str


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    device: GatewayDevice
    token: str


class GatewayStore:
    """Single-node durable store with an interface that can later move to PostgreSQL."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def issue_enrollment(
        self,
        *,
        workspace_id: str,
        tenant_id: str | None = None,
        expires_in_seconds: int = 600,
    ) -> str:
        workspace_id = _scope(workspace_id, "workspace_id")
        tenant_id = _optional_scope(tenant_id, "tenant_id")
        if not 60 <= expires_in_seconds <= 86_400:
            raise ValueError("expires_in_seconds must be between 60 and 86400")
        code = "enr_" + secrets.token_urlsafe(32)
        now = _utc_now()
        expires_at = now + timedelta(seconds=expires_in_seconds)
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO enrollment_codes (
                    code_hash, workspace_id, tenant_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (_secret_hash(code), workspace_id, tenant_id, _iso(now), _iso(expires_at)),
            )
        return code

    def redeem_enrollment(self, code: str, *, device_name: str, platform: str) -> EnrollmentResult:
        if not code.startswith("enr_"):
            raise EnrollmentError("invalid enrollment code")
        device_name = _label(device_name, "device_name")
        platform = _label(platform, "platform")
        now = _utc_now()
        code_hash = _secret_hash(code)
        with self._connection(write=True) as connection:
            row = connection.execute(
                """
                SELECT workspace_id, tenant_id, expires_at, used_at
                FROM enrollment_codes WHERE code_hash = ?
                """,
                (code_hash,),
            ).fetchone()
            if row is None or row["used_at"] is not None or _parse_time(row["expires_at"]) <= now:
                raise EnrollmentError("enrollment code is invalid, expired, or already used")
            updated = connection.execute(
                "UPDATE enrollment_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                (_iso(now), code_hash),
            )
            if updated.rowcount != 1:
                raise EnrollmentError("enrollment code was already consumed")
            token = "aops_" + secrets.token_urlsafe(32)
            device = GatewayDevice(
                device_id="dev_" + uuid.uuid4().hex,
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]) if row["tenant_id"] else None,
                name=device_name,
                platform=platform,
            )
            connection.execute(
                """
                INSERT INTO devices (
                    device_id, workspace_id, tenant_id, name, platform, token_hash,
                    created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device.device_id,
                    device.workspace_id,
                    device.tenant_id,
                    device.name,
                    device.platform,
                    _secret_hash(token),
                    _iso(now),
                    _iso(now),
                ),
            )
        return EnrollmentResult(device=device, token=token)

    def authenticate_device(self, token: str) -> GatewayDevice:
        if not token.startswith("aops_"):
            raise AuthenticationError("invalid device token")
        now = _iso(_utc_now())
        with self._connection(write=True) as connection:
            row = connection.execute(
                """
                SELECT device_id, workspace_id, tenant_id, name, platform
                FROM devices
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (_secret_hash(token),),
            ).fetchone()
            if row is None:
                raise AuthenticationError("invalid or revoked device token")
            connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE device_id = ?",
                (now, row["device_id"]),
            )
        return _device_from_row(row)

    def revoke_device(self, device_id: str) -> bool:
        with self._connection(write=True) as connection:
            updated = connection.execute(
                "UPDATE devices SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (_iso(_utc_now()), device_id),
            )
        return updated.rowcount == 1

    def create_run(
        self,
        *,
        run_id: str,
        workspace_id: str,
        incident_id: str,
        problem: str,
        order_no: str,
        tenant_id: str | None,
        key_slot: str,
        provider: str | None,
        fixture_name: str | None,
        created_by_device: str,
    ) -> dict[str, Any]:
        now = _iso(_utc_now())
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, workspace_id, incident_id, problem, order_no, tenant_id,
                    key_slot, provider, fixture_name, status, created_by_device, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    run_id,
                    workspace_id,
                    incident_id,
                    redact_text(problem, preserve=(order_no,)),
                    order_no,
                    tenant_id,
                    key_slot,
                    provider,
                    fixture_name,
                    created_by_device,
                    now,
                    now,
                ),
            )
        return self.get_run(run_id, workspace_id)

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        confidence: str | None = None,
        summary: str | None = None,
        result: dict[str, Any] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = _iso(_utc_now())
        started_at = now if status == "running" else None
        completed_at = now if status in TERMINAL_RUN_STATUSES else None
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE runs SET
                    status = ?, confidence = COALESCE(?, confidence),
                    summary = COALESCE(?, summary), result_json = COALESCE(?, result_json),
                    error_type = COALESCE(?, error_type),
                    error_message = COALESCE(?, error_message),
                    started_at = COALESCE(started_at, ?),
                    completed_at = COALESCE(completed_at, ?), updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    confidence,
                    summary,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_type,
                    error_message,
                    started_at,
                    completed_at,
                    now,
                    run_id,
                ),
            )
            if updated.rowcount != 1:
                raise RunNotFoundError(run_id)

    def append_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        payload.setdefault("at", _iso(_utc_now()))
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            connection.execute(
                """
                INSERT INTO events (run_id, sequence, at, type, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    str(payload["at"]),
                    str(payload.get("type", "runtime_event")),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
        return {"sequence": sequence, **payload}

    def get_run(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ? AND workspace_id = ?",
                (run_id, workspace_id),
            ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return _run_from_row(row)

    def list_runs(self, workspace_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs WHERE workspace_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (workspace_id, limit),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def list_events(
        self,
        run_id: str,
        workspace_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self.get_run(run_id, workspace_id)
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid event pagination")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT sequence, payload_json FROM events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (run_id, after, limit),
            ).fetchall()
        return [{"sequence": int(row["sequence"]), **json.loads(str(row["payload_json"]))} for row in rows]

    def list_devices(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT device_id, workspace_id, tenant_id, name, platform, created_at, "
            "last_seen_at, revoked_at FROM devices"
        )
        params: tuple[str, ...] = ()
        if workspace_id:
            sql += " WHERE workspace_id = ?"
            params = (workspace_id,)
        sql += " ORDER BY created_at DESC"
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS enrollment_codes (
                    code_hash TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    tenant_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    tenant_id TEXT,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    problem TEXT NOT NULL,
                    order_no TEXT NOT NULL,
                    tenant_id TEXT,
                    key_slot TEXT NOT NULL,
                    provider TEXT,
                    fixture_name TEXT,
                    status TEXT NOT NULL,
                    confidence TEXT,
                    summary TEXT,
                    result_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    created_by_device TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY(created_by_device) REFERENCES devices(device_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runs_workspace_created
                    ON runs(workspace_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    at TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
            if "error_message" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN error_message TEXT")
            if "provider" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN provider TEXT")
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


def _device_from_row(row: sqlite3.Row) -> GatewayDevice:
    return GatewayDevice(
        device_id=str(row["device_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]) if row["tenant_id"] else None,
        name=str(row["name"]),
        platform=str(row["platform"]),
    )


def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_result = result.pop("result_json", None)
    result["result"] = json.loads(raw_result) if raw_result else None
    return result


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(value: str, name: str) -> str:
    candidate = value.strip()
    if not SAFE_SCOPE.fullmatch(candidate):
        raise ValueError(f"{name} contains unsupported characters")
    return candidate


def _optional_scope(value: str | None, name: str) -> str | None:
    return _scope(value, name) if value and value.strip() else None


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
