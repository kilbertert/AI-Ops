"""登录代理的业务流程与它自己的持久化（#772 B / #778）。

这一层把三件事串起来，并保持一条纪律：**身份结论只来自公司登录交换**。

    company_login → read_permissions → (有模板权限码?) → provision_editor → mint_session

## 映射表

一张 SQLite 表，键是**公司用户 id**（不是邮箱）：同一公司用户二次登录必须落到同一个
Dify 账号（用户故事 14），而邮箱是从 id 确定性生成的，所以两者等价 —— 但键取 id，
因为它才是"这个人"在公司的稳定标识。

**表里没有密码。** 自签会话模型下不存在"要保管的密码"，这是它相对 PRD 原方案最大的一处
简化，也是原阻塞消失的原因（见 ADR-0023）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiops_diagnostics.dify_login_company import (
    CompanyAuthSettings,
    CompanyIdentity,
    company_login,
    read_permissions,
    read_user_type,
)
from aiops_diagnostics.dify_login_provision import (
    DifyConsoleSettings,
    account_email_for,
    provision_editor,
)
from aiops_diagnostics.dify_login_proxy import (
    CompanyLoginRejected,
    ConsoleSession,
    PermissionDenied,
    mint_console_session,
)

_LOGGER = logging.getLogger("aiops.dify_login_proxy.service")

#: 映射状态。``active`` 能登录；``disabled`` 是"公司侧已失权"的本地落点。
#:
#: **为什么是本地状态而不是去 Dify 封禁账号**（#779，两处源码核实）：
#: 1. Dify 1.17.1 **没有** moderation/ban 的 HTTP 接口 —— `AccountStatus.BANNED` 只在
#:    读取侧被检查（`load_user` 401），没有任何写入口。
#: 2. 退路「把成员从工作区移除」**不可逆**：`remove_member_from_tenant` 会把该成员的
#:    app/dataset 转给 owner，而重新加入的条件是 `status == PENDING or RBAC_ENABLED`；
#:    本实例 `RBAC_ENABLED=False`，一个已激活账号被移除后，重新邀请**静默不生效**
#:    （不建 join、也抛不出 `AccountAlreadyInTenantError`）。用它当封禁手段＝永久失去该
#:    成员且悄悄改掉归属。
#: ⇒ 封禁落在**我们自己的映射**上，而"活着的会话随之失效"由令牌寿命界定（见
#:    :data:`SESSION_MAX_MINUTES_NOTE`）—— 代理不在请求路径上时，这是能诚实给出的边界。
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

#: 自签会话的有效期就是撤权窗口：会话是**无状态**的（Dify 不查会话存储），所以一枚已
#: 签出的令牌在它 `exp` 之前一直有效。代理把 `access` 与 `refresh` 都签成同一个有效期，
#: 于是"公司撤权 → 该用户彻底登不进来"的最坏情形就是这一个窗口。默认 60 分钟，比公司
#: 令牌（960 分钟）短一个量级 —— PRD 要求的"不超过公司令牌同量级"因此**更强地**满足。
#: 要让窗口更小就把 `AIOPS_LOGIN_PROXY_ACCESS_MINUTES` 调小。
SESSION_MAX_MINUTES_NOTE = "access/refresh 同寿命；撤权窗口 = ACCESS_MINUTES"


@dataclass(frozen=True, slots=True)
class LoginPolicy:
    """谁可以登录。两个权限码都是**配置项**（生产值由 #777 给出）。"""

    edit_permission: str
    publish_permission: str = ""

    def may_edit(self, permissions: tuple[str, ...]) -> bool:
        return bool(self.edit_permission) and self.edit_permission in permissions


