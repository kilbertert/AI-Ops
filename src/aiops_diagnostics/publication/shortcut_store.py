"""快捷方式（Shortcut）SQLite 持久化存储。

只负责 ``shortcuts`` 与 ``shortcut_versions`` 表的读写、租户生效视图归并及行映射；
通过 ``gateway_db.connection`` 遵循统一锁纪律，不依赖 ``shortcut_lifecycle``。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    protect_private_file,
)
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.gateway import gateway_db, gateway_schema
from aiops_diagnostics.publication.shortcut_models import (
    _BUNDLED_SHORTCUTS,
    BUTTON_KIND,
    PLATFORM_SCOPE,
    SHORTCUT_KINDS,
    SHORTCUT_PLATFORM_SCOPE,
    TENANT_SCOPE,
    Shortcut,
    ShortcutConflict,
    ShortcutNotFound,
    ShortcutValidationError,
    ShortcutVersion,
    _actor,
    _json,
)


class ShortcutStore:
    """SQLite persistence for shortcuts sharing the gateway database file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def seed_bundled(self, context: Any, manager: Any) -> list[Shortcut]:
        """Create the initial seed shortcuts if absent (#230, #588).

        Idempotent: existing rows in this tenant+entry keep their lifecycle
        state (an operator may have disabled them deliberately). The seeded
        rows are drafts — publishing is an explicit operator act per the
        PRD's governance boundary, never automatic.

        The seed is not only buttons: the consumer entry also carries the
        chat-page banner. Which surface a row belongs to is `kind`, taken from
        the seed spec — never inferred from which fields happen to be set.
        """
        del manager
        seeded: list[Shortcut] = []
        tenant_id = context.effective_tenant_id
        for entry, fields in _BUNDLED_SHORTCUTS:
            for code, spec in fields.items():
                if self.find_by_code(tenant_id, entry, code) is not None:
                    continue
                seeded.append(
                    self.create(
                        tenant_id,
                        entry,
                        code,
                        intent=spec["intent"],
                        requires_order=spec["requires_order"],
                        sort_order=spec["sort_order"],
                        labels=spec["labels"],
                        descriptions=spec["descriptions"],
                        question_templates=spec.get("question_templates") or {},
                        target_agent_version=None,
                        jump_path=spec.get("jump_path"),
                        kind=spec.get("kind", BUTTON_KIND),
                        image_url=spec.get("image_url"),
                        created_by=_actor(context),
                    )
                )
        return seeded

    def create(
        self,
        tenant_id: str,
        business_entry: str,
        code: str,
        *,
        intent: str,
        requires_order: bool,
        sort_order: int,
        labels: dict[str, str],
        descriptions: dict[str, str],
        question_templates: dict[str, str],
        target_agent_version: str | None,
        jump_path: str | None = None,
        kind: str = BUTTON_KIND,
        image_url: str | None = None,
        created_by: str,
    ) -> Shortcut:
        shortcut_id = "sct_" + uuid.uuid4().hex
        now = format_stored_time(now_utc())
        payload = {
            "labels": labels,
            "descriptions": descriptions,
            "question_templates": question_templates,
            "target_agent_version": target_agent_version,
            "jump_path": jump_path,
        }
        with self._connection(write=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO shortcuts (
                        shortcut_id, tenant_id, business_entry, code, intent,
                        requires_order, sort_order, status, revision, fields_json,
                        published_version, kind, image_url, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        shortcut_id,
                        tenant_id,
                        business_entry,
                        code,
                        intent,
                        int(requires_order),
                        sort_order,
                        _json(payload),
                        kind,
                        image_url,
                        created_by,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ShortcutConflict(
                    f"shortcut code already exists in this tenant and entry: {code}"
                ) from exc
        return self.get(shortcut_id, tenant_id)

    def get(self, shortcut_id: str, tenant_id: str) -> Shortcut:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM shortcuts WHERE shortcut_id = ? AND tenant_id = ?",
                (shortcut_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ShortcutNotFound("shortcut not found")
        return _shortcut_from_row(row)

    def find_by_code(self, tenant_id: str, business_entry: str, code: str) -> Shortcut | None:
        """Locator for tests/imports; the HTTP listing uses list_published."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE tenant_id = ? AND business_entry = ? AND code = ?
                """,
                (tenant_id, business_entry, code),
            ).fetchone()
        return _shortcut_from_row(row) if row is not None else None

    def list_all(self, tenant_id: str, business_entry: str) -> list[Shortcut]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE tenant_id = ? AND business_entry = ?
                ORDER BY sort_order, code
                """,
                (tenant_id, business_entry),
            ).fetchall()
        return [_shortcut_from_row(row) for row in rows]

    def list_published(self, tenant_id: str, business_entry: str) -> list[Shortcut]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE tenant_id = ? AND business_entry = ? AND status = 'published'
                ORDER BY sort_order, code
                """,
                (tenant_id, business_entry),
            ).fetchall()
        return [_shortcut_from_row(row) for row in rows]

    def list_published_for_entry(self, business_entry: str) -> list[Shortcut]:
        """List published tenant rows for migration/admin inspection only."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE business_entry = ? AND status = 'published' AND tenant_id != ?
                ORDER BY business_entry, code, created_at, tenant_id
                """,
                (business_entry, SHORTCUT_PLATFORM_SCOPE),
            ).fetchall()
        return [_shortcut_from_row(row) for row in rows]

    def list_effective(
        self, tenant_id: str, business_entry: str, *, kind: str | None = None
    ) -> list[Shortcut]:
        """Resolve the published platform defaults and tenant rows.

        A published tenant row replaces the platform row with the same code.
        A disabled tenant row suppresses that code; drafts do not affect the
        effective result. The merge is the single seam shared by listing and
        shortcut execution.

        ``kind`` narrows the result to one surface (``button`` / ``banner``).
        The split is enforced HERE rather than in a response filter, so a
        banner row cannot reach the button listing by any route — including
        through the merge, where a tenant override could otherwise change a
        row's surface.
        """
        if tenant_id == SHORTCUT_PLATFORM_SCOPE:
            raise ShortcutValidationError("tenant id is reserved")
        if kind is not None and kind not in SHORTCUT_KINDS:
            raise ShortcutValidationError("kind is invalid")
        platform_rows = self.list_published(SHORTCUT_PLATFORM_SCOPE, business_entry)
        tenant_rows = self.list_all(tenant_id, business_entry)
        effective = {row.code: row for row in platform_rows}
        for row in tenant_rows:
            if row.status == "published":
                effective[row.code] = row
            elif row.status == "disabled":
                effective.pop(row.code, None)
        rows = [row for row in effective.values() if kind is None or row.kind == kind]
        return sorted(rows, key=lambda row: (row.sort_order, row.code))

    def find_effective_by_code(self, tenant_id: str, business_entry: str, code: str) -> Shortcut | None:
        return next((row for row in self.list_effective(tenant_id, business_entry) if row.code == code), None)

    def update(
        self,
        shortcut_id: str,
        tenant_id: str,
        expected_revision: int,
        *,
        intent: str,
        requires_order: bool,
        sort_order: int,
        labels: dict[str, str],
        descriptions: dict[str, str],
        question_templates: dict[str, str],
        target_agent_version: str | None,
        jump_path: str | None = None,
        image_url: str | None = None,
    ) -> Shortcut:
        # `kind` is deliberately NOT a parameter: a row does not become a
        # different kind of resource. Letting an edit move a row between
        # surfaces would make "which endpoint serves this" mutable config,
        # which is the implicit-input shape the explicit column exists to end.
        now = format_stored_time(now_utc())
        payload = {
            "labels": labels,
            "descriptions": descriptions,
            "question_templates": question_templates,
            "target_agent_version": target_agent_version,
            "jump_path": jump_path,
        }
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts
                SET intent = ?, requires_order = ?, sort_order = ?, fields_json = ?,
                    image_url = ?, revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'draft' AND revision = ?
                """,
                (
                    intent,
                    int(requires_order),
                    sort_order,
                    _json(payload),
                    image_url,
                    now,
                    shortcut_id,
                    tenant_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def fork_draft(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> Shortcut:
        now = format_stored_time(now_utc())
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts SET status = 'draft', revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'published' AND revision = ?
                """,
                (now, shortcut_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def publish(
        self,
        shortcut_id: str,
        tenant_id: str,
        expected_revision: int,
        snapshot: dict[str, Any],
        published_by: str,
    ) -> ShortcutVersion:
        now = format_stored_time(now_utc())
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT status, revision FROM shortcuts WHERE shortcut_id = ? AND tenant_id = ?",
                (shortcut_id, tenant_id),
            ).fetchone()
            if row is None:
                raise ShortcutNotFound("shortcut not found")
            if row["status"] != "draft" or int(row["revision"]) != expected_revision:
                raise ShortcutConflict("shortcut revision or state changed")
            current = connection.execute(
                """
                SELECT COALESCE(MAX(version_no), 0) AS version_no
                FROM shortcut_versions WHERE shortcut_id = ?
                """,
                (shortcut_id,),
            ).fetchone()
            version_no = int(current["version_no"]) + 1
            connection.execute(
                """
                INSERT INTO shortcut_versions
                    (shortcut_id, tenant_id, version_no, snapshot_json, published_by, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (shortcut_id, tenant_id, version_no, _json(snapshot), published_by, now),
            )
            connection.execute(
                """
                UPDATE shortcuts
                SET status = 'published', published_version = ?, revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND revision = ?
                """,
                (version_no, now, shortcut_id, tenant_id, expected_revision),
            )
        return ShortcutVersion(shortcut_id, tenant_id, version_no, snapshot, published_by, now)

    def disable(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> Shortcut:
        now = format_stored_time(now_utc())
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts SET status = 'disabled', revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'published' AND revision = ?
                """,
                (now, shortcut_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def enable(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> Shortcut:
        now = format_stored_time(now_utc())
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts SET status = 'published', revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'disabled' AND revision = ?
                """,
                (now, shortcut_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def delete_draft(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> None:
        with self._connection(write=True) as connection:
            deleted = connection.execute(
                """
                DELETE FROM shortcuts
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'draft'
                  AND published_version IS NULL AND revision = ?
                """,
                (shortcut_id, tenant_id, expected_revision),
            )
            if deleted.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)

    def version(self, shortcut_id: str, tenant_id: str, version_no: int) -> ShortcutVersion:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM shortcut_versions
                WHERE shortcut_id = ? AND tenant_id = ? AND version_no = ?
                """,
                (shortcut_id, tenant_id, version_no),
            ).fetchone()
        if row is None:
            raise ShortcutNotFound("shortcut version not found")
        return _version_from_row(row)

    @staticmethod
    def _raise_update_error(
        connection: sqlite3.Connection, shortcut_id: str, tenant_id: str, revision: int
    ) -> None:
        row = connection.execute(
            "SELECT revision FROM shortcuts WHERE shortcut_id = ? AND tenant_id = ?",
            (shortcut_id, tenant_id),
        ).fetchone()
        if row is None:
            raise ShortcutNotFound("shortcut not found")
        raise ShortcutConflict(f"shortcut revision conflict: expected {revision}")

    def _initialize(self) -> None:
        gateway_schema.ensure(self.path)
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False):
        # 连接与锁纪律唯一渲染在 gateway_db（#573）；本 store 走 LEASE profile。
        with gateway_db.connection(self.path, write=write) as connection:
            yield connection


def _shortcut_from_row(row: sqlite3.Row) -> Shortcut:
    fields = json.loads(str(row["fields_json"]))
    return Shortcut(
        shortcut_id=str(row["shortcut_id"]),
        tenant_id=str(row["tenant_id"]),
        business_entry=str(row["business_entry"]),
        code=str(row["code"]),
        intent=str(row["intent"]),
        requires_order=bool(row["requires_order"]),
        sort_order=int(row["sort_order"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        labels={str(k): str(v) for k, v in dict(fields.get("labels") or {}).items()},
        descriptions={str(k): str(v) for k, v in dict(fields.get("descriptions") or {}).items()},
        question_templates={str(k): str(v) for k, v in dict(fields.get("question_templates") or {}).items()},
        target_agent_version=fields.get("target_agent_version"),
        jump_path=fields.get("jump_path") or None,
        kind=str(row["kind"]) if row["kind"] is not None else BUTTON_KIND,
        image_url=str(row["image_url"]) if row["image_url"] is not None else None,
        published_version=int(row["published_version"]) if row["published_version"] is not None else None,
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        scope=PLATFORM_SCOPE if str(row["tenant_id"]) == SHORTCUT_PLATFORM_SCOPE else TENANT_SCOPE,
    )


def _version_from_row(row: sqlite3.Row) -> ShortcutVersion:
    return ShortcutVersion(
        shortcut_id=str(row["shortcut_id"]),
        tenant_id=str(row["tenant_id"]),
        version_no=int(row["version_no"]),
        snapshot=json.loads(str(row["snapshot_json"])),
        published_by=str(row["published_by"]),
        published_at=str(row["published_at"]),
    )
