"""开通操作与受管应用目录；正式指针仍由 chat_route_bindings 管理。

目录这一层从「每租户每端一个管家」扩到「受管应用可属于任一端」：`operator_apps` 加
`entry`/`scenario`/`environment`/`targets_json` 四列，表名与主键都不动。改名的收益是零
（既有部署的库文件里已经有这张表），代价是每次部署都要迁移两次。

`targets_json` 是**客户端的可发布能力**（知识库选择与只读开关）。放在目录行上而不是
另一张表，是因为它和「这个应用属于哪个客户哪一端」是同一个事实：分开存就会出现
「目录里有行、目标没有」这种半张纸的状态。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.dify import dual_entry


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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(operator_apps)")}
            for name, spec in (
                ("entry", "TEXT NOT NULL DEFAULT 'operator'"),
                ("scenario", "TEXT NOT NULL DEFAULT 'operator_order_diagnosis'"),
                ("environment", "TEXT NOT NULL DEFAULT ''"),
                ("targets_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                # 存量行按默认值补齐：它们全部是管家，与现状逐字一致，不需要数据回填。
                if name not in columns:
                    db.execute(f"ALTER TABLE operator_apps ADD COLUMN {name} {spec}")
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
        created = self.get(job_id)
        if created is None:  # pragma: no cover - 刚提交就读不到只可能是文件被换掉
            raise SetupConflict("SETUP_JOB_MISSING")
        return created

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

    def apps(self, entry: str | None = None) -> list[dict]:
        with self.connection() as db:
            if entry is None:
                rows = db.execute("SELECT * FROM operator_apps ORDER BY app_id")
            else:
                rows = db.execute("SELECT * FROM operator_apps WHERE entry=? ORDER BY app_id", (entry,))
            return [dict(r) for r in rows]

    def consumer_target(self, tenant_id: str, environment: str, app_id: str) -> dict | None:
        """这个客户在**这个环境**的、**这个应用**的受管客户端运行目标。

        按应用身份取，不按租户取第一条：一个客户可以同时有"现役正式版 + 新草稿"，
        按租户 `fetchone()` 会让第二份草稿永远拿不到自己的目标，于是它一发布就报
        `SETUP_APP_NOT_MANAGED` —— 而那看起来像"目录丢了这一行"。

        返回目录行本身；调用方从 ``targets_json`` 取可发布能力。``tenant_id`` 的比较留给
        调用方（`consumer_publication` 用与路由同一套 `tenant_matches`）。
        """
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM operator_apps"
                " WHERE entry='consumer' AND environment=? AND tenant_id=? AND app_id=?",
                (environment, tenant_id, app_id),
            ).fetchone()
        return dict(row) if row else None

    def set_targets(self, app_id: str, targets: dict) -> None:
        with self.connection() as db:
            count = db.execute(
                "UPDATE operator_apps SET targets_json=? WHERE app_id=?",
                (json.dumps(targets, ensure_ascii=False, sort_keys=True), app_id),
            ).rowcount
        if count != 1:
            raise SetupConflict("SETUP_APP_CONFLICT")

    def register(
        self,
        *,
        app_id,
        tenant_id,
        workspace_id,
        agent_name,
        profile_id,
        actor,
        entry="operator",
        scenario="",
        environment="",
        targets=None,
    ):
        scenario = scenario or dual_entry.scenario(entry)
        if scenario != dual_entry.scenario(entry):
            raise SetupConflict("SETUP_APP_CONFLICT")
        #: 判"同一条登记"的字段。**`created_by` 不在里面**：它是"谁先登记的"，
        #: 不是这条登记的身份。启动时的纳管（`managed-adoption`）会对已经在册的应用再
        #: 调一次 `register`，把 actor 算进比较就会让**每次启动都失败** —— 2026-10-30
        #: 41 的 CD 自检就是这么红的（`SetupConflict: SETUP_APP_CONFLICT`，主机自动回滚）。
        identity = (app_id, tenant_id, workspace_id, agent_name, profile_id, entry, scenario)
        encoded = json.dumps(targets or {}, ensure_ascii=False, sort_keys=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operator_apps WHERE app_id=?", (app_id,)).fetchone()
            if old:
                if (
                    tuple(
                        old[k]
                        for k in (
                            "app_id",
                            "tenant_id",
                            "workspace_id",
                            "agent_name",
                            "profile_id",
                            "entry",
                            "scenario",
                        )
                    )
                    != identity
                ):
                    raise SetupConflict("SETUP_APP_CONFLICT")
                if targets is not None and old["targets_json"] != encoded:
                    db.execute("UPDATE operator_apps SET targets_json=? WHERE app_id=?", (encoded, app_id))
                return
            db.execute(
                "INSERT INTO operator_apps"
                "(app_id,tenant_id,workspace_id,agent_name,profile_id,created_by,entry,scenario,"
                "environment,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*identity[:5], actor, *identity[5:], environment, encoded),
            )


#: 逐端公开状态的字段白名单。多给字段就是把内部资源 ID 顺手交给页面；
#: 少给字段页面就只能猜。这里逐字列出，不做 `dict(row)` 透传。
_ENTRY_FIELDS = (
    "app_id",
    "version_no",
    "published",
    "draft_only",
    "knowledge",
    "channel",
    "channel_note",
    "state",
    "error_code",
)


def public_entry(row: dict | None) -> dict:
    body = {key: (row or {}).get(key) for key in _ENTRY_FIELDS}
    body["published"] = bool(body["published"])
    body["draft_only"] = bool(body["draft_only"])
    body["version_no"] = body["version_no"] or None
    return body


def public_job(row: dict) -> dict:
    request, resources = json.loads(row["request_json"]), json.loads(row["resources_json"])
    entries = {
        entry: public_entry((resources.get("entries") or {}).get(entry))
        for entry in request.get("entries") or ["operator"]
    }
    return {
        "id": row["id"],
        "state": (
            "failed" if row["state"] == "running" and row["lease_until"] < time.time() else row["state"]
        ),
        "step": row["step"],
        "tenant_name": request["tenant_name"],
        "name": request["name"],
        "scope": request.get("scope", "operator"),
        "entries": entries,
        # 顶层三个字段继续镜像管家端（没有管家端时镜像第一个已选端），既有页面与既有
        # 契约不改也能跑；读取方一律优先读 `entries`。
        "app_id": resources.get("app_id"),
        "workspace_id": resources.get("workspace_id"),
        "version_no": resources.get("version_no"),
        "published": resources.get("published", False),
        "error_code": row["error_code"],
    }
