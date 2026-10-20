"""把一次**已授权**的读取交给可信执行节点：记录、冻结身份与节点绑定。

授权留在 Gateway。外部编排平台与执行节点只拿到一个高熵 ``run_id``，
**知道它本身不授予任何读取权限**：节点要用自己的独立凭据回呼领取端点，Gateway 先认节点、
核对绑定，再用记录里冻结的身份**重新做一次订单授权**，然后才读业务。

本模块管四件事：节点/环境绑定配置、请求级授权记录及其生命周期、冻结身份的序列化，
以及调用编排平台的那一跳。
HTTP 路由在 ``gateway_api``，授权读取规则与公共
``/v1/orders/{order_no}/charging-status`` 共用同一组函数（不在这里另造一份）。

不在本期：跨节点重选/负载均衡、续期、分布式队列、HMAC/JWT 票据格式。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aiops_diagnostics.bounded_http import ErrorMapping, HttpFailure, RequestSpec, request_json
from aiops_diagnostics.gateway_config import canonical_gateway_url
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

#: 授权记录的有效期（契约 §4：固定 180 秒，不续期；到期须从已认证入口重新授权）。
RUN_TTL_SECONDS = 180
OPERATION_CHARGING_STATUS = "charging_status"

ISSUED = "issued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
REVOKED = "revoked"
EXPIRED = "expired"

#: 智能体发布版本还没有接线（M1/M3 的追溯缺口）。回执里显式写出缺口，
#: 而不是留一个 null 让人读成「字段齐全」。
#: M3：执行配置身份。签发时固定**期望**，领取时落入**实际**，两者都只来自受控来源。
EXEC_CONFIG_COLUMNS = (
    "expected_agent_version",
    "observed_agent_version",
    "exec_config_state",
)

#: 未接线时的回执取值（与今天逐字一致）。
EXEC_CONFIG_NOT_WIRED = "not_wired"

#: 已固定期望、但从未核对一致（含漂移与「还没领过」）：不冒充一个有效版本。
EXEC_CONFIG_NOT_CONFIRMED = "not_confirmed"

#: 身份漂移写进 ``reason`` —— 与其它拒绝原因同一个落点，不另造原因列。
EXEC_CONFIG_DRIFT = "exec_config_drift"

#: 终态集合：这些状态下的记录不复活，也不因补审计被改写。
TERMINAL_STATES = frozenset({FAILED, REVOKED, EXPIRED})

#: ``exec_config_state`` 的三个取值。``expected`` 只说明「签发时固定了期望」，
#: **不**说明核对过 —— 后者是 ``matched``。两者混用会让一个从未核对过的记录
#: 在回执里看起来像已验证。
EXEC_CONFIG_EXPECTED = "expected"
EXEC_CONFIG_MATCHED = "matched"
EXEC_CONFIG_MISMATCHED = "mismatched"


def _migrate_execution_config_columns(connection: sqlite3.Connection) -> None:
    """给已有的库补 M3 的三列。

    ``CREATE TABLE IF NOT EXISTS`` **不会**给已存在的表加列 —— 所以列变更必须显式迁移，
    不能只改建表语句（那会让人以为"新库有、旧库也有"）。
    幂等：已有的列跳过；旧行保持 ``NULL``，**不回填、不猜**。
    """
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(agent_runs)")}
    for column in EXEC_CONFIG_COLUMNS:
        if column not in existing:
            connection.execute(f"ALTER TABLE agent_runs ADD COLUMN {column} TEXT")


AGENT_VERSION_GAP = "not_wired"


class AgentRunConfigError(ValueError):
    pass


class AgentRunDispatchError(RuntimeError):
    """编排平台那一跳失败；正文不向调用方转述。"""


# --------------------------------------------------------------------------
# 配置：环境 → appId/agentId/节点，以及节点凭据
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionNode:
    node_id: str
    environment: str
    active: bool


@dataclass(frozen=True, slots=True)
class AgentRunConfig:
    """可信服务端的执行绑定。**客户端、模型、平台都不能影响其中任何一项。**

    本期一个环境一个执行节点（契约 §3.2）；``nodes`` 可以列出多个，但只有
    ``expected_node_id`` 指向的那一个能领取本环境的记录 —— 同环境的另一个节点
    也领不到（这条由测试钉住，证明比对的是节点而不只是环境）。
    """

    environment: str
    snail_base_url: str
    snail_app_id: str
    snail_app_token: str
    snail_agent_id: int
    snail_open_id: str
    expected_node_id: str
    node_token_hashes: dict[str, ExecutionNode]

    @classmethod
    def load(cls, path: Path) -> AgentRunConfig:
        """每次调用都重读：撤销节点就是改这个文件，下一次领取立刻生效。"""
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise AgentRunConfigError("agent run config is unavailable") from exc
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            # 里面有平台应用令牌与节点凭据的摘要：与 production.env 同一条纪律。
            raise AgentRunConfigError("agent run config must not be group/other accessible")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            nodes: dict[str, ExecutionNode] = {}
            for item in raw["nodes"]:
                digest = item["token_sha256"]
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
                    raise AgentRunConfigError("node token digest must be sha256 hex")
                digest = digest.lower()
                if digest in nodes:
                    raise AgentRunConfigError("node token digest must be unique")
                active = item.get("active", True)
                if not isinstance(active, bool):
                    raise AgentRunConfigError("node active must be a JSON boolean")
                nodes[digest] = ExecutionNode(
                    node_id=str(item["node_id"]),
                    environment=str(item["environment"]),
                    active=active,
                )
            config = cls(
                environment=str(raw["environment"]),
                snail_base_url=canonical_gateway_url(str(raw["snail_base_url"])),
                snail_app_id=str(raw["snail_app_id"]),
                snail_app_token=str(raw["snail_app_token"]),
                snail_agent_id=int(raw["snail_agent_id"]),
                snail_open_id=str(raw["snail_open_id"]),
                expected_node_id=str(raw["expected_node_id"]),
                node_token_hashes=nodes,
            )
        except AgentRunConfigError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise AgentRunConfigError("agent run config is malformed") from exc
        if not any(node.node_id == config.expected_node_id for node in nodes.values()):
            raise AgentRunConfigError("expected node is not registered")
        return config

    def authenticate_node(self, token: str) -> ExecutionNode | None:
        """节点身份**只**从凭据派生；回呼里自报的 node/host 字段不参与。"""
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for known, node in self.node_token_hashes.items():
            if secrets.compare_digest(known, digest):
                return node if node.active else None
        return None


# --------------------------------------------------------------------------
# 冻结身份：记录里存的是建记录那一刻的 ScopeContext
# --------------------------------------------------------------------------


def _subject_dict(subject: SubjectRecord) -> dict[str, Any]:
    return {
        "b_user_id": subject.b_user_id,
        "c_user_id": subject.c_user_id,
        "username": subject.username,
        "tenant_id": subject.tenant_id,
        "organ_id": subject.organ_id,
        "shop_id": subject.shop_id,
        "b_subject_reason": subject.b_subject_reason,
        "user_type": subject.user_type,
    }


def scope_to_json(context: ScopeContext) -> str:
    return json.dumps(
        {
            "caller": _subject_dict(context.caller),
            "subject": _subject_dict(context.subject),
            "delegated": context.delegated,
            "effective_tenant_id": context.effective_tenant_id,
            "data_scope": {
                "type": context.data_scope.type,
                "organ_ids": list(context.data_scope.organ_ids),
                "shop_ids": list(context.data_scope.shop_ids),
                "site_ids": list(context.data_scope.site_ids),
            },
            "roles": sorted(context.roles),
            "permissions": sorted(context.permissions),
            "resolved_at": context.resolved_at.isoformat(),
            "scope_fingerprint": context.scope_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def scope_from_json(raw: str) -> ScopeContext:
    """重建时校验已存指纹与范围语义；摘要不提供对数据库写入者的认证。"""
    data = json.loads(raw)
    fingerprint = data["scope_fingerprint"]
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("stored scope fingerprint must be sha256 hex")
    scope = data["data_scope"]
    return ScopeContext(
        caller=SubjectRecord(**data["caller"]),
        subject=SubjectRecord(**data["subject"]),
        delegated=bool(data["delegated"]),
        effective_tenant_id=str(data["effective_tenant_id"]),
        data_scope=DataScope(
            type=scope["type"],
            organ_ids=tuple(scope["organ_ids"]),
            shop_ids=tuple(scope["shop_ids"]),
            site_ids=tuple(scope["site_ids"]),
        ),
        roles=frozenset(data["roles"]),
        permissions=frozenset(data["permissions"]),
        resolved_at=datetime.fromisoformat(data["resolved_at"]),
        scope_fingerprint=fingerprint,
    )


# --------------------------------------------------------------------------
# 请求级授权记录
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRun:
    run_id: str
    scope_fingerprint: str
    scope_json: str
    order_no: str
    operation: str
    environment: str
    app_id: str
    agent_id: str
    expected_node_id: str
    state: str
    reason: str | None
    snapshot: dict[str, Any] | None
    actual_node_id: str | None
    node_build: str | None
    expected_agent_version: str | None
    observed_agent_version: str | None
    exec_config_state: str | None
    gateway_version: str
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None

    @property
    def exec_config_confirmed(self) -> bool:
        """核对过**且**一致。``expected`` 只说明签发时固定了期望，不是核对。"""
        return (
            self.exec_config_state == EXEC_CONFIG_MATCHED
            and self.observed_agent_version is not None
            and self.observed_agent_version == self.expected_agent_version
        )

    def receipt(self) -> dict[str, Any]:
        """执行回执：全部字段来自服务端记录与认证结果，没有一个取自模型输出。

        ``agent_version`` 只在**核对一致**之后才给出取值。此前只要存在期望就回填它，
        于是一条漂移被拒的记录在回执里看起来像「跑在这个版本上」—— 那不是缺口标注，
        是把未核实的东西写成了已核实。
        """
        digest = (
            hashlib.sha256(json.dumps(self.snapshot, sort_keys=True).encode("utf-8")).hexdigest()
            if self.snapshot is not None
            else None
        )
        receipt: dict[str, Any] = {
            "run_id": self.run_id,
            "state": self.state,
            "reason": self.reason,
            "operation": self.operation,
            "scope_fingerprint": self.scope_fingerprint,
            "expected_environment": self.environment,
            "expected_app_id": self.app_id,
            "expected_agent_id": self.agent_id,
            "expected_node_id": self.expected_node_id,
            # 没有节点回呼时为 null —— 不用预期节点填充（契约 §5）。
            "actual_node_id": self.actual_node_id,
            # 节点声明的构建标识：受信节点的声明，不是镜像/硬件远程证明。
            "node_build": self.node_build,
            "gateway_version": self.gateway_version,
        }
        if self.expected_agent_version is not None:
            # 启用清单后新增的两个键。未启用时**一个都不加**：回执字段集合与今天逐字一致，
            # 这是 GW-04 的可机械判定形式（键集合相等，不是「看起来没变」）。
            receipt["agent_version_observed"] = self.observed_agent_version
        receipt.update(
            {
                # 期望与实际都来自**记录**（签发时固定 / 领取时对照），没有一个取自模型输出。
                "agent_version": (self.expected_agent_version if self.exec_config_confirmed else None),
                "agent_version_gap": self._version_gap(),
                "result_sha256": digest,
                "created_at": self.created_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
                "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            }
        )
        return receipt

    def _version_gap(self) -> str | None:
        if self.expected_agent_version is None:
            return EXEC_CONFIG_NOT_WIRED
        return None if self.exec_config_confirmed else EXEC_CONFIG_NOT_CONFIRMED


def _now() -> datetime:
    return datetime.now(UTC)


def _expire_in(connection: sqlite3.Connection, run_id: str) -> None:
    """把已过期的非终态收敛为 ``expired``。**过期语义只此一处**。

    ``expires_at <= now`` 即过期（与 ``claim``/``complete`` 的 ``expires_at > now`` 互补）。
    抽成函数是为了让「读路径顺手收敛」与「放行事务内核对」用同一条语句 ——
    复制一份判据是两套语义的开始。
    """
    connection.execute(
        """
        UPDATE agent_runs SET state = ?, reason = COALESCE(reason, 'ttl_elapsed')
         WHERE run_id = ? AND state IN (?, ?, ?) AND expires_at <= ?
        """,
        (EXPIRED, run_id, ISSUED, RUNNING, SUCCEEDED, _now().isoformat()),
    )


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class AgentRunStore:
    """授权记录与其生命周期。与 Gateway 共用同一个 SQLite 文件，各自建表。

    状态只有一条单向路径 ``issued → running → succeeded``，以及三个终态
    ``failed`` / ``revoked`` / ``expired``。每一次状态变化都是一条带条件的 UPDATE，
    靠受影响行数判定谁赢 —— 这就是「原子领取」，不需要锁表或队列。

    **重启不改写任何状态**：上一进程留下的 ``running`` 仍是 ``running``，绝不变回
    ``issued``（那会让同一笔授权被读第二次）。它会在有效期后变成 ``expired``。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    scope_fingerprint TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    order_no TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    expected_node_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT,
                    snapshot_json TEXT,
                    actual_node_id TEXT,
                    node_build TEXT,
                    gateway_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_scope
                    ON agent_runs(scope_fingerprint, created_at DESC);
                """
            )
            _migrate_execution_config_columns(connection)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
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
        scope: ScopeContext,
        order_no: str,
        config: AgentRunConfig,
        gateway_version: str,
        expected_agent_version: str | None = None,
    ) -> AgentRun:
        # 32 字节随机；只作关联编号，不承载任何身份或范围。
        run_id = "run_" + secrets.token_urlsafe(32)
        now = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, scope_fingerprint, scope_json, order_no, operation,
                    environment, app_id, agent_id, expected_node_id, state,
                    gateway_version, created_at, expires_at,
                    expected_agent_version, exec_config_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scope.scope_fingerprint,
                    scope_to_json(scope),
                    order_no,
                    OPERATION_CHARGING_STATUS,
                    config.environment,
                    config.snail_app_id,
                    str(config.snail_agent_id),
                    config.expected_node_id,
                    ISSUED,
                    gateway_version,
                    now.isoformat(),
                    (now + timedelta(seconds=RUN_TTL_SECONDS)).isoformat(),
                    expected_agent_version,
                    (None if expected_agent_version is None else EXEC_CONFIG_EXPECTED),
                ),
            )
        run = self.get(run_id)
        assert run is not None
        return run

    def get(self, run_id: str) -> AgentRun | None:
        """读一条记录；读的同时把已过期的非终态收敛为 ``expired``。"""
        self._expire(run_id)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row else None

    def peek(self, run_id: str) -> AgentRun | None:
        """**只读**一条记录，不做任何过期收敛 —— 判据是「这条通知该不该落库」时用它。

        ``get()`` 会顺手把过期的非终态收敛成 ``expired``，那对读路径是对的（调用方要看到
        真实状态），但它是一次写。完成端点先用 ``get()`` 做绑定判定时，那次收敛会把一条
        **未追踪**的历史行从 ``issued`` 改成 ``expired`` —— 与「未追踪记录一个列都不写」相反。
        """
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row else None

    def _expire(self, run_id: str) -> None:
        with self._connection(write=True) as connection:
            _expire_in(connection, run_id)

    def claim(self, run_id: str) -> bool:
        """``issued → running``。返回 True 的那一个调用者是唯一被允许读业务的人。"""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs SET state = ?, claimed_at = ?
                 WHERE run_id = ? AND state = ? AND expires_at > ?
                """,
                (RUNNING, _now().isoformat(), run_id, ISSUED, _now().isoformat()),
            )
            return cursor.rowcount == 1

    def record_observed_config(self, run_id: str, observed_agent_version: str) -> bool:
        """领取时把节点回传的**实际**身份落库（仅供审计，不覆盖签发时的期望）。

        条件写：只有记录仍处于可放行状态才写。此前的版本不带任何 `WHERE` 条件，
        于是**终态之后仍能写进 `matched`** —— 记录已经被别人拒绝成 `failed`，
        这边却把「核对一致」盖上去，两个字段自相矛盾。
        """
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                   SET observed_agent_version = ?, exec_config_state = ?
                 WHERE run_id = ? AND state NOT IN (?, ?, ?)
                """,
                (
                    observed_agent_version,
                    EXEC_CONFIG_MATCHED,
                    run_id,
                    FAILED,
                    REVOKED,
                    EXPIRED,
                ),
            )
            return cursor.rowcount == 1

    def confirm_exec_config(
        self, run_id: str, observed: str | None, *, actual_node_id: str | None = None
    ) -> AgentRun | None:
        """**一个写事务里**完成身份核对、记录写入，并把要放行的对象读回来。

        为什么必须是这个方法而不是「先读记录、再 `refuse()`、再返回早先读到的对象」：
        放行的判据（身份相等）与放行依据的状态（``succeeded``）如果来自两次独立读，
        中间任何一次别的请求的拒绝/撤销提交进来，我们就会拿着**陈旧的成功对象**把
        快照交出去 —— 这正是复现里 ``200 + 快照``、而库里已经是 ``failed`` 的交错。

        返回值语义：
        - ``None`` ⇒ 记录已不存在（被删或从未创建），调用方按「找不到」处理。
        - 身份不一致 ⇒ 已把记录钉成 ``failed`` + ``reason``、留下实际值供审计，然后返回
          **None**（不放行且不区分原因，避免把记录状态当放行信号）。
        - 记录已是终态（别人钉的 ``failed``/``revoked``，或本事务刚按 ``expires_at <= now``
          收敛的 ``expired``）⇒ **原样返回该记录**，由调用方按既有终态语义拒绝。
          用「执行配置漂移」去描述一条过期或被撤销的记录是错的原因码。
        - 其余 ⇒ **本事务内**写入之后读到的记录，调用方必须用它放行，
          而不是之前读到的那一份。
        - 未启用清单（没有期望）⇒ 原样返回记录，不写任何列，行为与今天逐字一致。

        **有效期必须在这里查**：拿记录的那一刻没有过期，不代表放行的这一瞬间没有。
        `get()` 只会在**读**的时候顺手收敛，而放行的判据是本事务内的这一份 ——
        只查 ``state`` 不查 ``expires_at``，就等于把「上一次读时还有效」当成「现在仍有效」。
        实测：把时钟推过到期点后，首次领取、成功重试、竞争后放行三条路径都返回了
        200 + 业务正文，而库里状态还是 ``succeeded``。
        """
        with self._connection(write=True) as connection:
            # 先按既有语义收敛过期，再读：这样「过期」与「被别人拒绝」走同一条读取路径，
            # 不需要在下面另写一个 if。
            _expire_in(connection, run_id)
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            run = _run_from_row(row)
            if run.expected_agent_version is None:
                # 未启用：原样返回，不写任何列，行为与今天逐字一致（终态由调用方按既有语义拒绝）。
                return run
            if run.state in TERMINAL_STATES:
                # 已过期（上面刚收敛）或别人钉的 failed/revoked：不放行、不改写、不覆盖原因。
                # 返回记录本身，由调用方按**既有**终态语义拒绝（`AGENT_RUN_NOT_ACTIVE`）——
                # 用「执行配置漂移」描述一条被撤销的记录是错的原因码。
                return run
            if observed != run.expected_agent_version:
                # 漂移：**已签发、已领取、已有成功快照**三种状态都要落终态。早先只调
                # 只认 ``succeeded`` 的 ``refuse()``，于是 ``issued``/``running`` 的记录
                # 拒绝之后仍是可领取的，换正确摘要再来一次就 200。
                # 已是终态的记录不在此列：不改写、不复活、不覆盖既有原因。
                connection.execute(
                    """
                    UPDATE agent_runs
                       SET state = ?, reason = ?, observed_agent_version = ?,
                           exec_config_state = ?, completed_at = ?,
                           actual_node_id = COALESCE(actual_node_id, ?)
                     WHERE run_id = ? AND state IN (?, ?, ?)
                    """,
                    (
                        FAILED,
                        EXEC_CONFIG_DRIFT,
                        observed,
                        EXEC_CONFIG_MISMATCHED,
                        _now().isoformat(),
                        actual_node_id,
                        run_id,
                        ISSUED,
                        RUNNING,
                        SUCCEEDED,
                    ),
                )
                return None
            updated = connection.execute(
                """
                UPDATE agent_runs
                   SET observed_agent_version = ?, exec_config_state = ?
                 WHERE run_id = ? AND state NOT IN (?, ?, ?)
                """,
                (
                    observed,
                    EXEC_CONFIG_MATCHED,
                    run_id,
                    FAILED,
                    REVOKED,
                    EXPIRED,
                ),
            )
            if updated.rowcount != 1:
                # 核对通过但没写进去：记录在读到写之间变成了终态。写不进去就是不放行，
                # 返回那条记录让调用方按既有终态语义拒绝（而不是谎报成漂移）。
                latest = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
                return _run_from_row(latest) if latest else None
            confirmed = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            return _run_from_row(confirmed) if confirmed else None

    def complete(
        self,
        run_id: str,
        *,
        snapshot: dict[str, Any],
        actual_node_id: str,
        node_build: str | None,
    ) -> bool:
        """``running`` + 快照。**是否同时转 ``succeeded`` 取决于这条记录有没有期望身份。**

        启用执行配置清单的 run（``expected_agent_version`` 非空）在这里**只保存快照、
        保持 ``running``**：快照存在＝节点已经取到业务数据，但「工具领取成功」不等于
        「整轮模型工具循环完成」。后者要等节点用 ``complete_loop()`` 显式确认 ——
        否则一次中途崩溃/截断/取消都会留下一条假 ``succeeded``。

        未启用清单的 run（以及迁移前的历史行）走既有语义：``running → succeeded``，
        与今天逐字一致（M2 契约）。

        期间被撤销或已过期则返回 False，结果不落库。
        """
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                   SET state = CASE WHEN expected_agent_version IS NULL THEN ? ELSE state END,
                       snapshot_json = ?, actual_node_id = ?, node_build = ?,
                       completed_at = CASE WHEN expected_agent_version IS NULL THEN ? ELSE NULL END
                 WHERE run_id = ? AND state = ? AND expires_at > ?
                """,
                (
                    SUCCEEDED,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    actual_node_id,
                    node_build,
                    _now().isoformat(),
                    run_id,
                    RUNNING,
                    _now().isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def complete_loop(
        self,
        run_id: str,
        *,
        observed_agent_version: str | None,
        actual_node_id: str | None = None,
        failure_reason: str | None = None,
        success_reason: str | None = None,
    ) -> tuple[str, AgentRun | None]:
        """整轮终局：成功则 ``running``（已有快照）→ ``succeeded``，失败则落 ``failed``。

        成功与失败**走同一个写事务、同一条过期原语**（``_expire_in`` + 事务内
        ``expires_at <= now``），所以失败通知不会在到期之后把 ``expired`` 覆盖成
        ``model_failed``，也不会出现「先读后写」的窗口。

        返回值是 ``(verdict, run)``，verdict 取：
        ``completed`` / ``already_completed`` / ``failed`` / ``already_failed`` /
        ``not_tracked`` / ``drift`` / ``not_active`` / ``missing`` / ``incomplete`` /
        ``expired`` / ``conflict``。

        三条不可放宽的条件（成功与失败共同）：
        - 记录有期望身份，且**本次回传的身份与之逐字相等**（领取时的 ``matched``
          不授权另一份配置来提交终局 —— 每个通知都要自己证明自己）；
        - 记录仍是 ``running``（失败还接受 ``issued``：首次领取就失败的那条路）；
        - 此刻未过期。

        重复通知幂等：同状态、同原因 ⇒ ``already_*``，不改时间戳与原因；
        同状态不同原因 ⇒ ``conflict``（不是「所有 failed 都算幂等成功」）。
        **终态记录一律不改写**：已确认的记录收到错身份只是 ``drift``，审计列保持原样。
        """
        observed = observed_agent_version
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return "missing", None
            untracked = _run_from_row(row)
            if untracked.expected_agent_version is None:
                # 未启用清单：没有「整轮身份」这回事，**一个列都不写**（旧记录不受影响）。
                # 判定必须在 `_expire_in` **之前**：那条收敛本身就是一次写（issued → expired），
                # 放在前面会把一条未追踪的历史行改掉，与「不写任何列」的契约相反。
                # 实测：未追踪的已过期记录收到完成通知时，状态从 issued 变成 expired/ttl_elapsed。
                return "not_tracked", untracked
            # 到这里才允许写：过期收敛对**启用清单**的记录是既有语义（与 get() 同一条语句）。
            _expire_in(connection, run_id)
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return "missing", None
            run = _run_from_row(row)
            if observed != run.expected_agent_version:
                # 每个通知都要自己证明身份；已确认的记录**不改写**（不覆盖审计列）。
                if run.state == SUCCEEDED:
                    return "drift", run
                connection.execute(
                    """
                    UPDATE agent_runs
                       SET state = ?, reason = ?, observed_agent_version = ?,
                           exec_config_state = ?, completed_at = ?,
                           actual_node_id = COALESCE(actual_node_id, ?)
                     WHERE run_id = ? AND state IN (?, ?)
                    """,
                    (
                        FAILED,
                        EXEC_CONFIG_DRIFT,
                        observed,
                        EXEC_CONFIG_MISMATCHED,
                        _now().isoformat(),
                        actual_node_id,
                        run_id,
                        ISSUED,
                        RUNNING,
                    ),
                )
                return "drift", _read_run(connection, run_id)

            if failure_reason is not None:
                if run.state == FAILED:
                    # 同原因 ⇒ 幂等；不同原因 ⇒ 冲突（不能把一次新的失败读成成功）。
                    return ("already_failed" if run.reason == failure_reason else "conflict"), run
                if run.state == EXPIRED:
                    return "expired", run
                if run.state in TERMINAL_STATES:
                    return "not_active", run
                if run.state not in {ISSUED, RUNNING}:
                    return "incomplete", run
                now = _now()
                if run.expires_at <= now:
                    return "expired", _read_run(connection, run_id)
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                       SET state = ?, reason = ?, actual_node_id = COALESCE(actual_node_id, ?),
                           completed_at = ?
                     WHERE run_id = ? AND state IN (?, ?) AND expires_at > ?
                    """,
                    (
                        FAILED,
                        failure_reason,
                        actual_node_id,
                        now.isoformat(),
                        run_id,
                        ISSUED,
                        RUNNING,
                        now.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    latest = _read_run(connection, run_id)
                    return (
                        "not_active"
                        if latest is not None and latest.state in TERMINAL_STATES
                        else "incomplete"
                    ), latest
                return "failed", _read_run(connection, run_id)

            if run.state == SUCCEEDED:
                # 成功记录上也带原因码：同原因 ⇒ 幂等，不同原因 ⇒ 冲突。
                # 早先这里无条件回 already_completed，于是 `completed+first` 与
                # `completed+second` 都返回 200 —— 「不同原因不是同一次幂等重试」这条
                # 规则在失败路径上成立、在成功路径上被静默丢掉了。
                if success_reason is None or run.reason == success_reason:
                    return "already_completed", run
                return "conflict", run
            if run.state == EXPIRED:
                # `_expire_in` 已按既有语义把它收敛过；这里只是把原因如实说出来。
                return "expired", run
            if run.state in TERMINAL_STATES:
                return "not_active", run
            if run.state != RUNNING or run.snapshot is None:
                # 没落到 running+快照就不存在「整轮完成」；issued/无快照都算未开始。
                return "incomplete", run
            now = _now()
            if run.expires_at <= now:
                return "expired", _read_run(connection, run_id)
            cursor = connection.execute(
                """
                UPDATE agent_runs
                   SET state = ?, completed_at = ?,
                       observed_agent_version = ?, exec_config_state = ?,
                       reason = COALESCE(?, reason)
                 WHERE run_id = ? AND state = ? AND expires_at > ?
                """,
                (
                    SUCCEEDED,
                    now.isoformat(),
                    observed,
                    EXEC_CONFIG_MATCHED,
                    success_reason,
                    run_id,
                    RUNNING,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                # 读到写之间被撤销/过期：不复活，按既有终态语义返回。
                latest = _read_run(connection, run_id)
                return (
                    "not_active" if latest is not None and latest.state in TERMINAL_STATES else "incomplete"
                ), latest
            return "completed", _read_run(connection, run_id)

    def fail(self, run_id: str, reason: str, *, actual_node_id: str | None = None) -> bool:
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                   SET state = ?, reason = ?, actual_node_id = COALESCE(actual_node_id, ?), completed_at = ?
                 WHERE run_id = ? AND state IN (?, ?)
                """,
                (FAILED, reason, actual_node_id, _now().isoformat(), run_id, ISSUED, RUNNING),
            )
            return cursor.rowcount == 1

    def refuse(self, run_id: str, reason: str) -> bool:
        """结果已算出，但**放行时复核没通过**：把它钉成 ``failed``，之后任何重试都释放不出来。

        只对 ``succeeded`` 生效。这不是撤回已经发出去的数据（做不到），而是保证同一笔授权
        不会在节点被停用、环境绑定改变或授权变化之后**再**被交出去（R1-1 的第三种交错）。
        """
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs SET state = ?, reason = ?, completed_at = ?
                 WHERE run_id = ? AND state = ?
                """,
                (FAILED, reason, _now().isoformat(), run_id, SUCCEEDED),
            )
            return cursor.rowcount == 1

    def revoke(self, run_id: str, reason: str = "revoked_by_caller") -> bool:
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs SET state = ?, reason = ?, completed_at = ?
                 WHERE run_id = ? AND state IN (?, ?, ?)
                """,
                (REVOKED, reason, _now().isoformat(), run_id, ISSUED, RUNNING, SUCCEEDED),
            )
            return cursor.rowcount == 1


