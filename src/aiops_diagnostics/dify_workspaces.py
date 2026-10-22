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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
                    -- 建的时候用的**派生名**。对账按这一列，不按当前派生值 ——
                    -- 名字里带公司名之后，公司改名会让"当前派生值"变化，而对账必须按
                    -- 建的时候那个名字去 Dify 里找，否则会命中 0 ⇒ 重建 ⇒ 永久孤儿。
                    workspace_name TEXT NOT NULL DEFAULT '',
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
            # 老库补列：`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列。
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(dify_workspaces)")}
            if "workspace_name" not in columns:
                connection.execute(
                    "ALTER TABLE dify_workspaces ADD COLUMN workspace_name TEXT NOT NULL DEFAULT ''"
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

    def row(self, tenant_id: str, environment: str) -> dict[str, Any] | None:
        """整行，给开户那一步按状态分支（``resolve`` 只回答"能不能用"）。"""
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection() as connection:
            found = connection.execute(
                "SELECT * FROM dify_workspaces WHERE tenant_id = ? AND environment = ?",
                (tenant_id, environment),
            ).fetchone()
        return dict(found) if found is not None else None

    def list_for_environment(self, environment: str) -> list[dict[str, Any]]:
        """Every workspace row in one environment, for the triple check (#927).

        Read-only and unopinionated: what counts as "usable" is the caller's
        judgement (``setup_state``), because that judgement differs by caller —
        onboarding repairs ``failed`` rows, the triple check reports them.
        """
        environment = _identifier(environment, "environment")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM dify_workspaces WHERE environment = ? ORDER BY tenant_id",
                (environment,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── writes（开户那一步用，#880） ──────────────────────────────────

    def claim(
        self,
        tenant_id: str,
        environment: str,
        *,
        owner: str,
        lease_seconds: float,
        allow_unknown: bool = False,
    ) -> bool:
        """**先占位**：原子地拿到"由我来建"的权利，成功返回 True。

        顺序是这张表存在的全部理由（ADR-0059 §5.1）：同一个租户的两次首次登录可以同时
        越过"表里没有行"这个检查，只有 ``UNIQUE(tenant_id, environment)`` + 先插入能把它们
        串行化。反过来（先建后写）两个请求各建一个，后写的赢，前一个成为**永久孤儿** ——
        Dify 没有删 tenant 的 API。

        三种"本来就该我接着做"的情形返回 True（并接管租约）：

        * 没有行 ⇒ 插入 ``creating``；
        * ``failed`` ⇒ 重建（上一轮明确失败，``workspace_id`` 保持空）；
        * ``creating`` 且**租约已过期** ⇒ 接管 —— 创建进程崩在中间，没有租约这条会永远卡住。

        三种"别人正在做或已经做好"返回 False：``created`` / ``ready`` / 租约未过期的
        ``creating``。``unknown`` **默认也返回 False**：它要先按名字对账，只有对账发现
        "Dify 里确实没有"之后（``allow_unknown=True``）才可以接管 —— 直接抢会把一个已经
        建成的 workspace 再建一遍。
        """
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        now = now_utc()
        expires = now + timedelta(seconds=max(1.0, lease_seconds))
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT setup_state, lease_expires_at FROM dify_workspaces"
                " WHERE tenant_id = ? AND environment = ?",
                (tenant_id, environment),
            ).fetchone()
            if existing is None:
                try:
                    connection.execute(
                        "INSERT INTO dify_workspaces (tenant_id, environment, workspace_id, setup_state,"
                        " lease_owner, lease_expires_at, last_error, created_at, updated_at)"
                        " VALUES (?, ?, '', 'creating', ?, ?, '', ?, ?)",
                        (tenant_id, environment, owner, _iso(expires), _iso(now), _iso(now)),
                    )
                except sqlite3.IntegrityError:
                    # 另一个进程在同一瞬间插入了 —— 它就是"正在建"，不是我。
                    return False
                return True
            state = str(existing["setup_state"])
            lease = existing["lease_expires_at"]
            if state in {"created", "ready"}:
                return False
            if state == "unknown" and not allow_unknown:
                return False
            if state == "creating" and lease and _parse_time(str(lease)) > now:
                return False
            # failed，或租约已过期的 creating：接管。workspace_id 保持原值 ——
            # 已过期的 creating 可能已经建成了，对账（调用方）来决定要不要补 UUID。
            connection.execute(
                "UPDATE dify_workspaces SET setup_state = 'creating', lease_owner = ?,"
                " lease_expires_at = ?, updated_at = ? WHERE tenant_id = ? AND environment = ?",
                (owner, _iso(expires), _iso(now), tenant_id, environment),
            )
        return True

    def mark_created(self, tenant_id: str, environment: str, workspace_id: str, *, name: str = "") -> None:
        """建成之后**立刻**写回 id —— 这一行与 Dify 的现状之间不留空窗。

        ``name`` 是**这次建的时候用的派生名**。写进表里，以后对账按它查：公司改名之后
        派生值会变，而 Dify 里那个 workspace 还叫旧名。
        """
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE dify_workspaces SET workspace_id = ?, workspace_name = ?,"
                " setup_state = 'created', last_error = '', lease_expires_at = NULL, updated_at = ?"
                " WHERE tenant_id = ? AND environment = ?",
                (
                    _identifier(workspace_id, "workspace_id"),
                    name.strip(),
                    _iso(now_utc()),
                    tenant_id,
                    environment,
                ),
            )

    def mark_failed(self, tenant_id: str, environment: str, *, error: str) -> None:
        """**明确失败**：Dify 没有建出来。``workspace_id`` 保持空 ⇒ 下一次开户可以重建。"""
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE dify_workspaces SET setup_state = 'failed', last_error = ?,"
                " lease_expires_at = NULL, updated_at = ? WHERE tenant_id = ? AND environment = ?",
                (error[:500], _iso(now_utc()), tenant_id, environment),
            )

    def mark_ready(self, tenant_id: str, environment: str) -> None:
        """三步都跑完（#881）。``created`` → ``ready`` 是**单行推进**，不可回退。

        为什么需要这个区别：``created`` 是"workspace 建出来了"，``ready`` 是"它能真的
        跑一次对话"。用户撞上 ``created`` 时该看到"正在配置"，撞上 ``ready`` 才该看到
        正常工作。
        """
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE dify_workspaces SET setup_state = 'ready', last_error = '', updated_at = ?"
                " WHERE tenant_id = ? AND environment = ?",
                (_iso(now_utc()), tenant_id, environment),
            )

    def mark_template_failed(self, tenant_id: str, environment: str, *, error: str) -> None:
        """**模板那一步**失败了，但 workspace 与凭据都还在（#881 / ADR-0059 §5.1）。

        与 :meth:`mark_failed` 的区别是**状态停在 ``created``**：这个 workspace 是能用的
        （人进得去、能改），只是模板没导进来。写成 ``failed`` 会让解析函数把它当成
        "没建出来"，而下一次 ``ensure-workspace`` 就会**重建**一个永久孤儿。
        """
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection(write=True) as connection:
            # ``AND setup_state <> 'ready'``：``created → ready`` 是单向的（见
            # :meth:`mark_ready`）。少了这个条件，一次**重跑**里的读取抖动会把一个已经
            # 就绪的 workspace 降级回 ``created``，而 ``ensure-workspace`` 对 ``ready``
            # 行是短路返回的 —— 没有人会再把它推回去。错误照样记在 ``last_error`` 里。
            # 状态**只降不升**：已经是 ``ready`` 的行保持 ``ready``，但原因照样记下来
            # （用 CASE 而不是整条 WHERE —— 少写 last_error 会让"为什么这次重跑失败"
            # 在库里一点痕迹都没有）。
            connection.execute(
                "UPDATE dify_workspaces SET setup_state ="
                " CASE WHEN setup_state = 'ready' THEN 'ready' ELSE 'created' END,"
                " last_error = ?, updated_at = ? WHERE tenant_id = ? AND environment = ?",
                (error[:500], _iso(now_utc()), tenant_id, environment),
            )

    def mark_unknown(self, tenant_id: str, environment: str, *, error: str) -> None:
        """**结果未知**：``workspace_id`` 原样保留（可能已经建成），下一次先对账。"""
        tenant_id = _identifier(tenant_id, "tenant_id")
        environment = _identifier(environment, "environment")
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE dify_workspaces SET setup_state = 'unknown', last_error = ?,"
                " lease_expires_at = NULL, updated_at = ? WHERE tenant_id = ? AND environment = ?",
                (error[:500], _iso(now_utc()), tenant_id, environment),
            )


