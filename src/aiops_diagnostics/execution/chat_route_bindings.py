"""聊天路由的**正式绑定**：这个（环境，租户，入口，场景）此刻由哪一版执行。

## 它补的是哪道缝

ADR-0124 定的是「一条登记只指向一个引擎」，登记写在运维手填的 0600 JSON 里。
ADR-0104 的「仍未做」一节点名了缺口：**SOP 发布绑定**。现状是

* 发布桥把 Dify 里的 DSL 冻结成 `agent_versions` 一行（`sync_binding_from_dify`）；
* 聊天路线读的是**运维手填**的 `workflow_id`；
* 两者之间没有任何代码路径相连 —— 也就是说，运营点了「发布」，App 跑的还是上一版。

本模块就是那条缺失的路径，且**只有一条**。

## 三个刻意的决定

### 1. 正式指针就是这张表里的 `active` 行

不新造"当前版本"概念，也不复用 `agents.published_version`：后者是**agent 内容**的
权威（可以被试用、被多个入口共用），而这里是**路由**的权威（哪个入口此刻走哪一版）。
一个租户可能同时有运营入口和 consumer 入口指向不同版本，把它们塞进一个标量列里，
第一次出现第二个入口时就要改表。

行上冻结的 `agent_version_no` 指向 `agent_versions` 的那一行；内容本身不进这张表：
版本快照是不可变证据，路由只是指针。

### 2. 撤回是**改状态**，不是删行

删行会让 `select_binding` 找不到绑定，于是请求回到直读 —— 那正是任务书点名禁止的
「用直读冒充停用」。所以 `state` 只有 `active` / `retracted` 两值，撤回后这一行仍在，
查得到、能回答"这一版被撤回了"，并且**不会**交付。

### 3. 管理行与文件行同时存在 = 启动失败

`chat_execution` 仍支持运维手填的 JSON（节点路线与既有 Dify 部署靠它）。同一个 key
两边都有 ⇒ **拒绝启动**，不按"谁优先"裁决。隐式优先级会让"这次到底谁在跑"变成
一个问题，而不是一个事实。

## 与执行记录的关系

执行记录（`chat_dify_runs` / `agent_runs`）在签发时就冻结 `binding_key`，并按它找回
**签发时那一条**。本表被改写不会改变已签发 run 的解释；回读守卫另外要求「当前这条
绑定仍指向同一个 `agent_version_no`」，所以撤回之后，在飞与历史结果都不再交付。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 表只认这两个状态。没有 `deleted`：撤回不删行（见模块文档第 2 条）。
STATE_ACTIVE = "active"
STATE_RETRACTED = "retracted"
STATES = (STATE_ACTIVE, STATE_RETRACTED)

#: 与 `chat_execution` 的引擎词表同一份取值。这里复制的是**取值**，不是判别逻辑：
#: 判别仍然只在 `chat_execution.ENGINES` 那处定义，本模块只负责存。
ENGINE_NODE = "node"
ENGINE_DIFY = "dify"


class RouteBindingError(RuntimeError):
    """路由绑定层面的拒绝。``code`` 是稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RouteBinding:
    """一条正式绑定。``agent_id`` + ``agent_version_no`` 指向不可变版本快照。"""

    environment: str
    tenant_id: str
    entry: str
    scenario: str
    engine: str
    agent_id: str
    agent_version_no: int
    #: 仅 dify：应用身份、已发布 workflow 身份、服务凭据的**引用**（不是凭据本身）。
    app_id: str = ""
    workflow_id: str = ""
    workflow_key_ref: str = ""
    node_config_file: str = ""
    state: str = STATE_ACTIVE
    #: 乐观并发：每次改写 +1。发布与撤回都按它做 CAS。
    revision: int = 0
    updated_by: str = ""
    updated_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.scenario}:{self.environment}:{self.tenant_id}"

    @property
    def active(self) -> bool:
        return self.state == STATE_ACTIVE


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ChatRouteBindingStore:
    """`chat_route_bindings` 表。与网关共用同一个 SQLite 文件，各自建表。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_route_bindings (
                    environment TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    entry TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    agent_version_no INTEGER NOT NULL,
                    app_id TEXT NOT NULL DEFAULT '',
                    workflow_id TEXT NOT NULL DEFAULT '',
                    workflow_key_ref TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'active',
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (environment, tenant_id, entry, scenario)
                )
                """
            )

        with self._connection(write=True) as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(chat_route_bindings)")}
            if "node_config_file" not in columns:
                connection.execute(
                    "ALTER TABLE chat_route_bindings ADD COLUMN node_config_file TEXT NOT NULL DEFAULT ''"
                )

    def _connection(self, *, write: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    # ------------------------------------------------------------------ 读

    def get(self, environment: str, tenant_id: str, entry: str, scenario: str) -> RouteBinding | None:
        with self._connection(write=False) as connection:
            row = connection.execute(
                """
                SELECT * FROM chat_route_bindings
                 WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
                """,
                (environment, tenant_id, entry, scenario),
            ).fetchone()
        return _row(row) if row else None

    def by_key(self, key: str) -> RouteBinding | None:
        """按 `scenario:environment:tenant` 找回。找不到 ⇒ None（不是"回落"）。"""
        parts = key.split(":")
        if len(parts) != 3:
            return None
        scenario, environment, tenant_id = parts
        entry = "operator" if scenario == "operator_order_diagnosis" else "consumer"
        return self.get(environment, tenant_id, entry, scenario)

    def all(self) -> tuple[RouteBinding, ...]:
        with self._connection(write=False) as connection:
            rows = connection.execute("SELECT * FROM chat_route_bindings").fetchall()
        return tuple(_row(row) for row in rows)

    # ------------------------------------------------------------------ 写

    def publish(
        self,
        *,
        environment: str,
        tenant_id: str,
        entry: str,
        scenario: str,
        engine: str,
        agent_id: str,
        agent_version_no: int,
        app_id: str = "",
        workflow_id: str = "",
        workflow_key_ref: str = "",
        node_config_file: str = "",
        expected_revision: int | None = None,
        updated_by: str = "",
        source_agent_name: str = "",
    ) -> RouteBinding:
        """把正式指针推进到这一版。

        `expected_revision` 是**乐观锁**：给定时，只有当前行仍是那个 revision 才写。
        新行（此前没有绑定）用 `expected_revision=0` 表达"我认为这里还是空的"。
        并发改写会以 `ROUTE_BINDING_CONFLICT` 失败，而不是后写覆盖先写。
        """
        if engine not in (ENGINE_NODE, ENGINE_DIFY):
            raise RouteBindingError("ROUTE_ENGINE_UNKNOWN", "unknown engine")
        now = _now()
        with self._connection(write=True) as connection:
            current = connection.execute(
                """
                SELECT revision FROM chat_route_bindings
                 WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
                """,
                (environment, tenant_id, entry, scenario),
            ).fetchone()
            # **CAS 必须在写语句里完成**，不能"读一次、比一比、再写"：sqlite3 的默认
            # 事务要等到第一条写语句才开始，所以上面那个 SELECT 并不持有写锁 ——
            # 两个发布者可以同时读到同一个 revision 再各写一次（实测：都成功、都是 2）。
            # 下面两种写法各自带 revision 谓词，写不中就是 rowcount 0 ⇒ 冲突。
            actual = int(current["revision"]) if current else 0
            if expected_revision is not None and actual != expected_revision:
                raise RouteBindingError("ROUTE_BINDING_CONFLICT", "route binding moved")
            next_revision = actual + 1
            if current is None:
                cursor = connection.execute(
                    """
                    INSERT INTO chat_route_bindings
                        (environment, tenant_id, entry, scenario, engine, agent_id,
                         agent_version_no, app_id, workflow_id, workflow_key_ref,
                         state, revision, updated_by, updated_at, node_config_file)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(environment, tenant_id, entry, scenario) DO NOTHING
                    """,
                    (
                        environment,
                        tenant_id,
                        entry,
                        scenario,
                        engine,
                        agent_id,
                        agent_version_no,
                        app_id,
                        workflow_id,
                        workflow_key_ref,
                        STATE_ACTIVE,
                        next_revision,
                        updated_by,
                        now,
                        node_config_file,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE chat_route_bindings
                       SET engine = ?, agent_id = ?, agent_version_no = ?, app_id = ?,
                           workflow_id = ?, workflow_key_ref = ?, state = ?,
                           revision = ?, updated_by = ?, updated_at = ?, node_config_file = ?
                     WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
                       AND revision = ?
                    """,
                    (
                        engine,
                        agent_id,
                        agent_version_no,
                        app_id,
                        workflow_id,
                        workflow_key_ref,
                        STATE_ACTIVE,
                        next_revision,
                        updated_by,
                        now,
                        node_config_file,
                        environment,
                        tenant_id,
                        entry,
                        scenario,
                        actual,
                    ),
                )
            # 没写中 ⇒ 有人在这期间改过这一行。不覆盖对方。
            if cursor.rowcount != 1:
                raise RouteBindingError("ROUTE_BINDING_CONFLICT", "route binding moved")
            if source_agent_name:
                connection.execute(
                    "INSERT INTO dify_app_registry(tenant_id,business_entry,app_id,agent_name,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,business_entry) DO UPDATE SET "
                    "app_id=excluded.app_id,agent_name=excluded.agent_name,updated_at=excluded.updated_at",
                    (tenant_id, entry, app_id, source_agent_name, now),
                )
        record = self.get(environment, tenant_id, entry, scenario)
        if record is None:  # pragma: no cover - 刚写完就读不到只可能是文件被换掉
            raise RouteBindingError("ROUTE_BINDING_MISSING", "route binding vanished")
        return record

    def retract(
        self,
        *,
        environment: str,
        tenant_id: str,
        entry: str,
        scenario: str,
        expected_revision: int | None = None,
        updated_by: str = "",
    ) -> RouteBinding:
        """撤回：把这一行改成 `retracted`，**不删行**。

        已经是 `retracted` 时按幂等处理（重复撤回是同一个结果），这不掩盖问题：
        "本来就撤回了"与"这次撤回成功"对调用方是同一个答案，而删除行会是另一个答案
        （请求会掉回直读）。
        """
        now = _now()
        with self._connection(write=True) as connection:
            current = connection.execute(
                """
                SELECT revision FROM chat_route_bindings
                 WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
                """,
                (environment, tenant_id, entry, scenario),
            ).fetchone()
            if current is None:
                raise RouteBindingError("ROUTE_BINDING_MISSING", "no route binding to retract")
            if expected_revision is not None and int(current["revision"]) != expected_revision:
                raise RouteBindingError("ROUTE_BINDING_CONFLICT", "route binding moved")
            # 与 `publish` 同一个理由：CAS 落在写语句里。否则一个过期的撤回者
            # 可以把别人刚发布的新版撤掉 —— 它读到的是旧 revision，写的却是新行。
            cursor = connection.execute(
                """
                UPDATE chat_route_bindings
                   SET state = ?, revision = revision + 1, updated_by = ?, updated_at = ?
                 WHERE environment = ? AND tenant_id = ? AND entry = ? AND scenario = ?
                   AND revision = ?
                """,
                (
                    STATE_RETRACTED,
                    updated_by,
                    now,
                    environment,
                    tenant_id,
                    entry,
                    scenario,
                    int(current["revision"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RouteBindingError("ROUTE_BINDING_CONFLICT", "route binding moved")
        record = self.get(environment, tenant_id, entry, scenario)
        if record is None:  # pragma: no cover
            raise RouteBindingError("ROUTE_BINDING_MISSING", "route binding vanished")
        return record


def _row(row: Any) -> RouteBinding:
    return RouteBinding(
        environment=row["environment"],
        tenant_id=row["tenant_id"],
        entry=row["entry"],
        scenario=row["scenario"],
        engine=row["engine"],
        agent_id=row["agent_id"],
        agent_version_no=int(row["agent_version_no"]),
        app_id=row["app_id"],
        workflow_id=row["workflow_id"],
        workflow_key_ref=row["workflow_key_ref"],
        node_config_file=row["node_config_file"],
        state=row["state"],
        revision=int(row["revision"]),
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
    )
