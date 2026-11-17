"""目录草稿账本：复用生效目录的授权和锁，不将保存视为发布。"""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiops_diagnostics.common.private_files import validate_private_directory, validate_private_file
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.gateway import environment_registry as registry
from aiops_diagnostics.gateway import gateway_db

REFERENCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def validate(body):
    fields = {
        "id",
        "name",
        "enabled",
        "gateway_url",
        "cpms_base_url",
        "company_gateway_url",
        "deployment_target",
        "credential_ref",
        "timezone",
        "tenants",
    }
    if not isinstance(body, dict) or set(body) != fields:
        raise registry.RegistryError("ENVIRONMENT_DRAFT_INVALID")
    for key in ("deployment_target", "credential_ref"):
        if not isinstance(body[key], str) or not REFERENCE_PATTERN.fullmatch(body[key]):
            raise registry.RegistryError("ENVIRONMENT_REFERENCE_INVALID")
    try:
        if not isinstance(body["timezone"], str) or len(body["timezone"]) > 64:
            raise ValueError()
        ZoneInfo(body["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise registry.RegistryError("ENVIRONMENT_TIMEZONE_INVALID") from exc
    # 配置路径由安装器决定；浏览器只提供受管目标和凭据引用。
    row = {k: v for k, v in body.items() if k not in {"deployment_target", "credential_ref", "timezone"}}
    try:
        registry.validate([{**row, "config_ref": "/managed-by-installer"}], managed=True)
    except ValueError as exc:
        raise registry.RegistryError("ENVIRONMENT_DRAFT_INVALID") from exc


def access(body, identity):
    scopes = set()
    for m in body.get("managers", []):
        if tuple(m[k] for k in ("environment", "tenant_id", "user_id")) == identity:
            scopes.update(m["environments"])
    if not scopes:
        raise registry.RegistryError("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
    return scopes


def _database(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in path.parents):
        raise registry.RegistryError("ENVIRONMENT_REGISTRY_UNAVAILABLE")
    validate_private_directory(path.parent)
    db = path.with_name(path.name + ".drafts.db")
    if db.exists() or db.is_symlink():
        validate_private_file(db)
    return db


@contextmanager
def _locked(path):
    import fcntl

    path = Path(path).absolute()
    _database(path)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _rows(db):
    if not db.exists():
        return []
    with gateway_db.connection(db, readonly=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='environment_drafts'"
        ).fetchone():
            return []
        return [
            dict(r)
            for r in conn.execute(
                "SELECT environment,revision,registry_revision,body,digest,updated_at "
                "FROM environment_drafts ORDER BY environment"
            )
        ]


def catalogue(path, identity):
    with _locked(path):
        body = registry.load(path)
        allowed = access(body, identity)
        rows = _rows(_database(path))
        return {
            "registry_revision": body["revision"],
            "can_create": "*" in allowed,
            "effective": [
                {k: v for k, v in r.items() if k != "config_ref"}
                for r in body["environments"]
                if "*" in allowed or r["id"] in allowed
            ],
            "drafts": [
                {
                    **{k: v for k, v in r.items() if k != "body"},
                    "draft": json.loads(r["body"]),
                    "state": "draft",
                    "acceptance": "unverified",
                    "registry_changed": r["registry_revision"] != body["revision"],
                }
                for r in rows
                if "*" in allowed or r["environment"] in allowed
            ],
        }


def save(path, identity, draft, *, expected_revision, registry_revision):
    if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
        raise registry.RegistryError("ENVIRONMENT_DRAFT_REVISION_INVALID")
    validate(draft)
    encoded = json.dumps(draft, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 65536:
        raise registry.RegistryError("ENVIRONMENT_DRAFT_TOO_LARGE")
    with _locked(path):
        effective = registry.load(path)
        allowed = access(effective, identity)
        if "*" not in allowed and draft["id"] not in allowed:
            raise registry.RegistryError("ENVIRONMENT_MANAGEMENT_FORBIDDEN")
        if type(registry_revision) is not int or effective["revision"] != registry_revision:
            raise registry.RegistryError("ENVIRONMENT_REGISTRY_REVISION_CONFLICT")
        db = _database(path)
        if not db.exists():
            fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with gateway_db.initialize(db) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS environment_drafts (environment TEXT PRIMARY KEY, "
                "revision INTEGER NOT NULL, registry_revision INTEGER NOT NULL, "
                "body TEXT NOT NULL, digest TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS environment_draft_audit (environment TEXT NOT NULL, "
                "revision INTEGER NOT NULL, digest TEXT NOT NULL, actor TEXT NOT NULL, "
                "at TEXT NOT NULL, PRIMARY KEY(environment,revision))"
            )
            old = conn.execute(
                "SELECT revision FROM environment_drafts WHERE environment=?", (draft["id"],)
            ).fetchone()
            if (old[0] if old else 0) != expected_revision:
                raise registry.RegistryError("ENVIRONMENT_DRAFT_REVISION_CONFLICT")
            if not old and conn.execute("SELECT COUNT(*) FROM environment_drafts").fetchone()[0] >= 64:
                raise registry.RegistryError("ENVIRONMENT_DRAFT_LIMIT")
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            at = format_stored_time(now_utc())
            actor = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            revision = expected_revision + 1
            conn.execute(
                "INSERT INTO environment_drafts VALUES(?,?,?,?,?,?) ON CONFLICT(environment) DO UPDATE SET "
                "revision=excluded.revision,registry_revision=excluded.registry_revision,"
                "body=excluded.body,digest=excluded.digest,updated_at=excluded.updated_at",
                (draft["id"], revision, registry_revision, encoded, digest, at),
            )
            conn.execute(
                "INSERT INTO environment_draft_audit VALUES(?,?,?,?,?)",
                (draft["id"], revision, digest, actor, at),
            )
        return {
            "revision": revision,
            "registry_revision": registry_revision,
            "digest": digest,
            "state": "draft",
            "acceptance": "unverified",
        }


def initialize_plans(connection):
    """同一独立草稿库的持久结构在此统一声明。"""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS environment_plans (id TEXT PRIMARY KEY, "
        "environment TEXT NOT NULL, digest TEXT NOT NULL, inputs TEXT NOT NULL, "
        "created_at REAL NOT NULL, expires_at REAL NOT NULL)"
    )
