"""把一次**已授权**的读取交给可信执行节点：记录、冻结身份与节点绑定。

授权留在 Gateway。外部编排平台与执行节点只拿到一个高熵 ``run_id``，
**知道它本身不授予任何读取权限**：节点要用自己的独立凭据回呼领取端点，Gateway 先认节点、
核对绑定，再用记录里冻结的身份**重新做一次订单授权**，然后才读业务。

本模块只管三件事：节点/环境绑定配置、请求级授权记录及其生命周期、冻结身份的序列化。
HTTP 路由在 ``gateway_api``，授权读取规则与公共
``/v1/orders/{order_no}/charging-status`` 共用同一组函数（不在这里另造一份）。
向编排平台发起对话的那一跳不在这里。

不在本期：跨节点重选/负载均衡、续期、分布式队列、HMAC/JWT 票据格式。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
AGENT_VERSION_GAP = "not_wired"


class AgentRunConfigError(ValueError):
    pass


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
    gateway_version: str
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None

    def receipt(self) -> dict[str, Any]:
        """执行回执：全部字段来自服务端记录与认证结果，没有一个取自模型输出。"""
        digest = (
            hashlib.sha256(json.dumps(self.snapshot, sort_keys=True).encode("utf-8")).hexdigest()
            if self.snapshot is not None
            else None
        )
        return {
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
            "agent_version": None,
            "agent_version_gap": AGENT_VERSION_GAP,
            "result_sha256": digest,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


def _now() -> datetime:
    return datetime.now(UTC)


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
                    gateway_version, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def _expire(self, run_id: str) -> None:
        with self._connection(write=True) as connection:
            connection.execute(
                """
                UPDATE agent_runs SET state = ?, reason = COALESCE(reason, 'ttl_elapsed')
                 WHERE run_id = ? AND state IN (?, ?, ?) AND expires_at <= ?
                """,
                (EXPIRED, run_id, ISSUED, RUNNING, SUCCEEDED, _now().isoformat()),
            )

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

    def complete(
        self,
        run_id: str,
        *,
        snapshot: dict[str, Any],
        actual_node_id: str,
        node_build: str | None,
    ) -> bool:
        """``running → succeeded``。期间被撤销或已过期则返回 False，结果不落库。"""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                   SET state = ?, snapshot_json = ?, actual_node_id = ?, node_build = ?, completed_at = ?
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
        gateway_version=row["gateway_version"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        claimed_at=_ts(row["claimed_at"]),
        completed_at=_ts(row["completed_at"]),
    )
