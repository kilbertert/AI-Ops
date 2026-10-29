"""开通操作与受管草稿身份；正式指针仍由 chat_route_bindings 管理。"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file


class SetupConflict(ValueError):
    pass


class SelfServiceStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().absolute()
        ensure_private_directory(self.path.parent)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operator_apps (
                    app_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL, agent_name TEXT NOT NULL,
                    profile_id TEXT NOT NULL, created_by TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS operator_setup_jobs (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL,
                    request_json TEXT NOT NULL, state TEXT NOT NULL, step TEXT NOT NULL,
                    resources_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                    UNIQUE(actor, request_key)
                );
            """)
        protect_private_file(self.path)

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            yield db

    def recover(self):
        with self.connection() as db:
            db.execute(
                "UPDATE operator_setup_jobs SET state='failed',step='knowledge',"
                "error_code='SETUP_KNOWLEDGE_REQUIRED' "
                "WHERE state='completed' AND json_extract(resources_json,'$.knowledge_ready') IS NOT 1"
            )
            db.execute(
                "UPDATE operator_setup_jobs SET state='failed',error_code='SETUP_INTERRUPTED',"
                "lease_until=0,lease_owner='' WHERE state IN ('pending','running')"
            )

    def create(self, actor: str, request_key: str, request: dict) -> dict:
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM operator_setup_jobs WHERE actor=? AND request_key=?", (actor, request_key)
            ).fetchone()
            if row:
                if row["request_json"] != encoded:
                    raise SetupConflict("SETUP_REQUEST_CHANGED")
                return dict(row)
            active_count = db.execute(
                "SELECT count(*) FROM operator_setup_jobs WHERE state IN ('pending','running')"
            ).fetchone()[0]
            if active_count >= 20:
                raise SetupConflict("SETUP_BUSY")
            # 同一客户的外部资源准备串行；不同客户不互相阻塞。
            for active in db.execute(
                "SELECT * FROM operator_setup_jobs WHERE state IN ('pending','running')"
            ):
                if json.loads(active["request_json"])["tenant_id"] == request["tenant_id"]:
                    raise SetupConflict("SETUP_TENANT_BUSY")
            job_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO operator_setup_jobs(id,actor,request_key,request_json,state,step,updated_at) "
                "VALUES(?,?,?,?,'pending','workspace',?)",
                (job_id, actor, request_key, encoded, time.time()),
            )
        return self.get(job_id)

    def request(self, actor: str, request_key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM operator_setup_jobs WHERE actor=? AND request_key=?", (actor, request_key)
            ).fetchone()
        return dict(row) if row else None

    def pending(self, job_id):
        with self.connection() as db:
            db.execute(
                "UPDATE operator_setup_jobs SET state='pending',error_code='' "
                "WHERE id=? AND (state='failed' OR (state='running' AND lease_until<?))",
                (job_id, time.time()),
            )

    def get(self, job_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM operator_setup_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, actor: str) -> list[dict]:
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM operator_setup_jobs WHERE actor=? ORDER BY updated_at DESC LIMIT 20",
                    (actor,),
                )
            ]

    def claim(self, job_id: str, owner: str) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operator_setup_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] == "completed" or row["lease_until"] > time.time():
                return False
            tenant = json.loads(row["request_json"])["tenant_id"]
            others = db.execute(
                "SELECT request_json FROM operator_setup_jobs WHERE id!=? AND lease_until>?",
                (job_id, time.time()),
            )
            if any(json.loads(r[0])["tenant_id"] == tenant for r in others):
                return False
            db.execute(
                "UPDATE operator_setup_jobs SET state='running',error_code='',lease_owner=?,"
                "lease_until=?,updated_at=? WHERE id=?",
                (owner, time.time() + 600, time.time(), job_id),
            )
        return True

    def save(self, job_id: str, owner: str, *, step: str, resources: dict, state="running", error=""):
        with self.connection() as db:
            count = db.execute(
                "UPDATE operator_setup_jobs SET step=?,resources_json=?,state=?,error_code=?,"
                "lease_until=?,updated_at=? WHERE id=? AND lease_owner=? AND lease_until>?",
                (
                    step,
                    json.dumps(resources, ensure_ascii=False),
                    state,
                    error,
                    time.time() + 600 if state == "running" else 0,
                    time.time(),
                    job_id,
                    owner,
                    time.time(),
                ),
            ).rowcount
            if count != 1:
                raise SetupConflict("SETUP_LEASE_LOST")

    def app(self, app_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM operator_apps WHERE app_id=?", (app_id,)).fetchone()
        return dict(row) if row else None

    def apps(self) -> list[dict]:
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM operator_apps ORDER BY app_id")]

    def register(self, *, app_id, tenant_id, workspace_id, agent_name, profile_id, actor):
        values = (app_id, tenant_id, workspace_id, agent_name, profile_id, actor)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operator_apps WHERE app_id=?", (app_id,)).fetchone()
            if old:
                if (
                    tuple(old[k] for k in ("app_id", "tenant_id", "workspace_id", "agent_name", "profile_id"))
                    != values[:5]
                ):
                    raise SetupConflict("SETUP_APP_CONFLICT")
                return
            db.execute(
                "INSERT INTO operator_apps(app_id,tenant_id,workspace_id,agent_name,profile_id,created_by) "
                "VALUES(?,?,?,?,?,?)",
                values,
            )


def public_job(row: dict) -> dict:
    request, resources = json.loads(row["request_json"]), json.loads(row["resources_json"])
    return {
        "id": row["id"],
        "state": (
            "failed" if row["state"] == "running" and row["lease_until"] < time.time() else row["state"]
        ),
        "step": row["step"],
        "tenant_name": request["tenant_name"],
        "name": request["name"],
        "app_id": resources.get("app_id"),
        "workspace_id": resources.get("workspace_id"),
        "version_no": resources.get("version_no"),
        "published": resources.get("published", False),
        "error_code": row["error_code"],
    }
