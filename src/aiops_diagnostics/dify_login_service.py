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
)
from aiops_diagnostics.dify_login_provision import (
    DifyConsoleSettings,
    provision_editor,
)
from aiops_diagnostics.dify_login_proxy import (
    ConsoleSession,
    PermissionDenied,
    mint_console_session,
)

_LOGGER = logging.getLogger("aiops.dify_login_proxy.service")

#: 映射状态。``active`` 能登录；``disabled`` 是"公司侧已失权"的本地落点 —— 自签模型下
#: 封禁是**我们自己的映射**，不是去 Dify 发一个 banned（那个动作在自签模型里既无必要
#: 也无人可见：没人拿密码进 Dify）。#779 会在这里加"每次登录重读权限码"的封禁/解封。
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


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
    ) -> None:
        import json

        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dify_login_mappings (
                    company_user_id, company_username, tenant_id, dify_account_id, dify_email,
                    permissions_json, status, created_at, last_login_at, last_reconciled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_user_id) DO UPDATE SET
                    company_username = excluded.company_username,
                    tenant_id = excluded.tenant_id,
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
                    dify_account_id,
                    dify_email,
                    json.dumps(list(permissions), ensure_ascii=False),
                    status,
                    now,
                    now,
                    now,
                ),
            )

    def set_status(self, company_user_id: str, status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE dify_login_mappings SET status = ?, last_reconciled_at = ? WHERE company_user_id = ?",
                (status, _utc_now(), company_user_id),
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
        identity: CompanyIdentity = company_login(
            self.company, username=username, password=password, random_str=random_str, code=code
        )
        permissions = read_permissions(self.company, identity)

        existing = self.store.get(identity.user_id)
        if existing is not None and existing["status"] == STATUS_DISABLED:
            # 本地已失权：即使公司此刻又给了权限码，也要求一次显式的重新启用（#779）。
            raise PermissionDenied("MAPPING_DISABLED", "该账号已被停用，请联系管理员")

        if not self.policy.may_edit(permissions):
            # 没有编辑权限码 ⇒ **不开户、不签会话**。用户故事 4：明确告诉他去找谁开权限，
            # 而不是给一个空工作区。
            required = self.policy.edit_permission or "未配置"
            raise PermissionDenied(
                "MISSING_EDIT_PERMISSION",
                f"公司账号缺少「可编辑智能体」权限（{required}），请联系公司管理员",
            )

        account_id = existing["dify_account_id"] if existing is not None else ""
        if not account_id:
            account_id = provision_editor(
                self.dify,
                owner_session=self.owner_session(),
                company_user_id=identity.user_id,
                username=identity.username,
            )
        from aiops_diagnostics.dify_login_provision import account_email_for

        self.store.upsert(
            company_user_id=identity.user_id,
            username=identity.username,
            tenant_id=identity.tenant_id,
            dify_account_id=account_id,
            dify_email=account_email_for(identity.user_id),
            permissions=permissions,
            status=STATUS_ACTIVE,
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
