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
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from aiops_diagnostics.common.private_files import (
    ensure_private_directory,
    protect_private_file,
    validate_private_file,
)
from aiops_diagnostics.dify import dual_entry
from aiops_diagnostics.gateway import gateway_db, gateway_schema


class SetupConflict(ValueError):
    pass


class SelfServiceStore:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.path = path.expanduser().absolute()
        if readonly:
            validate_private_file(self.path)
            gateway_schema.check(self.path)
            return
        ensure_private_directory(self.path.parent)
        gateway_schema.ensure(self.path)
        protect_private_file(self.path)

    @contextmanager
    def connection(self, *, write: bool = True):
        # 默认写租约兼容外部写调用；纯 SELECT 显式选择读连接。
        with gateway_db.connection(self.path, write=write) as connection:
            yield connection

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
            # 租约由 LEASE 在连接入口持有——这里不再需要手动 BEGIN。
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
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM operator_setup_jobs WHERE actor=? AND request_key=?", (actor, request_key)
            ).fetchone()
        return dict(row) if row else None

    def pending(self, job_id):
        with self.connection() as db:
            db.execute(
                "UPDATE operator_setup_jobs SET state='pending',error_code='' "
                "WHERE id=? AND (state IN ('failed','partial') OR (state='running' AND lease_until<?))",
                (job_id, time.time()),
            )

    def get(self, job_id: str) -> dict | None:
        with self.connection(write=False) as db:
            row = db.execute("SELECT * FROM operator_setup_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, actor: str) -> list[dict]:
        with self.connection(write=False) as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM operator_setup_jobs WHERE actor=? ORDER BY updated_at DESC LIMIT 20",
                    (actor,),
                )
            ]

    def claim(self, job_id: str, owner: str) -> bool:
        with self.connection() as db:
            # 租约由 LEASE 在连接入口持有——这里不再需要手动 BEGIN。
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
        with self.connection(write=False) as db:
            row = db.execute("SELECT * FROM operator_apps WHERE app_id=?", (app_id,)).fetchone()
        return dict(row) if row else None

    def apps(self, entry: str | None = None) -> list[dict]:
        with self.connection(write=False) as db:
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
        with self.connection(write=False) as db:
            row = db.execute(
                "SELECT * FROM operator_apps"
                " WHERE entry='consumer' AND environment=? AND tenant_id=? AND app_id=?",
                (environment, tenant_id, app_id),
            ).fetchone()
        return dict(row) if row else None

    def recoverable_consumer_draft(self, app_id, tenant_id, workspace_id, environment):
        """只识别系统创建且从未更新/发布的受管应用半成品，不用提示词判归属。"""
        with self.connection(write=False) as db:
            rows = db.execute(
                "SELECT a.agent_id FROM agents a JOIN operator_apps o "
                "ON a.tenant_id=o.tenant_id AND a.name=o.agent_name "
                "WHERE o.app_id=? AND o.tenant_id=? AND o.workspace_id=? AND o.environment=? "
                "AND o.entry='consumer' AND o.agent_name=? AND a.created_by='aiops-admin' "
                "AND a.status='draft' AND a.revision=1 AND a.published_version IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM agent_versions v WHERE v.agent_id=a.agent_id)",
                (app_id, tenant_id, workspace_id, environment, "consumer-" + app_id),
            ).fetchall()
        return rows[0]["agent_id"] if len(rows) == 1 else None

    def repair_entry(self, app_id: str, *, tenant_id: str, entry: str) -> bool:
        """把一条端写错的旧登记改回注册表说的那一端；返回是否改动。

        2026-10-31 现场：`operator_apps` 里一条**客户端**应用的端是 `operator`、场景是管家的、
        `profile_id=''`。`register` 遇到同一条登记只会更新元数据，于是这条错行在每次启动纳管时
        都被"确认"一次：页面读客户端状态 `KeyError: ''`（500），双端开通报
        `SETUP_SOURCE_NOT_PUBLISHED`。

        只有**注册表**能裁定端（`dify_app_registry.business_entry`），所以修复入口在这里，
        而不是让任意 `register` 调用改端。改动范围只有端与场景；`profile_id` 由调用方随后用
        `register` 覆盖。
        """
        scenario = dual_entry.scenario(entry)
        with self.connection() as db:
            # 租约由 LEASE 在连接入口持有——这里不再需要手动 BEGIN。
            row = db.execute("SELECT tenant_id,entry FROM operator_apps WHERE app_id=?", (app_id,)).fetchone()
            if row is None or row["tenant_id"] != tenant_id or row["entry"] == entry:
                return False
            db.execute(
                "UPDATE operator_apps SET entry=?,scenario=?,profile_id='' WHERE app_id=?",
                (entry, scenario, app_id),
            )
        return True

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
        connection=None,
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
        with nullcontext(connection) if connection is not None else self.connection() as db:
            # 租约由 LEASE 在连接入口持有——这里不再需要手动 BEGIN。
            old = db.execute("SELECT * FROM operator_apps WHERE app_id=?", (app_id,)).fetchone()
            if old:
                # **应用与客户**是这条登记的身份；工作区、agent 名、profile 是可推导的元数据。
                # 同一应用既能是管家端也能是客户端（两端各有自己的 Dify 应用，但**同一个**
                # 应用被登记成另一个端仍然是冲突）。所以端不放开：放开它就把"改个标签当另一端"
                # 变成一条静默的更新。
                if (old["app_id"], old["tenant_id"], old["entry"]) != (app_id, tenant_id, entry):
                    raise SetupConflict("SETUP_APP_CONFLICT")
                # 端写错的**旧行**由纳管（`repair_entry`）显式改回，见 api 层；这里只管同端更新。
                db.execute(
                    "UPDATE operator_apps SET workspace_id=?,agent_name=?,profile_id=?,scenario=?,"
                    "environment=?,targets_json=? WHERE app_id=?",
                    (
                        workspace_id,
                        agent_name,
                        profile_id,
                        scenario,
                        environment or old["environment"],
                        encoded if targets is not None else old["targets_json"],
                        app_id,
                    ),
                )
                return
            db.execute(
                "INSERT INTO operator_apps"
                "(app_id,tenant_id,workspace_id,agent_name,profile_id,created_by,entry,scenario,"
                "environment,targets_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*identity[:5], actor, *identity[5:], environment, encoded),
            )

    def register_runtime(self, registration, consumer, *, verify, observe_only=False):
        """只登记已核验的工作区/双端候选；不更新或收养已有不同归属。"""
        from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc

        environment, tenant, workspace = (
            registration[k] for k in ("environment", "tenant_id", "workspace_id")
        )
        desired = [
            dict(
                app_id=registration["operator_app_id"],
                tenant_id=tenant,
                workspace_id=workspace,
                agent_name="operator-" + registration["operator_app_id"],
                profile_id="operator-" + environment,
                entry="operator",
                environment=environment,
                targets={},
            ),
            dict(
                app_id=registration["consumer_app_id"],
                tenant_id=tenant,
                workspace_id=workspace,
                agent_name="consumer-" + registration["consumer_app_id"],
                profile_id="",
                entry="consumer",
                environment=environment,
                targets={
                    k: consumer[k] for k in ("knowledge_base_ids", "live_order_status", "live_billing_record")
                }
                | {"photo_enabled": consumer.get("photo_enabled", False)},
            ),
        ]
        with gateway_db.connection(self.path, write=not observe_only, readonly=observe_only) as db:
            if observe_only:
                db.execute("BEGIN")
            verify()
            from aiops_diagnostics.gateway.gateway_store import JOB_TABLES

            active_rows = [(t, "status", ("queued", "running")) for t in JOB_TABLES]
            active_rows += [
                ("agent_runs", "state", ("issued", "running")),
                ("chat_dify_runs", "state", ("running", "running")),
                ("operator_setup_jobs", "state", ("pending", "running")),
            ]
            for table, column, active in active_rows:
                if db.execute(f"SELECT 1 FROM {table} WHERE {column} IN (?,?) LIMIT 1", active).fetchone():
                    raise SetupConflict("SETUP_INFLIGHT_WORK")
            existing_workspace = db.execute(
                "SELECT * FROM dify_workspaces WHERE tenant_id=? AND environment=?", (tenant, environment)
            ).fetchone()
            if (
                db.execute(
                    "SELECT 1 FROM dify_workspaces WHERE workspace_id=? "
                    "AND NOT (tenant_id=? AND environment=?)",
                    (workspace, tenant, environment),
                ).fetchone()
                or existing_workspace
                and (
                    existing_workspace["workspace_id"] != workspace
                    or existing_workspace["setup_state"] != "created"
                )
            ):
                raise SetupConflict("SETUP_WORKSPACE_CONFLICT")
            if (
                db.execute(
                    "SELECT 1 FROM chat_route_bindings WHERE environment=? AND tenant_id=?",
                    (environment, tenant),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM dify_app_registry WHERE tenant_id=? OR app_id IN (?,?)",
                    (tenant, registration["operator_app_id"], registration["consumer_app_id"]),
                ).fetchone()
            ):
                raise SetupConflict("SETUP_ROUTE_CONFLICT")
            replaced = False
            existing_apps = []
            for app in desired:
                row = db.execute("SELECT * FROM operator_apps WHERE app_id=?", (app["app_id"],)).fetchone()
                if row is None and not observe_only:
                    # 候选被一次越过中央的发布消费后由准备对账重铸：同租户同端同工作区
                    # 的既有登记是本血统的上一任候选，原位换成新应用；任何其它不一致
                    # 仍是归属冲突，不收养、不静默覆盖。
                    prior = db.execute(
                        "SELECT * FROM operator_apps WHERE tenant_id=? AND environment=? AND entry=?",
                        (tenant, environment, app["entry"]),
                    ).fetchone()
                    if prior is not None and (
                        prior["created_by"] != "environment-runtime"
                        or prior["revision"] != 1
                        or prior["workspace_id"] != workspace
                        or prior["agent_name"] != app["entry"] + "-" + prior["app_id"]
                        or prior["profile_id"] != app["profile_id"]
                        or prior["scenario"] != dual_entry.scenario(app["entry"])
                    ):
                        raise SetupConflict("SETUP_APP_CONFLICT")
                    if prior is not None:
                        db.execute(
                            "UPDATE operator_apps SET app_id=?,agent_name=?,targets_json=? WHERE app_id=?",
                            (
                                app["app_id"],
                                app["agent_name"],
                                json.dumps(app["targets"], ensure_ascii=False, sort_keys=True),
                                prior["app_id"],
                            ),
                        )
                        replaced = True
                        row = db.execute(
                            "SELECT * FROM operator_apps WHERE app_id=?", (app["app_id"],)
                        ).fetchone()
                if row and (
                    any(
                        row[k] != app[k]
                        for k in (
                            "app_id",
                            "tenant_id",
                            "workspace_id",
                            "agent_name",
                            "profile_id",
                            "entry",
                            "environment",
                        )
                    )
                    or row["revision"] != 1
                    or json.loads(row["targets_json"]) != app["targets"]
                    or row["created_by"] != "environment-runtime"
                    or row["scenario"] != dual_entry.scenario(app["entry"])
                ):
                    raise SetupConflict("SETUP_APP_CONFLICT")
                existing_apps.append(row)
            if any(existing_apps) != all(existing_apps) or bool(existing_workspace) != all(existing_apps):
                raise SetupConflict("SETUP_PARTIAL_REGISTRATION")
            if existing_workspace:
                verify()
                return bool(replaced)
            if observe_only:
                verify()
                return None
            now = format_stored_time(now_utc())
            db.execute(
                "INSERT INTO dify_workspaces(tenant_id,environment,workspace_id,setup_state,"
                "created_at,updated_at) "
                "VALUES(?,?,?,'created',?,?)",
                (tenant, environment, workspace, now, now),
            )
            for app in desired:
                self.register(**app, actor="environment-runtime", connection=db)
            verify()
            return True


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
    # 失败时的定位信息：步骤名是代码里的固定字面量，种类是异常类名。
    # 异常**正文**不在这里 —— 它可能含上游原文（URL、id、凭据片段）。
    "failed_step",
    "failure_kind",
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
        # 作业级失败的定位信息（两端都没走到时才有值；逐端失败看 `entries`）。
        "failed_step": resources.get("failed_step"),
        "failure_kind": resources.get("failure_kind"),
    }
