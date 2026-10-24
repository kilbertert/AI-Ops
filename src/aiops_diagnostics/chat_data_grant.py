"""短期、单请求、单订单的**取数授权**（APP-RUNTIME-02）。

## 它解决的是什么

Dify 的 Workflow 里有一个 HTTP 步骤要去 Gateway 取「这一单的状态」。它需要一个凭据。
三个候选都不行：

* 复用调用者的登录会话 —— 那等于把用户的会话交给第三方编排平台；
* 复用运维工具的整租户 key —— 那会把**个人**权限降级成**租户**权限（ADR-0091 的边界）；
* 让模型生成或挑选参数 —— 模型能选的就不是授权。

所以签发**这一次、这一单**的凭据：

## 判据

1. **不可猜**：≥256 bit 随机；库里只存 sha256 哈希，明文只出现在这一次的注入值与
   Dify inputs 里（后者是已知限制，见 `dify_chat_workflow` 的模块文档）。
2. **绑死范围**：主体指纹、订单、环境、租户、QA、workflow 身份、有效期全部落库；
   凭据本身只承载随机串，**不带**任何可被解读的身份——校验是按哈希查行，不是解析 token。
3. **一次性 + 短期**：有效期沿用项目既定的 180 秒；首次取数成功后仍保留记录，
   但重复回调**只能**拿回同一轮已存快照，不会再读一次业务。
4. **撤销即失效**：完成、失败、取消都调 `revoke`；此后同一凭据一律拒绝。
5. **run_id / qa_id 单独不能取数**：它们能定位记录，但取数需要凭据本身。

## 边界

本模块只管"这次取数凭不凭得住"，不判断"这个调用者能不能看这张单" ——
后者是 `gateway_api` 的订单授权判据，调用方必须在**用凭据取数之前**再做一次。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: 取数授权的有效期。与本项目既定的 run TTL 对齐，**不因为重试而续期**。
GRANT_TTL_SECONDS = 180

#: 随机字节数。32 字节 = 256 bit，满足"不可猜"的下限。
GRANT_BYTES = 32

#: 靠猜是猜不出来的前缀；它只是让人在日志里认出这是哪一类凭据。
GRANT_PREFIX = "cdg_"

ACTIVE = "active"
CONSUMED = "consumed"
REVOKED = "revoked"


class DataGrantError(RuntimeError):
    """凭据不可用。``code`` 是回给调用方的稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DataGrant:
    """一条**已签发**的取数授权。所有范围字段都在签发时定死。"""

    grant_id: str
    scope_fingerprint: str
    order_no: str
    environment: str
    tenant_id: str
    qa_id: str
    workflow_id: str
    state: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DataGrantStore:
    """取数授权表。与网关共用同一个 SQLite 文件，各自建表。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_data_grants (
                    grant_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    scope_fingerprint TEXT NOT NULL,
                    order_no TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    qa_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_chat_data_grants_qa
                    ON chat_data_grants(qa_id);
                """
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
        scope_fingerprint: str,
        order_no: str,
        environment: str,
        tenant_id: str,
        qa_id: str,
        workflow_id: str,
    ) -> tuple[str, DataGrant]:
        """签发一张。返回 (明文 token, 记录)；明文只在这一刻存在。"""
        token = GRANT_PREFIX + secrets.token_urlsafe(GRANT_BYTES)
        grant_id = "cdg_" + secrets.token_hex(8)
        now = _now()
        with self._connection(write=True) as connection:
            connection.execute(
                """
                INSERT INTO chat_data_grants (
                    grant_id, token_hash, scope_fingerprint, order_no, environment,
                    tenant_id, qa_id, workflow_id, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    _hash(token),
                    scope_fingerprint,
                    order_no,
                    environment,
                    tenant_id,
                    qa_id,
                    workflow_id,
                    ACTIVE,
                    now.isoformat(),
                    (now + timedelta(seconds=GRANT_TTL_SECONDS)).isoformat(),
                ),
            )
        return token, self.by_id(grant_id)  # type: ignore[return-value]

    def by_token(self, token: str) -> DataGrant | None:
        """按明文 token 查记录。**只读**：不做任何过期收敛或状态改写。"""
        if not token or not token.startswith(GRANT_PREFIX):
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM chat_data_grants WHERE token_hash = ?", (_hash(token),)
            ).fetchone()
        return _grant_from_row(row) if row else None

    def by_id(self, grant_id: str) -> DataGrant | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM chat_data_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        return _grant_from_row(row) if row else None

    def consume(self, grant_id: str) -> bool:
        """标记"这次凭据已经取过一次数"。**只有第一次**能成功。

        返回 False 的那个调用者不许再读业务 —— 它只能拿回同轮已存的快照。
        """
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE chat_data_grants SET state = ?, consumed_at = ?
                 WHERE grant_id = ? AND state = ? AND expires_at > ?
                """,
                (CONSUMED, _now().isoformat(), grant_id, ACTIVE, _now().isoformat()),
            )
            return cursor.rowcount == 1

    def revoke(self, grant_id: str) -> bool:
        """撤销。幂等；已撤销的仍返回 False（不是"又撤了一次"）。"""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE chat_data_grants SET state = ? WHERE grant_id = ? AND state != ?",
                (REVOKED, grant_id, REVOKED),
            )
            return cursor.rowcount == 1

    def revoke_for_qa(self, qa_id: str) -> int:
        """按 QA 撤销该 QA 的全部凭据（取消/失败/完成三条路都要调）。"""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE chat_data_grants SET state = ? WHERE qa_id = ? AND state != ?",
                (REVOKED, qa_id, REVOKED),
            )
            return cursor.rowcount


def _grant_from_row(row: sqlite3.Row) -> DataGrant:
    return DataGrant(
        grant_id=row["grant_id"],
        scope_fingerprint=row["scope_fingerprint"],
        order_no=row["order_no"],
        environment=row["environment"],
        tenant_id=row["tenant_id"],
        qa_id=row["qa_id"],
        workflow_id=row["workflow_id"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        consumed_at=datetime.fromisoformat(row["consumed_at"]) if row["consumed_at"] else None,
    )


def usable(grant: DataGrant | None, *, now: datetime | None = None) -> DataGrant:
    """这张凭据此刻能不能用。四条判据，缺一条就拒绝。

    * 不存在 ⇒ `GRANT_UNKNOWN`；
    * 已撤销 ⇒ `GRANT_REVOKED`（取消、失败、完成都会走到这里）；
    * 已过期 ⇒ `GRANT_EXPIRED`（按 `expires_at` 判，不看 state —— 一条停在那里没被
      收敛的 active 记录仍然是过期的）；
    * 已经被这次请求之外的用途取过数 ⇒ 由调用方的 `consume` 裁决，不在这里合并。
    """
    if grant is None:
        raise DataGrantError("GRANT_UNKNOWN", "data grant is not recognised")
    if grant.state == REVOKED:
        raise DataGrantError("GRANT_REVOKED", "data grant has been revoked")
    if grant.expires_at <= (now or _now()):
        raise DataGrantError("GRANT_EXPIRED", "data grant has expired")
    return grant


__all__: list[str] = [
    "ACTIVE",
    "CONSUMED",
    "DataGrant",
    "DataGrantError",
    "DataGrantStore",
    "GRANT_PREFIX",
    "GRANT_TTL_SECONDS",
    "REVOKED",
    "usable",
]