def _read_run(connection: sqlite3.Connection, run_id: str) -> AgentRun | None:
    row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _run_from_row(row) if row else None


def _run_from_row(row: sqlite3.Row) -> AgentRun:
    snapshot = row["snapshot_json"]
    return AgentRun(
        run_id=row["run_id"],
        scope_fingerprint=row["scope_fingerprint"],
        scope_json=row["scope_json"],
        order_no=row["order_no"],
        operation=row["operation"],
        environment=row["environment"],
        app_id=row["app_id"],
        agent_id=row["agent_id"],
        expected_node_id=row["expected_node_id"],
        state=row["state"],
        reason=row["reason"],
        snapshot=json.loads(snapshot) if snapshot else None,
        actual_node_id=row["actual_node_id"],
        node_build=row["node_build"],
        expected_agent_version=row["expected_agent_version"],
        observed_agent_version=row["observed_agent_version"],
        exec_config_state=row["exec_config_state"],
        gateway_version=row["gateway_version"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        claimed_at=_ts(row["claimed_at"]),
        completed_at=_ts(row["completed_at"]),
    )


# --------------------------------------------------------------------------
# 编排平台那一跳
# --------------------------------------------------------------------------


def _dispatch_failure(_failure: HttpFailure) -> AgentRunDispatchError:
    return AgentRunDispatchError("agent platform unavailable")


def _parse_snail_envelope(payload: Any, mapping: ErrorMapping) -> Any:
    # Snail 的 Result：status==1 为成功；其余一律视为平台不可用，正文不转述。
    if isinstance(payload, dict) and payload.get("status") == 1 and isinstance(payload.get("data"), dict):
        return payload["data"]
    raise mapping.invalid_envelope(HttpFailure("invalid_envelope", detail="agent platform rejected"))


class SnailDispatcher:
    """以 Gateway 自己的平台应用凭据发起一次对话。

    传给平台的只有：服务端绑定的 agentId、``sid=run_id``、一句固定的提问。
    订单号、用户、租户都**不**进平台请求 —— 节点工具的业务输入从 Gateway 记录读。
    """

    question = "请查询本次已授权订单的当前充电状态，并用一句话回答。"

    def dispatch(self, config: AgentRunConfig, run_id: str) -> str:
        data = request_json(
            RequestSpec(
                url=f"{config.snail_base_url}/snail-ai/openapi/v1/agent/chat/sync",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Snail-Ai-App-Id": config.snail_app_id,
                    "Snail-Ai-Token": config.snail_app_token,
                },
                body=json.dumps(
                    {
                        "agentId": config.snail_agent_id,
                        "openId": config.snail_open_id,
                        # 每次一个全新会话：不能让上一轮历史混进这一轮。
                        "conversationId": "conv_" + uuid.uuid4().hex,
                        "content": self.question,
                        "sid": run_id,
                        "timeout": 60,
                    }
                ).encode("utf-8"),
                timeout=75,
                max_read_bytes=65536,
                follow_redirects=False,
            ),
            mapping=ErrorMapping(
                auth_rejected=_dispatch_failure,
                http_error=_dispatch_failure,
                unavailable=_dispatch_failure,
                invalid_body=_dispatch_failure,
                invalid_envelope=_dispatch_failure,
            ),
            envelope=_parse_snail_envelope,
        )
        return str(data.get("content") or "")
