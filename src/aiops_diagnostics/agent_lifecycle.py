"""Tenant-scoped agent drafts and immutable published versions.

The lifecycle is deliberately independent from the model runtime.  A published
snapshot is data consumed by later QA/BFF work; editing a draft never mutates a
snapshot that an in-flight turn may already be using.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file

AGENT_MANAGE_SCOPE = "aiops:agents:manage"
AGENT_TYPES = frozenset({"customer", "operations"})
AGENT_STATUSES = frozenset({"draft", "published", "disabled"})
OUTPUT_CONTRACTS = frozenset({"blocks-v1", "diagnosis-v1"})
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

VIEW_ROLES = frozenset(
    {"ROLE_AGENT_VIEWER", "ROLE_AGENT_ADMIN", "ROLE_AGENT_PUBLISHER", "ROLE_PLATFORM_ADMIN"}
)
EDIT_ROLES = frozenset({"ROLE_AGENT_ADMIN", "ROLE_PLATFORM_ADMIN"})
PUBLISH_ROLES = frozenset({"ROLE_AGENT_PUBLISHER", "ROLE_AGENT_ADMIN", "ROLE_PLATFORM_ADMIN"})


class AgentError(RuntimeError):
    """Base class for lifecycle failures."""

    code = "AGENT_ERROR"


class AgentNotFound(AgentError):
    code = "AGENT_NOT_FOUND"


class AgentForbidden(AgentError):
    code = "AGENT_FORBIDDEN"


class AgentConflict(AgentError):
    code = "AGENT_REVISION_CONFLICT"


class AgentValidationError(AgentError):
    code = "AGENT_VALIDATION_FAILED"


class AgentPublishError(AgentValidationError):
    code = "AGENT_PUBLISH_REJECTED"


class KnowledgeBindingResolver(Protocol):
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None: ...


class UnavailableKnowledgeBindingResolver:
    """Fail closed until the real knowledge service is injected by integration work."""

    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id
        if knowledge_base_ids:
            raise AgentPublishError("knowledge-base validation is unavailable")


@dataclass(frozen=True, slots=True)
class AgentConfig:
    agent_type: str
    prompt: str
    knowledge_base_ids: tuple[str, ...]
    model: str
    output_contract: str
    opening_questions: tuple[str, ...] = ()
    quick_commands: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "prompt": self.prompt,
            "knowledge_base_ids": list(self.knowledge_base_ids),
            "model": self.model,
            "output_contract": self.output_contract,
            "opening_questions": list(self.opening_questions),
            "quick_commands": list(self.quick_commands),
        }


@dataclass(frozen=True, slots=True)
class Agent:
    agent_id: str
    tenant_id: str
    name: str
    description: str
    status: str
    revision: int
    config: AgentConfig
    published_version: int | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "revision": self.revision,
            "config": self.config.to_dict(),
            "published_version": self.published_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class AgentVersion:
    agent_id: str
    tenant_id: str
    version_no: int
    snapshot: dict[str, Any]
    published_by: str
    published_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "version_no": self.version_no,
            "snapshot": json.loads(json.dumps(self.snapshot, ensure_ascii=False)),
            "published_by": self.published_by,
            "published_at": self.published_at,
        }


class AgentStore:
    """Small SQLite persistence layer sharing the Gateway database file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def create(
        self, tenant_id: str, name: str, description: str, config: AgentConfig, created_by: str
    ) -> Agent:
        agent_id = "agt_" + uuid.uuid4().hex
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO agents (
                    agent_id, tenant_id, name, description, status, revision,
                    config_json, published_version, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'draft', 1, ?, NULL, ?, ?, ?)
                """,
                (agent_id, tenant_id, name, description, _json(config.to_dict()), created_by, now, now),
            )
        return self.get(agent_id, tenant_id)

    def get(self, agent_id: str, tenant_id: str) -> Agent:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id = ? AND tenant_id = ?",
                (agent_id, tenant_id),
            ).fetchone()
        if row is None:
            raise AgentNotFound("agent not found")
        return _agent_from_row(row)

    def list(self, tenant_id: str) -> list[Agent]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agents WHERE tenant_id = ? ORDER BY created_at DESC",
                (tenant_id,),
            ).fetchall()
        return [_agent_from_row(row) for row in rows]

    def update(
        self,
        agent_id: str,
        tenant_id: str,
        expected_revision: int,
        *,
        name: str,
        description: str,
        config: AgentConfig,
    ) -> Agent:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE agents
                SET name = ?, description = ?, config_json = ?, revision = revision + 1, updated_at = ?
                WHERE agent_id = ? AND tenant_id = ? AND status = 'draft' AND revision = ?
                """,
                (name, description, _json(config.to_dict()), now, agent_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, agent_id, tenant_id, expected_revision)
        return self.get(agent_id, tenant_id)

    def fork_draft(self, agent_id: str, tenant_id: str, expected_revision: int) -> Agent:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM agents WHERE agent_id = ? AND tenant_id = ?",
                (agent_id, tenant_id),
            ).fetchone()
            if row is None:
                raise AgentNotFound("agent not found")
            if row["status"] != "published" or int(row["revision"]) != expected_revision:
                raise AgentConflict("agent revision or state changed")
            updated = connection.execute(
                """
                UPDATE agents SET status = 'draft', revision = revision + 1, updated_at = ?
                WHERE agent_id = ? AND tenant_id = ? AND status = 'published' AND revision = ?
                """,
                (now, agent_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                raise AgentConflict("agent revision or state changed")
        return self.get(agent_id, tenant_id)

    def publish(
        self,
        agent_id: str,
        tenant_id: str,
        expected_revision: int,
        snapshot: dict[str, Any],
        published_by: str,
    ) -> AgentVersion:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT status, revision FROM agents WHERE agent_id = ? AND tenant_id = ?",
                (agent_id, tenant_id),
            ).fetchone()
            if row is None:
                raise AgentNotFound("agent not found")
            if row["status"] != "draft" or int(row["revision"]) != expected_revision:
                raise AgentConflict("agent revision or state changed")
            current = connection.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM agent_versions WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            version_no = int(current["version_no"]) + 1
            connection.execute(
                """
                INSERT INTO agent_versions
                    (agent_id, tenant_id, version_no, snapshot_json, published_by, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (agent_id, tenant_id, version_no, _json(snapshot), published_by, now),
            )
            connection.execute(
                """
                UPDATE agents
                SET status = 'published', published_version = ?, revision = revision + 1, updated_at = ?
                WHERE agent_id = ? AND tenant_id = ? AND revision = ?
                """,
                (version_no, now, agent_id, tenant_id, expected_revision),
            )
        return AgentVersion(agent_id, tenant_id, version_no, snapshot, published_by, now)

    def disable(self, agent_id: str, tenant_id: str, expected_revision: int) -> Agent:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE agents SET status = 'disabled', revision = revision + 1, updated_at = ?
                WHERE agent_id = ? AND tenant_id = ? AND status = 'published' AND revision = ?
                """,
                (now, agent_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, agent_id, tenant_id, expected_revision)
        return self.get(agent_id, tenant_id)

    def delete_draft(self, agent_id: str, tenant_id: str, expected_revision: int) -> None:
        with self._connection(write=True) as connection:
            deleted = connection.execute(
                """
                DELETE FROM agents
                WHERE agent_id = ? AND tenant_id = ? AND status = 'draft'
                  AND published_version IS NULL AND revision = ?
                """,
                (agent_id, tenant_id, expected_revision),
            )
            if deleted.rowcount != 1:
                self._raise_update_error(connection, agent_id, tenant_id, expected_revision)

    def version(self, agent_id: str, tenant_id: str, version_no: int) -> AgentVersion:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_versions
                WHERE agent_id = ? AND tenant_id = ? AND version_no = ?
                """,
                (agent_id, tenant_id, version_no),
            ).fetchone()
        if row is None:
            raise AgentNotFound("agent version not found")
        return _version_from_row(row)

    @staticmethod
    def _raise_update_error(
        connection: sqlite3.Connection, agent_id: str, tenant_id: str, revision: int
    ) -> None:
        row = connection.execute(
            "SELECT revision FROM agents WHERE agent_id = ? AND tenant_id = ?",
            (agent_id, tenant_id),
        ).fetchone()
        if row is None:
            raise AgentNotFound("agent not found")
        raise AgentConflict(f"agent revision conflict: expected {revision}")

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    published_version INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agents_tenant_created
                    ON agents(tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS agent_versions (
                    agent_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, version_no),
                    FOREIGN KEY(agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
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


def allowed_models_from_settings(settings: Any) -> tuple[str, ...]:
    """Derive the AgentManager model whitelist from Settings — the single
    derivation shared by the gateway HTTP app and ``aiops admin reconcile``
    (providers' models, else the agent default, else ``aiops-api``)."""
    agent_settings = getattr(settings, "agent", None)
    configured = tuple(
        provider.model
        for provider in getattr(agent_settings, "providers", ())
        if getattr(provider, "model", "")
    )
    if not configured and agent_settings is not None:
        configured = (getattr(agent_settings, "model", "") or "aiops-api",)
    return configured or ("aiops-api",)


class AgentManager:
    def __init__(
        self,
        store: AgentStore,
        *,
        knowledge_resolver: KnowledgeBindingResolver | None = None,
        allowed_models: Iterable[str] = ("aiops-api",),
    ) -> None:
        self.store = store
        self.knowledge_resolver = knowledge_resolver or UnavailableKnowledgeBindingResolver()
        self.allowed_models = frozenset(item.strip() for item in allowed_models if item and item.strip())

    def create(self, context: Any, *, name: str, description: str, config: AgentConfig) -> Agent:
        self._require(context, EDIT_ROLES)
        self._validate_config(config, enforce_model=False)
        return self.store.create(
            context.effective_tenant_id,
            _label(name, "name"),
            _description(description),
            config,
            _actor(context),
        )

    def get(self, context: Any, agent_id: str) -> Agent:
        self._require(context, VIEW_ROLES)
        return self.store.get(_id(agent_id), context.effective_tenant_id)

    def list(self, context: Any) -> list[Agent]:
        self._require(context, VIEW_ROLES)
        return self.store.list(context.effective_tenant_id)

    def update(
        self,
        context: Any,
        agent_id: str,
        *,
        expected_revision: int,
        name: str,
        description: str,
        config: AgentConfig,
    ) -> Agent:
        self._require(context, EDIT_ROLES)
        self._validate_config(config, enforce_model=False)
        return self.store.update(
            _id(agent_id),
            context.effective_tenant_id,
            expected_revision,
            name=_label(name, "name"),
            description=_description(description),
            config=config,
        )

    def publish(self, context: Any, agent_id: str, *, expected_revision: int) -> AgentVersion:
        self._require(context, PUBLISH_ROLES)
        agent = self.store.get(_id(agent_id), context.effective_tenant_id)
        if agent.revision != expected_revision or agent.status != "draft":
            raise AgentConflict("agent revision or state changed")
        self._validate_config(agent.config, enforce_model=True)
        self.knowledge_resolver.validate(context.effective_tenant_id, agent.config.knowledge_base_ids)
        snapshot = {
            "name": agent.name,
            "description": agent.description,
            "status": "published",
            "revision": agent.revision,
            **agent.config.to_dict(),
        }
        return self.store.publish(
            agent.agent_id, context.effective_tenant_id, expected_revision, snapshot, _actor(context)
        )

    def fork_draft(self, context: Any, agent_id: str, *, expected_revision: int) -> Agent:
        self._require(context, EDIT_ROLES)
        return self.store.fork_draft(_id(agent_id), context.effective_tenant_id, expected_revision)

    def disable(self, context: Any, agent_id: str, *, expected_revision: int) -> Agent:
        self._require(context, PUBLISH_ROLES)
        return self.store.disable(_id(agent_id), context.effective_tenant_id, expected_revision)

    def delete(self, context: Any, agent_id: str, *, expected_revision: int) -> None:
        self._require(context, EDIT_ROLES)
        self.store.delete_draft(_id(agent_id), context.effective_tenant_id, expected_revision)

    def version(self, context: Any, agent_id: str, version_no: int) -> AgentVersion:
        self._require(context, VIEW_ROLES)
        if version_no < 1:
            raise AgentValidationError("version_no must be positive")
        return self.store.version(_id(agent_id), context.effective_tenant_id, version_no)

    @staticmethod
    def _require(context: Any, roles: frozenset[str]) -> None:
        effective_tenant = getattr(context, "effective_tenant_id", "")
        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_tenant or not effective_roles.intersection(roles):
            raise AgentForbidden("agent access is not permitted")

    def _validate_config(self, config: AgentConfig, *, enforce_model: bool) -> None:
        if config.agent_type not in AGENT_TYPES:
            raise AgentValidationError("agent_type is invalid")
        if not 1 <= len(config.prompt.strip()) <= 8000:
            raise AgentValidationError("prompt length is invalid")
        if config.output_contract not in OUTPUT_CONTRACTS:
            raise AgentValidationError("output_contract is not allowed")
        if enforce_model and config.model not in self.allowed_models:
            raise AgentValidationError("model is not allowlisted")
        if config.agent_type == "customer" and config.output_contract != "blocks-v1":
            raise AgentValidationError("customer agents require blocks-v1")
        if config.agent_type == "operations" and config.output_contract != "diagnosis-v1":
            raise AgentValidationError("operations agents require diagnosis-v1")
        for collection, label in (
            (config.knowledge_base_ids, "knowledge_base_ids"),
            (config.opening_questions, "opening_questions"),
            (config.quick_commands, "quick_commands"),
        ):
            if len(collection) > 20 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 500 for item in collection
            ):
                raise AgentValidationError(f"{label} is invalid")
        if any(not SAFE_ID.fullmatch(item) for item in config.knowledge_base_ids):
            raise AgentValidationError("knowledge_base_ids contains an invalid id")


def _agent_from_row(row: sqlite3.Row) -> Agent:
    config = json.loads(str(row["config_json"]))
    return Agent(
        agent_id=str(row["agent_id"]),
        tenant_id=str(row["tenant_id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        config=AgentConfig(
            agent_type=str(config["agent_type"]),
            prompt=str(config["prompt"]),
            knowledge_base_ids=tuple(config.get("knowledge_base_ids", ())),
            model=str(config["model"]),
            output_contract=str(config["output_contract"]),
            opening_questions=tuple(config.get("opening_questions", ())),
            quick_commands=tuple(config.get("quick_commands", ())),
        ),
        published_version=int(row["published_version"]) if row["published_version"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _version_from_row(row: sqlite3.Row) -> AgentVersion:
    return AgentVersion(
        agent_id=str(row["agent_id"]),
        tenant_id=str(row["tenant_id"]),
        version_no=int(row["version_no"]),
        snapshot=json.loads(str(row["snapshot_json"])),
        published_by=str(row["published_by"]),
        published_at=str(row["published_at"]),
    )


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _id(value: str) -> str:
    candidate = (value or "").strip()
    if not SAFE_ID.fullmatch(candidate):
        raise AgentValidationError("agent_id is invalid")
    return candidate


def _label(value: str, name: str) -> str:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 128 or any(ord(char) < 32 for char in candidate):
        raise AgentValidationError(f"{name} is invalid")
    return candidate


def _description(value: str) -> str:
    candidate = (value or "").strip()
    if len(candidate) > 1000 or any(ord(char) < 32 for char in candidate if char not in "\r\n\t"):
        raise AgentValidationError("description is invalid")
    return candidate


def _actor(context: Any) -> str:
    caller = getattr(context, "caller", None)
    return str(getattr(caller, "b_user_id", "agent-admin"))[:128]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
