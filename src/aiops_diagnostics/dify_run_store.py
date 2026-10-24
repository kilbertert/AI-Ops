"""Dify 路线的**执行记录**（APP-RUNTIME-02）。

## 为什么不是往 `agent_runs` 里加字段

`agent_runs` 的列是为客户执行节点长出来的：`expected_node_id`、`actual_node_id`、
`node_build`、`exec_config_state`（`expected`/`matched`/`mismatched`）。Dify 的一次运行
**没有节点**，也**没有执行配置清单的 matched 语义**。把这些列硬填成 Dify 的值，等于用
一个看起来齐全的行去冒充另一种执行——审查的人读到的会是"这台节点核对过了"，而事实是
"这次压根没有节点"。

所以单独一张表，只放 Dify 真正有的东西：app/workflow 身份、`workflow_run_id`、
`task_id`、取数凭据的哈希、状态与时间戳。QA 用 `qa_id` 关联，与 `agent_runs.qa_id`
同一条纪律（存储关联，不从字符串反推）。

## 状态

`running`（已受理）→ `succeeded` / `failed` / `cancelled` / `expired`。
只有 `succeeded` 且**有同轮取数记录**的一次运行，才可以被适配成答案；
"没有取数却自称成功"是失败，不是成功。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: 与 `agent_runs` 同一口径：180 秒，不因为重试或轮询而延长。
DIFY_RUN_TTL_SECONDS = 180

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
EXPIRED = "expired"


class DifyRunError(RuntimeError):
    """执行记录层面的拒绝。``code`` 是稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DifyRun:
    run_id: str
    qa_id: str
    scope_fingerprint: str
    #: 签发时冻结的完整身份。回调要用它去问授权器，不能问请求体自称的身份。
    scope_json: str
    #: 签发时选中的那条绑定的钥匙。回读按它找回**签发时那一条**，不拿当前第一条顶替。
    binding_key: str
    order_no: str
    environment: str
    tenant_id: str
    app_id: str
    workflow_id: str
    workflow_run_id: str | None
    task_id: str | None
    grant_id: str
    state: str
    reason: str | None
    created_at: datetime
    expires_at: datetime
    completed_at: datetime | None
    #: 签发时对外的那一版。0 = 来自文件登记（没有版本概念）。
    agent_version_no: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