class MappingStore:
    """公司用户 id ↔ Dify 账号 的映射，以及每次登录的权限码快照。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dify_login_mappings (
                    company_user_id TEXT PRIMARY KEY,
                    company_username TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    user_type TEXT NOT NULL DEFAULT '',
                    dify_account_id TEXT NOT NULL,
                    dify_email TEXT NOT NULL,
                    permissions_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    last_login_at TEXT NOT NULL,
                    last_reconciled_at TEXT
                )
                """
            )
            # 审计账本（#779）：**追加写**，一行一个动作。与"当前状态"分开存，因为
            # "现在是什么状态"与"谁在什么时候把它改成了什么"是两个问题，合成一列会让
            # 后者被前者覆盖掉。here 记的只有非敏感字段：公司用户 id、Dify 账号 id、
            # 动作、原因码、时间 —— 没有密码、没有令牌、没有会话。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dify_login_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    company_user_id TEXT NOT NULL,
                    dify_account_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT ''
                )
                """
            )
            mapping_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(dify_login_mappings)").fetchall()
            }
            if "user_type" not in mapping_columns:
                connection.execute(
                    "ALTER TABLE dify_login_mappings ADD COLUMN user_type TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_dify_login_audit_user"
                " ON dify_login_audit(company_user_id, at DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self, company_user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dify_login_mappings WHERE company_user_id = ?",
                (company_user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def by_dify_account(self, dify_account_id: str) -> dict[str, Any] | None:
        """Reverse lookup: which company user owns this Dify account (#780).

        The bridge's only identity input is the Dify console session, and the
        session names a **Dify account**. Mapping it back to a company user is
        this hop; if there is no row, the account was never opened by us, which
        the caller treats as "no mapping" (refuse to publish).
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dify_login_mappings WHERE dify_account_id = ? LIMIT 1",
                (dify_account_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def permissions_of(self, row: dict[str, Any]) -> tuple[str, ...]:
        """The permission codes captured at this mapping's **last login** (#780).

        Deliberately a snapshot and not a live read: the proxy has no way to ask
        the company about an arbitrary user without that user's own login, so the
        honest semantics are "what was true when they last logged in". #779 keeps
        that snapshot fresh (a lost permission disables the mapping at next
        login), and the session lifetime bounds the window.
        """
        import json

        raw = row.get("permissions_json") or "[]"
        try:
            parsed = json.loads(raw)
        except ValueError:
            return ()
        return tuple(str(item) for item in parsed if isinstance(item, str) and item)

    def upsert(
        self,
        *,
        company_user_id: str,
        username: str,
        tenant_id: str,
        dify_account_id: str,
        dify_email: str,
        permissions: tuple[str, ...],
        status: str = STATUS_ACTIVE,
        user_type: str = "",
    ) -> None:
        import json

        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dify_login_mappings (
                    company_user_id, company_username, tenant_id, user_type, dify_account_id, dify_email,
                    permissions_json, status, created_at, last_login_at, last_reconciled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_user_id) DO UPDATE SET
                    company_username = excluded.company_username,
                    tenant_id = excluded.tenant_id,
                    user_type = excluded.user_type,
                    dify_account_id = excluded.dify_account_id,
                    dify_email = excluded.dify_email,
                    permissions_json = excluded.permissions_json,
                    status = excluded.status,
                    last_login_at = excluded.last_login_at,
                    last_reconciled_at = excluded.last_reconciled_at
                """,
                (
                    company_user_id,
                    username,
                    tenant_id,
                    user_type,
                    dify_account_id,
                    dify_email,
                    json.dumps(list(permissions), ensure_ascii=False),
                    status,
                    now,
                    now,
                    now,
                ),
            )

    def record_audit(
        self,
        *,
        company_user_id: str,
        dify_account_id: str,
        action: str,
        reason_code: str = "",
    ) -> None:
        """Append one lifecycle action. Never raises into the login path.

        审计写在**登录成功/失败的关键分支**上；它是可观测性，不是授权判据 ——
        所以一次磁盘写入失败不该把一次合法登录变成失败（但会告警）。
        """
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    "INSERT INTO dify_login_audit"
                    " (at, company_user_id, dify_account_id, action, reason_code)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (_utc_now(), company_user_id, dify_account_id, action, reason_code),
                )
        except sqlite3.Error:  # pragma: no cover - 磁盘问题不该挡登录
            _LOGGER.warning("dify-login: audit write failed for %s/%s", company_user_id, action)

    def audit_entries(self, company_user_id: str | None = None) -> list[dict[str, Any]]:
        """读审计行（新在前）；给测试与运维查询用。"""
        with self._connect() as connection:
            if company_user_id is None:
                rows = connection.execute("SELECT * FROM dify_login_audit ORDER BY id DESC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM dify_login_audit WHERE company_user_id = ? ORDER BY id DESC",
                    (company_user_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, company_user_id: str, status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE dify_login_mappings SET status = ?, last_reconciled_at = ? WHERE company_user_id = ?",
                (status, _utc_now(), company_user_id),
            )

    def retag_email(self, company_user_id: str, dify_email: str) -> None:
        """把一行的 ``dify_email`` 改写成环境化的派生值（#864 的迁移）。

        **只改这一列**，不动 ``dify_account_id``：Dify 里的账号 id 与它的邮箱是两件
        独立的事，而会话是**按 id** 签的（`mint_console_session(account_id)`），
        `by_dify_account` 也是按 id 反查。所以这条迁移不碰 Dify、不换账号、
        历史编辑与发布记录的归属一毫不受影响。

        Dify 侧那个账号的邮箱仍旧是旧的（无前缀）—— 这**不会**造成撞号：新派生出的
        邮箱一律带环境前缀，永远不可能等于无前缀的旧值。详见 ADR-0061。
        """
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE dify_login_mappings SET dify_email = ?, last_reconciled_at = ?"
                " WHERE company_user_id = ?",
                (dify_email, _utc_now(), company_user_id),
            )


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class LoginService:
    """一次登录的完整流程。**副作用只有两处**：映射表、以及（首次登录时的）Dify 开户。"""

    def __init__(
        self,
        *,
        company: CompanyAuthSettings,
        dify: DifyConsoleSettings,
        store: MappingStore,
        policy: LoginPolicy,
        owner_user_id: str,
    ) -> None:
        self.company = company
        self.dify = dify
        self.store = store
        self.policy = policy
        #: 应急 owner 的公司**映射账号 id**（不是公司账号）。开户需要 owner 会话，
        #: 而 owner 会话由它自己的 Dify 账号 id 自签得到 —— 于是这张票也不需要密码。
        self.owner_user_id = owner_user_id

    def owner_session(self) -> ConsoleSession:
        return mint_console_session(
            self.owner_user_id,
            secret_key=self.dify.secret_key,
            deployment_edition=self.dify.deployment_edition,
            access_expire_minutes=self.dify.access_expire_minutes,
        )

    def login(
        self,
        *,
        username: str,
        password: str,
        random_str: str,
        code: str,
    ) -> tuple[ConsoleSession, dict[str, Any]]:
        """一次登录。返回（会话, 审计用的**非敏感**摘要）。

        失败一律抛 :class:`DifyLoginError`，``code`` 稳定、``message`` 面向运营。
        **公司密码与令牌不在这里落任何地方。**
        """
        try:
            identity: CompanyIdentity = company_login(
                self.company, username=username, password=password, random_str=random_str, code=code
            )
        except CompanyLoginRejected:
            # 公司登录**失败**（密码错、账号停用/不存在）**绝不改变映射状态** —— 失败可能
            # 只是密码打错，据此封禁会把一次手误变成一次锁定（#779 的明确要求）。
            raise
        permissions = read_permissions(self.company, identity)
        user_type = read_user_type(self.company, identity)

        existing = self.store.get(identity.user_id)

        if not self.policy.may_edit(permissions):
            # 失去编辑权限码 ⇒ **无论此前是什么状态**都拒绝登录。若此前是 active，就地
            # 置为 disabled 并记审计 —— 这一次登录就是撤权生效点（#779）。此后即使公司又给回
            # 权限码，重登也会重新授权并把状态改回 active（下一段），所以它是**可逆**的。
            if existing is not None and existing["status"] == STATUS_ACTIVE:
                self.store.set_status(identity.user_id, STATUS_DISABLED)
                self.store.record_audit(
                    company_user_id=identity.user_id,
                    dify_account_id=str(existing["dify_account_id"]),
                    action="revoked",
                    reason_code="MISSING_EDIT_PERMISSION",
                )
                _LOGGER.info("dify-login: revoked company_user=%s (lost edit permission)", identity.user_id)
            elif existing is None:
                self.store.record_audit(
                    company_user_id=identity.user_id,
                    dify_account_id="",
                    action="denied",
                    reason_code="MISSING_EDIT_PERMISSION",
                )
            # 用户故事 4：明确告诉他去找谁开权限，而不是给一个空工作区。
            required = self.policy.edit_permission or "未配置"
            raise PermissionDenied(
                "MISSING_EDIT_PERMISSION",
                f"公司账号缺少「可编辑智能体」权限（{required}），请联系公司管理员",
            )

        account_id = existing["dify_account_id"] if existing is not None else ""
        email = account_email_for(identity.user_id, environment=self.dify.environment)
        if existing is not None and str(existing["dify_email"] or "") not in ("", email):
            # 这条映射是**加环境前缀之前**建的（#864）。它的 `dify_account_id` 指向一个用
            # 旧邮箱开出来的账号，而新派生出的邮箱是另一个。
            #
            # **不换账号，只改这一列**：会话是按 `account_id` 签的，`by_dify_account`
            # 也按 id 反查 —— 换掉邮箱列不影响任何一条身份路径，而 Dify 侧那个账号
            # 的历史编辑与发布记录一毫不受影响。旧邮箱在 Dify 里保持原样，但它已不再
            # 被任何派生值命中（新派生一律带环境前缀），因此不会与新账号撞号。
            #
            # 详见 ADR-0061 §「已有映射怎么迁」。
            self.store.retag_email(identity.user_id, email)
            self.store.record_audit(
                company_user_id=identity.user_id,
                dify_account_id=account_id,
                action="email_retagged",
                reason_code="ENVIRONMENT_PREFIX",
            )
            _LOGGER.info(
                "dify-login: retagged mapping email for company_user=%s (environment prefix)",
                identity.user_id,
            )
        if not account_id:
            account_id = provision_editor(
                self.dify,
                owner_session=self.owner_session(),
                company_user_id=identity.user_id,
                username=identity.username,
            )

        # 解封：此前 disabled 而现在又有编辑权限码 ⇒ 改回 active（#779 的"重新获得权限后
        # 再登录，解除封禁"）。**这是与封禁可逆的那一半**，所以必须在 upsert 之前判出方向
        # 以便记审计 —— upsert 自己就会把状态写成 active。
        reactivated = existing is not None and existing["status"] == STATUS_DISABLED
        self.store.upsert(
            company_user_id=identity.user_id,
            username=identity.username,
            tenant_id=identity.tenant_id,
            dify_account_id=account_id,
            dify_email=email,
            permissions=permissions,
            status=STATUS_ACTIVE,
            user_type=user_type,
        )
        if reactivated:
            self.store.record_audit(
                company_user_id=identity.user_id,
                dify_account_id=account_id,
                action="reactivated",
                reason_code="EDIT_PERMISSION_REGAINED",
            )
            _LOGGER.info("dify-login: reactivated company_user=%s", identity.user_id)
        elif existing is None:
            self.store.record_audit(
                company_user_id=identity.user_id,
                dify_account_id=account_id,
                action="provisioned",
                reason_code="FIRST_LOGIN",
            )
        session = mint_console_session(
            account_id,
            secret_key=self.dify.secret_key,
            deployment_edition=self.dify.deployment_edition,
            access_expire_minutes=self.dify.access_expire_minutes,
        )
        # 审计/日志只用**非敏感**摘要：没有公司密码、没有公司令牌、没有 Dify 会话。
        summary = {
            "company_user_id": identity.user_id,
            "username": identity.username,
            "dify_account_id": account_id,
            "permissions_count": len(permissions),
        }
        _LOGGER.info(
            "dify-login: user=%s account=%s permissions=%d",
            identity.user_id,
            account_id,
            len(permissions),
        )
        return session, summary


def utc_iso() -> str:
    return _utc_now()


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "LoginPolicy",
    "LoginService",
    "MappingStore",
    "utc_iso",
]
