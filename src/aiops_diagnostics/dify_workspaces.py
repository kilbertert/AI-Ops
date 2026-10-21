"""``dify_workspaces``：一个租户在**一个环境**里的 Dify workspace（ADR-0059 / #879）。

## 三件事，只有一起才有意义

1. **表**：``(tenant_id, environment) → workspace_id + setup_state``。唯一键是这一对，
   因为 41 与 95 是两套独立 UPMS、两套数据库，``tenant_id`` 各自生成 —— 撞号时只有
   ``environment`` 能分辨它们其实是两个租户。
2. **解析**：:meth:`DifyWorkspaces.resolve` **只查不建**，失败是**带码的异常**，
   不是一个空字符串。
3. **建**：不在这里。建 workspace 是开户那一步的显式动作（#880）；解析函数一旦会
   隐式建，"发布桥顺手建了一个 workspace"就成了没人打算做的事。

## 为什么失败不能是 ``""``

一个返回空串的解析函数让调用方把"这个租户没有 workspace"与"workspace_id 是空"混成
一件事，而后者在 Dify 上的含义是**平台根**：每一条 app 动作会落到别人的工作区。所以
这里只产出三种东西 —— 一个非空 ``workspace_id``、一个带码的异常、没有第四种。

## 三种终态与一种进行态（都不是死胡同）

| 状态 | 判据 | 解析给调用方 |
|---|---|---|
| ``ready`` / ``created`` | 有 ``workspace_id`` | 它就是答案 |
| ``creating`` | 有人正在建（租约可能已过期） | ``DIFY_WORKSPACE_CREATING``，**可重试** |
| ``failed`` | 明确失败（错误响应 / 连接被拒） | ``DIFY_WORKSPACE_FAILED``，要重建 |
| ``unknown`` | 结果未知（超时 / 建成没写回） | ``DIFY_WORKSPACE_UNKNOWN``，先对账 |

``creating`` 的租约（``lease_owner`` / ``lease_expires_at``）只在**写**的那一刻有意义：
抢占方必须先对账（#880）。解析方读到的仍是 ``CREATING`` —— 租约过期不代表没建成，
把一个可能已经存在的 workspace 报成"没有"才是会造孤儿的那一侧。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file

#: 一张行可能处于的全部状态。``created`` 与 ``ready`` 的区别在于**后续步骤是否跑完**
#: （插件、凭据、模板 DSL，#881）；对"这个租户的 workspace 是哪一个"这个问题，两者
#: 答案相同，所以解析都返回它的 id。
SETUP_STATES = ("creating", "created", "ready", "failed", "unknown")

#: 解析失败的结果码。**彼此可区分**是这一段的主要产出：调用方据此决定"稍后重试"
#: 还是"找人重建"还是"先对账"，而不是统一收到一个空值。
DIFY_WORKSPACE_UNRESOLVED = "DIFY_WORKSPACE_UNRESOLVED"
DIFY_WORKSPACE_CREATING = "DIFY_WORKSPACE_CREATING"
DIFY_WORKSPACE_FAILED = "DIFY_WORKSPACE_FAILED"
DIFY_WORKSPACE_UNKNOWN = "DIFY_WORKSPACE_UNKNOWN"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class DifyWorkspaceError(RuntimeError):
    """解析没有得到一个可用的 ``workspace_id``。

    ``code`` 稳定、非本地化；``http_status`` 是**唯一**一份状态映射 —— 网关的只读端点
    与未来的进程内调用方都读它，而不是各自再判一次码。
    """

    def __init__(self, code: str, message: str, *, http_status: int, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable


class DifyWorkspaces:
    """The mapping table, sharing the gateway SQLite file (like ``DifyAppRegistry``)."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                -- 一个租户在一个环境里的 Dify workspace（ADR-0059 / #879）。
                --
                -- 唯一键是 (tenant_id, environment)：两套独立 UPMS 的 tenant_id 各自
                -- 生成，只有环境列能分辨撞号的两个人。`dify_app_registry` **不动** ——
                -- 它记的是"哪个 app 服务哪个入口"，与"租户的工作区是哪个"是两个问题。
                --
                -- workspace_id 允许为空：`creating` 行在 workspace 建出来之前就是空的，
                -- 而"先占位、再建"正是唯一能把同一租户的两次首次登录串行化的顺序。
                CREATE TABLE IF NOT EXISTS dify_workspaces (
                    tenant_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT '',
                    setup_state TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, environment)
                );
                """
            )
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            if write:
                connection.commit()
        finally:
            connection.close()

    # ── reads ─────────────────────────────────────────────────────────

    def resolve(self, tenant_id: str, environment: str) -> str:
        """``(tenant_id, environment)`` → 那个 workspace 的 id，或一个带码的拒绝。

        **只查不建。** 缺行不是"顺手建一个"的时机 —— 建只属于开户那一步（#880），
        发布桥 / 试运行 / 运维 CLI 在缺行时只能拒绝（ADR-0059 §5.1）。
        """
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT workspace_id, setup_state, last_error FROM dify_workspaces"
                " WHERE tenant_id = ? AND environment = ?",
                (tenant_id, environment),
            ).fetchone()
        if row is None:
            raise DifyWorkspaceError(
                DIFY_WORKSPACE_UNRESOLVED,
                f"租户 {tenant_id} 在环境 {environment} 里还没有 workspace",
                http_status=404,
            )
        state = str(row["setup_state"])
        workspace_id = str(row["workspace_id"] or "")
        if state in {"created", "ready"}:
            if not workspace_id:
                # 状态说"建好了"却没有 id —— 这一行不可用，按"没有"回答。
                # 不编一个 id，也不把它当成已迁移：那会让调用方把动作打到平台根。
                raise DifyWorkspaceError(
                    DIFY_WORKSPACE_UNRESOLVED,
                    f"租户 {tenant_id} 在环境 {environment} 里的映射行没有 workspace_id",
                    http_status=404,
                )
            return workspace_id
        if state == "creating":
            raise DifyWorkspaceError(
                DIFY_WORKSPACE_CREATING,
                f"租户 {tenant_id} 在环境 {environment} 里的 workspace 正在创建，稍后重试",
                http_status=409,
                retryable=True,
            )
        if state == "failed":
            detail = str(row["last_error"] or "")
            raise DifyWorkspaceError(
                DIFY_WORKSPACE_FAILED,
                f"租户 {tenant_id} 在环境 {environment} 里的 workspace 创建失败"
                + (f"：{detail}" if detail else "")
                + "；可重建",
                http_status=409,
            )
        if state == "unknown":
            raise DifyWorkspaceError(
                DIFY_WORKSPACE_UNKNOWN,
                f"租户 {tenant_id} 在环境 {environment} 里的 workspace 创建结果未知；先对账再重建",
                http_status=409,
            )
        raise DifyWorkspaceError(
            DIFY_WORKSPACE_UNRESOLVED,
            f"租户 {tenant_id} 在环境 {environment} 里的映射行状态不可识别：{state!r}",
            http_status=404,
        )

    def exists(self, tenant_id: str, environment: str) -> bool:
        """这一对有没有行 —— 与 :meth:`resolve` **不是**同一个问题。

        ``creating`` / ``failed`` / ``unknown`` 都算"有行"。开户那一步（#880）需要这个
        区别：有行且不是终态时不能另建一个，与"能不能拿来用"是两件事。
        """
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM dify_workspaces WHERE tenant_id = ? AND environment = ?",
                (tenant_id, environment),
            ).fetchone()
        return row is not None


def _identifier(value: str, name: str) -> str:
    candidate = (value or "").strip()
    if not _SAFE_ID.fullmatch(candidate):
        raise ValueError(f"{name} contains unsupported characters")
    return candidate


def now() -> datetime:
    """The module's clock, named so a lease test can reason about it."""
    return datetime.now(UTC)