def still_valid(run: DifyRun, *, now: datetime | None = None) -> bool:
    """这条记录**此刻**还在有效期里吗。

    必须单独判，不能指望 `get()` 的过期收敛：那个收敛只把 `running` 改成 `expired`，
    一条 **succeeded** 的记录过了 180 秒仍然是 `succeeded`。结果就是"回读必须看 TTL"
    这条判据在成功路径上从未生效 —— 一次演示里把 `expires_at` 改成过去时间，
    `get()` 依旧返回 succeeded。
    """
    return run.expires_at > (now or _now())


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class DifyRunStore:
    """执行记录表。与网关共用同一个 SQLite 文件，各自建表。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_dify_runs (
                    run_id TEXT PRIMARY KEY,
                    qa_id TEXT NOT NULL,
                    scope_fingerprint TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    binding_key TEXT NOT NULL DEFAULT '',
                    order_no TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    workflow_run_id TEXT,
                    task_id TEXT,
                    grant_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    completed_at TEXT,
                    snapshot_json TEXT,
                    agent_version_no INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_chat_dify_runs_qa
                    ON chat_dify_runs(qa_id);
                """
            )
            # 幂等迁移：`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列。
            existing = {row["name"] for row in connection.execute("PRAGMA table_info(chat_dify_runs)")}
            if "snapshot_json" not in existing:
                connection.execute("ALTER TABLE chat_dify_runs ADD COLUMN snapshot_json TEXT")
            if "binding_key" not in existing:
                connection.execute(
                    "ALTER TABLE chat_dify_runs ADD COLUMN binding_key TEXT NOT NULL DEFAULT ''"
                )
            if "scope_json" not in existing:
                connection.execute(
                    "ALTER TABLE chat_dify_runs ADD COLUMN scope_json TEXT NOT NULL DEFAULT ''"
                )
            # 签发时冻结**正式版本号**：撤回/重新发布之后，回读靠它判断"这条 run 跑的那一版
            # 还是不是现在对外的那一版"。默认 0 = 文件登记（没有版本概念），不受影响。
            if "agent_version_no" not in existing:
                connection.execute(
                    "ALTER TABLE chat_dify_runs ADD COLUMN agent_version_no INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def _connection(self, *, write: bool = False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
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

    def issue(
        self,
        *,
        run_id: str,
        qa_id: str,
        scope_fingerprint: str,
        scope_json: str,
        binding_key: str,
        order_no: str,
        environment: str,
        tenant_id: str,
        app_id: str,
        workflow_id: str,
        grant_id: str,
        agent_version_no: int = 0,
    ) -> DifyRun:
        now = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO chat_dify_runs (
                    run_id, qa_id, scope_fingerprint, scope_json, binding_key, order_no,
                    environment, tenant_id, app_id, workflow_id, workflow_run_id, task_id,
                    grant_id, state, created_at, expires_at, agent_version_no
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    qa_id,
                    scope_fingerprint,
                    scope_json,
                    binding_key,
                    order_no,
                    environment,
                    tenant_id,
                    app_id,
                    workflow_id,
                    grant_id,
                    RUNNING,
                    now.isoformat(),
                    (now + timedelta(seconds=DIFY_RUN_TTL_SECONDS)).isoformat(),
                    agent_version_no,
                ),
            )
        record = self.get(run_id)
        assert record is not None
        return record

    def bind_workflow(self, run_id: str, *, workflow_run_id: str, task_id: str) -> bool:
        """把 Dify 自己给的两个 id 落库。**只写一次**：重写会让取消找错对象。"""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE chat_dify_runs SET workflow_run_id = ?, task_id = ?
                 WHERE run_id = ? AND (workflow_run_id IS NULL OR workflow_run_id = ?)
                   AND (task_id IS NULL OR task_id = ?)
                """,
                (workflow_run_id, task_id, run_id, workflow_run_id, task_id),
            )
            return cursor.rowcount == 1

    def get(self, run_id: str) -> DifyRun | None:
        """同一事务内收敛过期并读取。调用方以该次读取为判定的时点。"""
        with self._connection(write=True) as connection:
            _expire_in(connection, run_id)
            row = connection.execute("SELECT * FROM chat_dify_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row else None

    def peek(self, run_id: str) -> DifyRun | None:
        """**只读**：做过期收敛会是一次写，判"这条通知该不该落库"时不能用它。"""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM chat_dify_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row else None

    def running_for(
        self, *, tenant_id: str, environment: str, binding_key: str | None = None
    ) -> list[DifyRun]:
        """该租户/环境下**仍在运行**的执行记录（撤回要收敛的那一批）。

        `environment` 是逻辑执行环境，不是主机名：同一份数据里可能同时存在别的环境的
        记录（隔离栈与回归都会写同一个库），撤回只该收自己那一片。

        `binding_key` 把范围再收到**这一条路由**上。同一个租户可以同时有 consumer 与
        operator 两条路由，撤回其中一条不该把另一条的在飞运行也取消掉 —— 那会把一次
        正常的发布撤回动作变成另一条路由上的故障。
        """
        with self._connection() as connection:
            if binding_key is None:
                rows = connection.execute(
                    """
                    SELECT * FROM chat_dify_runs
                     WHERE tenant_id = ? AND environment = ? AND state = ?
                    """,
                    (tenant_id, environment, RUNNING),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM chat_dify_runs
                     WHERE tenant_id = ? AND environment = ? AND state = ? AND binding_key = ?
                    """,
                    (tenant_id, environment, RUNNING, binding_key),
                ).fetchall()
        return [_run_from_row(row) for row in rows]

    def by_qa(self, qa_id: str) -> DifyRun | None:
        """按 QA 反查承载它的执行记录。**只读**，不做过期收敛。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM chat_dify_runs WHERE qa_id = ? ORDER BY created_at DESC LIMIT 1",
                (qa_id,),
            ).fetchone()
        return _run_from_row(row) if row else None

    def store_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> bool:
        """把**这一轮**的业务快照落库。只写一次：第二次读业务会得到另一份。

        `snapshot_json IS NULL` 是条件的一部分，所以并发的第二个取数请求写不进来。
        """
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE chat_dify_runs SET snapshot_json = ? WHERE run_id = ? AND snapshot_json IS NULL",
                (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), run_id),
            )
            return cursor.rowcount == 1

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        """读这一轮已存的快照。**只读**；没有就是没有，不触发任何读取。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM chat_dify_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or not row["snapshot_json"]:
            return None
        loaded = json.loads(row["snapshot_json"])
        return loaded if isinstance(loaded, dict) else None

    def succeed(self, run_id: str) -> bool:
        """`running → succeeded`。**只要还有效**，且必须已经绑上 workflow run。"""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE chat_dify_runs SET state = ?, completed_at = ?
                 WHERE run_id = ? AND state = ? AND expires_at > ?
                   AND workflow_run_id IS NOT NULL
                """,
                (SUCCEEDED, _now().isoformat(), run_id, RUNNING, _now().isoformat()),
            )
            return cursor.rowcount == 1

    def fail(self, run_id: str, reason: str) -> bool:
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE chat_dify_runs SET state = ?, reason = ?, completed_at = ?
                 WHERE run_id = ? AND state = ?
                """,
                (FAILED, reason, _now().isoformat(), run_id, RUNNING),
            )
            return cursor.rowcount == 1

    def cancel(self, run_id: str, reason: str = "cancelled_by_caller") -> bool:
        """`running → cancelled`。**迟到结果不能把它覆盖回 succeeded**（见 `succeed` 的条件）。"""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE chat_dify_runs SET state = ?, reason = ?, completed_at = ?
                 WHERE run_id = ? AND state = ?
                """,
                (CANCELLED, reason, _now().isoformat(), run_id, RUNNING),
            )
            return cursor.rowcount == 1


def _expire_in(connection: sqlite3.Connection, run_id: str) -> None:
    """把已过期的 `running` 收敛成 `expired`。过期语义只此一处。"""
    connection.execute(
        """
        UPDATE chat_dify_runs SET state = ?, reason = COALESCE(reason, 'ttl_elapsed'),
                                  completed_at = COALESCE(completed_at, ?)
         WHERE run_id = ? AND state = ? AND expires_at <= ?
        """,
        (EXPIRED, _now().isoformat(), run_id, RUNNING, _now().isoformat()),
    )


def _run_from_row(row: sqlite3.Row) -> DifyRun:
    return DifyRun(
        run_id=row["run_id"],
        qa_id=row["qa_id"],
        scope_fingerprint=row["scope_fingerprint"],
        scope_json=row["scope_json"],
        binding_key=row["binding_key"],
        order_no=row["order_no"],
        environment=row["environment"],
        tenant_id=row["tenant_id"],
        app_id=row["app_id"],
        workflow_id=row["workflow_id"],
        workflow_run_id=row["workflow_run_id"],
        task_id=row["task_id"],
        grant_id=row["grant_id"],
        state=row["state"],
        reason=row["reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        completed_at=_ts(row["completed_at"]),
        agent_version_no=int(row["agent_version_no"] or 0),
    )


__all__ = [
    "CANCELLED",
    "DIFY_RUN_TTL_SECONDS",
    "DifyRun",
    "DifyRunError",
    "DifyRunStore",
    "EXPIRED",
    "FAILED",
    "RUNNING",
    "SUCCEEDED",
]
