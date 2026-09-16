"""Platform and tenant shortcut drafts with immutable published versions (#230/#244).

Independent product-entry resource — NOT the Agent ``quick_commands`` model.
Tenant identity is ``(tenant_id, business_entry, code)``; platform defaults use
an internal platform scope. The lifecycle mirrors
agent_lifecycle (draft -> published -> disabled) so operators already know
the semantics, but the store, permission scope, and version snapshots are
separate. Editing a draft never mutates a snapshot an in-flight client may
already be rendering.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_lifecycle import SAFE_ID
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file

# Promotional Agent/version reference — same shape as ConversationCreateRequest's
# agent_version_key ("agt_<hex>#vN"), so a shortcut target always resolves to
# one immutable published agent version.
_AGENT_VERSION = re.compile(r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$")

SHORTCUT_MANAGE_SCOPE = "aiops:shortcuts:manage"
SHORTCUT_INTENTS = frozenset(
    {"knowledge", "casual", "order_issue", "report_fault", "case_exploration", "solution_discovery"}
)
SHORTCUT_STATUSES = frozenset({"draft", "published", "disabled"})
PLATFORM_SCOPE = "platform"
TENANT_SCOPE = "tenant"
PLATFORM_TENANT_ID = "__platform__"
SHORTCUT_SCOPES = frozenset({PLATFORM_SCOPE, TENANT_SCOPE})
PLATFORM_ROLES = frozenset({"ROLE_PLATFORM_ADMIN"})
# Stable codes the frontend may hard-wire behavior to (order picker etc.).
STABLE_CODES = frozenset({"case_exploration", "smart_diagnosis", "report_fault"})

# Product default for the fault-reporting jump action. The path is an
# in-app route, so it is not localized: one path per action, not per language.
REPORT_FAULT_JUMP_PATH = "/charge/pages/faultReport/faultReportList"

VIEW_ROLES = frozenset(
    {"ROLE_AGENT_VIEWER", "ROLE_AGENT_ADMIN", "ROLE_AGENT_PUBLISHER", "ROLE_PLATFORM_ADMIN"}
)
EDIT_ROLES = frozenset({"ROLE_AGENT_ADMIN", "ROLE_PLATFORM_ADMIN"})
PUBLISH_ROLES = frozenset({"ROLE_AGENT_PUBLISHER", "ROLE_AGENT_ADMIN", "ROLE_PLATFORM_ADMIN"})


class ShortcutError(RuntimeError):
    """Base class for shortcut lifecycle failures."""

    code = "SHORTCUT_ERROR"


# The initial product-entry shortcuts (#230 initial codes). Copy lives here
# only as the seed default; after creation the rows are ordinary managed
# resources — operators edit/publish/disable them through the API.
_BUNDLED_SHORTCUTS: tuple[tuple[str, dict[str, dict[str, Any]]], ...] = (
    (
        "consumer",
        {
            "case_exploration": {
                "intent": "case_exploration",
                "requires_order": False,
                "sort_order": 10,
                "labels": {"zh": "客户案例", "en": "Customer Cases"},
                "descriptions": {
                    "zh": "查看不同行业的充电运营标杆案例",
                    "en": "Explore charging-operation benchmark cases by industry",
                },
                "question_templates": {
                    "zh": "我想看看客户案例",
                    "en": "I'd like to see customer cases",
                },
            },
            "smart_diagnosis": {
                "intent": "order_issue",
                "requires_order": True,
                "sort_order": 20,
                "labels": {"zh": "智能检测", "en": "Smart Diagnosis"},
                "descriptions": {
                    "zh": "选择订单后自动诊断充电异常",
                    "en": "Pick an order, then diagnose the charging issue automatically",
                },
                "question_templates": {
                    "zh": "帮我检测这个订单的充电异常",
                    "en": "Diagnose the charging issue of this order",
                },
            },
            "report_fault": {
                "intent": "report_fault",
                "requires_order": False,
                "sort_order": 30,
                "labels": {"zh": "故障上报", "en": "Report a Fault"},
                "descriptions": {
                    "zh": "描述故障现象，由平台跟进处理",
                    "en": "Describe the fault and the platform will follow up",
                },
                "question_templates": {
                    "zh": "我要上报一个故障",
                    "en": "I want to report a fault",
                },
                # The product's default fault-reporting entry is the in-app
                # form, not a preset prompt. Kept here as the versioned
                # definition so the path is a repo asset, not a magic string
                # in an ad-hoc migration command.
                "jump_path": REPORT_FAULT_JUMP_PATH,
            },
        },
    ),
)


class ShortcutNotFound(ShortcutError):
    code = "SHORTCUT_NOT_FOUND"


class ShortcutForbidden(ShortcutError):
    code = "SHORTCUT_FORBIDDEN"


class ShortcutConflict(ShortcutError):
    code = "SHORTCUT_REVISION_CONFLICT"


class ShortcutValidationError(ShortcutError):
    code = "SHORTCUT_VALIDATION_FAILED"


@dataclass(frozen=True, slots=True)
class Shortcut:
    shortcut_id: str
    tenant_id: str
    business_entry: str
    code: str
    intent: str
    requires_order: bool
    sort_order: int
    status: str
    revision: int
    labels: dict[str, str]  # language -> label
    descriptions: dict[str, str]
    question_templates: dict[str, str]
    target_agent_version: str | None  # "agt_xxx#vN" for promotional targets
    # In-app route the client navigates to on click. A non-empty path IS the
    # discriminator: it makes this a jump action, which never reaches the
    # unified assistant entry. None keeps the prompt action behavior.
    jump_path: str | None
    published_version: int | None
    created_by: str
    created_at: str
    updated_at: str
    scope: str = TENANT_SCOPE

    def public(self, language: str) -> dict[str, Any]:
        """Public listing shape: stable code + localized text for ONE language.

        Falls back to zh for a missing language (repo i18n rule: the catalog
        never returns empty copy). Status is always ``published`` here — the
        listing endpoint only serves published rows (#230 acceptance).
        """
        return {
            "code": self.code,
            "intent": self.intent,
            "requires_order": self.requires_order,
            "sort_order": self.sort_order,
            "label": self.labels.get(language) or self.labels.get("zh", ""),
            "description": self.descriptions.get(language) or self.descriptions.get("zh", ""),
            "question_template": (
                self.question_templates.get(language) or self.question_templates.get("zh", "")
            ),
            "target_agent_version": self.target_agent_version,
            "jump_path": self.jump_path,
        }

    def to_dict(self) -> dict[str, Any]:
        """Full management shape (admin surface only)."""
        return {
            "shortcut_id": self.shortcut_id,
            "code": self.code,
            "business_entry": self.business_entry,
            "intent": self.intent,
            "requires_order": self.requires_order,
            "sort_order": self.sort_order,
            "status": self.status,
            "revision": self.revision,
            "labels": dict(self.labels),
            "descriptions": dict(self.descriptions),
            "question_templates": dict(self.question_templates),
            "target_agent_version": self.target_agent_version,
            "jump_path": self.jump_path,
            "published_version": self.published_version,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class ShortcutVersion:
    shortcut_id: str
    tenant_id: str
    version_no: int
    snapshot: dict[str, Any]
    published_by: str
    published_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shortcut_id": self.shortcut_id,
            "version_no": self.version_no,
            "snapshot": json.loads(json.dumps(self.snapshot, ensure_ascii=False)),
            "published_by": self.published_by,
            "published_at": self.published_at,
        }


class ShortcutStore:
    """SQLite persistence for shortcuts sharing the gateway database file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def seed_bundled(self, context: Any, manager: ShortcutManager) -> list[Shortcut]:
        """Create the three initial stable-code shortcuts if absent (#230).

        Idempotent: existing rows in this tenant+entry keep their lifecycle
        state (an operator may have disabled them deliberately). The seeded
        rows are drafts — publishing is an explicit operator act per the
        PRD's governance boundary, never automatic.
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
                        question_templates=spec["question_templates"],
                        target_agent_version=None,
                        jump_path=spec.get("jump_path"),
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
        created_by: str,
    ) -> Shortcut:
        shortcut_id = "sct_" + uuid.uuid4().hex
        now = _iso(datetime.now(UTC))
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
                        published_version, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, NULL, ?, ?, ?)
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
                (business_entry, PLATFORM_TENANT_ID),
            ).fetchall()
        return [_shortcut_from_row(row) for row in rows]

    def list_effective(self, tenant_id: str, business_entry: str) -> list[Shortcut]:
        """Resolve the published platform defaults and tenant rows.

        A published tenant row replaces the platform row with the same code.
        A disabled tenant row suppresses that code; drafts do not affect the
        effective result. The merge is the single seam shared by listing and
        shortcut execution.
        """
        if tenant_id == PLATFORM_TENANT_ID:
            raise ShortcutValidationError("tenant id is reserved")
        platform_rows = self.list_published(PLATFORM_TENANT_ID, business_entry)
        tenant_rows = self.list_all(tenant_id, business_entry)
        effective = {row.code: row for row in platform_rows}
        for row in tenant_rows:
            if row.status == "published":
                effective[row.code] = row
            elif row.status == "disabled":
                effective.pop(row.code, None)
        return sorted(effective.values(), key=lambda row: (row.sort_order, row.code))

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
    ) -> Shortcut:
        now = _iso(datetime.now(UTC))
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
                    revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'draft' AND revision = ?
                """,
                (
                    intent,
                    int(requires_order),
                    sort_order,
                    _json(payload),
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
        now = _iso(datetime.now(UTC))
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
        now = _iso(datetime.now(UTC))
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
        now = _iso(datetime.now(UTC))
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
        now = _iso(datetime.now(UTC))
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
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS shortcuts (
                    shortcut_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    business_entry TEXT NOT NULL,
                    code TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    requires_order INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    fields_json TEXT NOT NULL,
                    published_version INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, business_entry, code)
                );
                CREATE INDEX IF NOT EXISTS idx_shortcuts_listing
                    ON shortcuts(tenant_id, business_entry, status, sort_order);
                CREATE TABLE IF NOT EXISTS shortcut_versions (
                    shortcut_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    PRIMARY KEY(shortcut_id, version_no),
                    FOREIGN KEY(shortcut_id) REFERENCES shortcuts(shortcut_id) ON DELETE CASCADE
                );
                """
            )
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False):
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


class ShortcutManager:
    def __init__(self, store: ShortcutStore) -> None:
        self.store = store

    def create(self, context: Any, payload: dict[str, Any], *, scope: str = TENANT_SCOPE) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        fields = self._validated_fields(payload, for_publish=False)
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        return self.store.create(
            tenant_id,
            _entry(payload.get("business_entry")),
            _code(payload.get("code")),
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
            created_by=_actor(context),
        )

    def list(self, context: Any, *, business_entry: str | None, scope: str = TENANT_SCOPE) -> list[Shortcut]:
        """Management listing (all statuses) for the caller's tenant+entry."""
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        return self.store.list_all(tenant_id, _entry(business_entry))

    def list_published(self, context: Any, *, business_entry: str) -> list[Shortcut]:
        """Public listing — authenticated callers, roles NOT required (#230).

        Any caller that passed the endpoint's auth scope may read the entry's
        published shortcuts (they render the product home). Only drafts,
        disabled rows, other tenants, and other entries are withheld.
        """
        entry = (business_entry or "").strip().lower()
        if entry not in {"consumer", "operator"}:
            raise ShortcutValidationError("business_entry is invalid")
        return self.store.list_published(context.effective_tenant_id, entry)

    def list_effective(self, context: Any, *, business_entry: str) -> list[Shortcut]:
        entry = (business_entry or "").strip().lower()
        if entry not in {"consumer", "operator"}:
            raise ShortcutValidationError("business_entry is invalid")
        return self.store.list_effective(context.effective_tenant_id, entry)

    def get(self, context: Any, shortcut_id: str, *, scope: str = TENANT_SCOPE) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        return self.store.get(_id(shortcut_id), tenant_id)

    def update(
        self, context: Any, shortcut_id: str, payload: dict[str, Any], *, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        # Absent ``jump_path`` means "leave it alone", not "clear it". The HTTP
        # request model defaults the field to None, so an unchanged edit would
        # otherwise silently demote a jump action to a prompt action — turning
        # the one field that discriminates the two into collateral damage of an
        # unrelated label edit. Explicit null clears it; both spellings go
        # through the single _jump_path validator so there is one normalizer.
        current = self.store.get(_id(shortcut_id), tenant_id)
        fields = self._validated_fields(
            {
                **payload,
                "jump_path": payload.get("jump_path") or current.jump_path,
            },
            for_publish=False,
        )
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        return self.store.update(
            _id(shortcut_id),
            tenant_id,
            int(payload.get("expected_revision") or 0),
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
        )

    def publish(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, PUBLISH_ROLES)
        shortcut = self.store.get(_id(shortcut_id), tenant_id)
        if shortcut.revision != expected_revision or shortcut.status != "draft":
            raise ShortcutConflict("shortcut revision or state changed")
        snapshot = shortcut.to_dict()
        return self.store.publish(
            shortcut.shortcut_id,
            tenant_id,
            expected_revision,
            snapshot,
            _actor(context),
        )

    def fork_draft(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        return self.store.fork_draft(_id(shortcut_id), tenant_id, expected_revision)

    def suppress(self, context: Any, *, business_entry: str, code: str) -> Shortcut:
        """Publish a tenant-only disabled override for a platform action."""
        tenant_id = self._scope_tenant(context, TENANT_SCOPE, PUBLISH_ROLES)
        entry = _entry(business_entry)
        stable_code = _code(code)
        existing = self.store.find_by_code(tenant_id, entry, stable_code)
        if existing is not None:
            if existing.status == "disabled":
                return existing
            if existing.status != "published":
                raise ShortcutConflict("shortcut override is not published")
            return self.store.disable(existing.shortcut_id, tenant_id, existing.revision)
        platform = self.store.find_by_code(PLATFORM_TENANT_ID, entry, stable_code)
        if platform is None or platform.status != "published":
            raise ShortcutNotFound("platform shortcut not found")
        created = self.store.create(
            tenant_id,
            entry,
            stable_code,
            intent=platform.intent,
            requires_order=platform.requires_order,
            sort_order=platform.sort_order,
            labels=platform.labels,
            descriptions=platform.descriptions,
            question_templates=platform.question_templates,
            target_agent_version=None,
            jump_path=platform.jump_path,
            created_by=_actor(context),
        )
        self.publish(context, created.shortcut_id, expected_revision=created.revision)
        published = self.store.get(created.shortcut_id, tenant_id)
        return self.store.disable(published.shortcut_id, tenant_id, published.revision)

    def restore(self, context: Any, *, business_entry: str, code: str) -> Shortcut:
        """Restore the previous tenant override after a tenant suppression."""
        tenant_id = self._scope_tenant(context, TENANT_SCOPE, PUBLISH_ROLES)
        existing = self.store.find_by_code(tenant_id, _entry(business_entry), _code(code))
        if existing is None:
            raise ShortcutNotFound("tenant shortcut override not found")
        if existing.status != "disabled":
            return existing
        return self.store.enable(existing.shortcut_id, tenant_id, existing.revision)

    def rollback(
        self,
        context: Any,
        shortcut_id: str,
        *,
        version_no: int,
        expected_revision: int,
        scope: str = TENANT_SCOPE,
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        if version_no < 1:
            raise ShortcutValidationError("version_no must be positive")
        current = self.store.get(_id(shortcut_id), tenant_id)
        if current.status != "published" or current.revision != expected_revision:
            raise ShortcutConflict("shortcut revision or state changed")
        target = self.store.version(current.shortcut_id, tenant_id, version_no)
        fields = self._validated_fields(target.snapshot, for_publish=False)
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        draft = self.store.fork_draft(current.shortcut_id, tenant_id, expected_revision)
        updated = self.store.update(
            draft.shortcut_id,
            tenant_id,
            draft.revision,
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
        )
        return self.publish(context, updated.shortcut_id, expected_revision=updated.revision, scope=scope)

    def disable(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, PUBLISH_ROLES)
        return self.store.disable(_id(shortcut_id), tenant_id, expected_revision)

    def delete(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> None:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        self.store.delete_draft(_id(shortcut_id), tenant_id, expected_revision)

    def version(
        self, context: Any, shortcut_id: str, version_no: int, *, scope: str = TENANT_SCOPE
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        if version_no < 1:
            raise ShortcutValidationError("version_no must be positive")
        return self.store.version(_id(shortcut_id), tenant_id, version_no)

    @staticmethod
    def _scope_tenant(context: Any, scope: str, roles: frozenset[str]) -> str:
        if scope not in SHORTCUT_SCOPES:
            raise ShortcutValidationError("scope is invalid")
        if scope == PLATFORM_SCOPE:
            ShortcutManager._require(context, roles | PLATFORM_ROLES)
            if not frozenset(getattr(context, "roles", ())).intersection(PLATFORM_ROLES):
                raise ShortcutForbidden("platform shortcut access is not permitted")
            return PLATFORM_TENANT_ID
        ShortcutManager._require(context, roles)
        if getattr(context, "effective_tenant_id", "") == PLATFORM_TENANT_ID:
            raise ShortcutValidationError("tenant id is reserved")
        return context.effective_tenant_id

    @staticmethod
    def _require(context: Any, roles: frozenset[str]) -> None:
        effective_tenant = getattr(context, "effective_tenant_id", "")
        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_tenant or not effective_roles.intersection(roles):
            raise ShortcutForbidden("shortcut access is not permitted")

    def _validated_fields(self, payload: dict[str, Any], *, for_publish: bool) -> dict[str, Any]:
        del for_publish  # publish-time validation equals create/update: same rules
        intent = str(payload.get("intent") or "")
        if intent not in SHORTCUT_INTENTS:
            raise ShortcutValidationError("intent is invalid")
        requires_order = bool(payload.get("requires_order", False))
        if intent in {"order_issue", "smart_diagnosis"} and not requires_order:
            # smart_diagnosis / any order_issue shortcut must force the picker.
            raise ShortcutValidationError("order-related shortcuts must set requires_order")
        sort_order = payload.get("sort_order", 100)
        if not isinstance(sort_order, int) or not 0 <= sort_order <= 9999:
            raise ShortcutValidationError("sort_order is invalid")
        labels = _localized(payload.get("labels"), "labels", require_zh=True)
        descriptions = _localized(payload.get("descriptions"), "descriptions", require_zh=False)
        question_templates = _localized(
            payload.get("question_templates"), "question_templates", require_zh=False
        )
        jump_path = _jump_path(payload.get("jump_path"))
        target = payload.get("target_agent_version")
        if target is not None:
            if not isinstance(target, str) or not _AGENT_VERSION.fullmatch(target):
                raise ShortcutValidationError("target_agent_version is invalid")
            if intent not in {"case_exploration", "solution_discovery"}:
                raise ShortcutValidationError("target_agent_version is only allowed for promotional intents")
        if jump_path is not None and target is not None:
            # A jump action never reaches the agent, so a pin would be dead
            # config with a live side effect: the pin is what marks an agent
            # promotional, so it would keep excluding that agent from
            # customer-agent selection for a response nobody ever fetches.
            #
            # Rejected only for NEW configuration. An old published version that
            # carries both must still be rollback-able, so this is enforced in
            # _reject_new_conflicts rather than here.
            raise ShortcutValidationError("jump_path and target_agent_version are mutually exclusive")
        return {
            "intent": intent,
            "requires_order": requires_order,
            "sort_order": sort_order,
            "labels": labels,
            "descriptions": descriptions,
            "question_templates": question_templates,
            "target_agent_version": target,
            "jump_path": jump_path,
        }


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
        published_version=int(row["published_version"]) if row["published_version"] is not None else None,
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        scope=PLATFORM_SCOPE if str(row["tenant_id"]) == PLATFORM_TENANT_ID else TENANT_SCOPE,
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


_JUMP_PATH_MAX = 512


def _jump_path(value: Any) -> str | None:
    """Validate the optional in-app route for a jump action.

    Only the format the product states is enforced (must start with ``/``).
    There is deliberately NO route allowlist: the repository holds no
    authoritative H5 route convention, so a whitelist here would copy the
    client's router into the backend and need a backend release per new page.
    Whether the page exists is the client's acceptance scope.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShortcutValidationError("jump_path is invalid")
    candidate = value.strip()
    if not candidate:
        return None
    if not candidate.startswith("/") or len(candidate) > _JUMP_PATH_MAX:
        raise ShortcutValidationError(
            f"jump_path must start with '/' and be at most {_JUMP_PATH_MAX} characters"
        )
    return candidate


def _localized(value: Any, name: str, *, require_zh: bool) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ShortcutValidationError(f"{name} is invalid")
    result: dict[str, str] = {}
    for language, text in value.items():
        if language not in SUPPORTED_LANGUAGES:
            raise ShortcutValidationError(f"{name} uses an unsupported language")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ShortcutValidationError(f"{name} text is invalid")
        result[str(language)] = text
    if require_zh and not result.get("zh", "").strip():
        raise ShortcutValidationError(f"{name} must include zh copy")
    return result


def _entry(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if candidate not in {"consumer", "operator"}:
        raise ShortcutValidationError("business_entry is invalid")
    return candidate


def _code(value: Any) -> str:
    candidate = str(value or "").strip()
    if not SAFE_ID.fullmatch(candidate) or len(candidate) > 64:
        raise ShortcutValidationError("code is invalid")
    return candidate


def _id(value: str) -> str:
    candidate = (value or "").strip()
    if not SAFE_ID.fullmatch(candidate):
        raise ShortcutValidationError("shortcut_id is invalid")
    return candidate


def _actor(context: Any) -> str:
    caller = getattr(context, "caller", None)
    return str(getattr(caller, "b_user_id", "shortcut-admin"))[:128]


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