#: 平台租户的**哨兵** tenant_id（#882）。公司 `sys_user.type == -1` 的账号不属于任何
#: `sys_organ`，所以它没有真实的租户 id；用这个值在 `dify_workspaces` 里占一行。
#:
#: 为什么不用空串：空串在解析路径上会被 `_identifier` 当成非法值（那是刻意的 —— 空
#: workspace/空租户从来不是"没有"的表达）。而平台账号**确实**需要一个自己的 workspace，
#: 所以它有一个**明确的**、不会被真实 `sys_organ.id` 命中的哨兵值。
PLATFORM_TENANT_ID = "platform"


def workspace_name(environment: str, tenant_id: str, *, company: str = "") -> str:
    """workspace 的**派生名**：``aiops-{environment}-{tenant_id}``，可带公司名。

    ``tenants.name`` 在 Dify 里**没有唯一约束**（只有 ``tenant_pkey``），所以唯一性来自
    这条命名约定，不是 Dify 保证的。

    ``company`` 只影响**建的那一刻**取什么名字。取到之后写进
    ``dify_workspaces.workspace_name``，**以后对账一律按那一列** —— 不按当前派生值。
    理由是公司会改名：`Ulink` 改成别的之后派生值变了，而 Dify 里那个 workspace 还叫旧名；
    按当前派生值对账会命中 0 ⇒ 当"没建过"⇒ 重建 ⇒ 永久孤儿（Dify 没有删 workspace 的 API）。

    Dify 侧的名字用 ``{id 后 8 位}`` 而不是全 id：全 id 有 19 位，在切换器里会被截断成
    ``aiops-41-194210547659886...``（实测），人看不出是谁。
    """
    environment_part = _identifier(environment, "environment")
    tenant_part = _identifier(tenant_id, "tenant_id")
    readable = _readable_company(company)
    tail = tenant_part[-8:]
    if readable:
        return f"aiops-{environment_part}-{readable}-{tail}"
    return f"aiops-{environment_part}-{tenant_part}"


#: Dify 的 ``tenants.name`` 是普通字符串列，但控制台与切换器都按单行短文本渲染。
#: 这里把公司名压成一段可读的 slug：只留字母数字与 `-`，其余折成 `-`，长度封顶。
_MAX_COMPANY_SLUG = 24


def _readable_company(company: str) -> str:
    """公司名 → 可读 slug。空 ⇒ 空串（退回纯 id 命名）。"""
    import re
    import unicodedata

    text = unicodedata.normalize("NFKC", (company or "").strip())
    if not text:
        return ""
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", text).strip("-")
    return slug[:_MAX_COMPANY_SLUG].strip("-")


def _identifier(value: str, name: str) -> str:
    candidate = (value or "").strip()
    if not _SAFE_ID.fullmatch(candidate):
        raise ValueError(f"{name} contains unsupported characters")
    return candidate


def now_utc() -> datetime:
    """The module's clock, named so a lease test can reason about it."""
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
