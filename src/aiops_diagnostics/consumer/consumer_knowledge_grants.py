"""对客知识的分发授权：**显式登记**，不按名字、不按"属于这个租户"推导。

## 为什么需要这一层

规划要求的判据是四条同时成立：库属于目标租户、**允许对客**、在该应用的发布选择内、当前仍
可用。前三条里的"属于租户"由 kb-service 的租户目录回答，第四条由检索时的可用性回答；
唯独"允许对客"没有任何上游字段承载 —— RAGFlow 的 dataset 没有"可对外"这个属性，而
**名字**不是判据：一个叫"对客知识库"的库完全可能是内部资料。

于是把它做成**服务端登记**：一条 `(租户, 环境, 库)` 的授权行。没有这一行，这个库就是
内部库，发布契约和实际检索都会拒绝它 —— 隐藏下拉项不算隔离。

## 三个刻意的决定

1. **只能显式授予。** 开通时如果客户没有指定库，我们**新建**一个库并授予它；不会按名字
   复用同名库（复用可能挑中一个内部资料库）。
2. **发布与检索都查。** 只在发布时查，撤回授权之后已经在跑的版本仍然能检索 —— 那不是隔离。
3. **撤销即时生效。** 检索路径每次重新读授权，所以撤销之后下一次提问就会被拒。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file

AUDIENCE_CUSTOMER = "customer"
AUDIENCE_INTERNAL = "internal"


class KnowledgeGrantError(ValueError):
    """可向页面展示的稳定拒绝。"""


class KnowledgeGrants:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        ensure_private_directory(self.path.parent)
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS consumer_knowledge_grants (
                    tenant_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL,
                    granted_by TEXT NOT NULL,
                    granted_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, environment, knowledge_base_id)
                )
                """
            )
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(consumer_knowledge_grants)")}
            if "revoked_at" not in columns:
                db.execute("ALTER TABLE consumer_knowledge_grants ADD COLUMN revoked_at REAL")
        protect_private_file(self.path)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def granted(self, tenant_id: str, environment: str) -> set[str]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT knowledge_base_id FROM consumer_knowledge_grants "
                "WHERE tenant_id=? AND environment=? AND revoked_at IS NULL",
                (tenant_id, environment),
            ).fetchall()
        return {row["knowledge_base_id"] for row in rows}

    def grant(
        self,
        tenant_id: str,
        environment: str,
        knowledge_base_id: str,
        granted_by: str,
        *,
        backfill: bool = False,
    ) -> None:
        # 纳管只补从未登记的库；显式授权才允许覆盖撤销记录。
        conflict = (
            "DO NOTHING"
            if backfill
            else (
                "DO UPDATE SET revoked_at=NULL, granted_by=excluded.granted_by, "
                "granted_at=excluded.granted_at"
            )
        )
        with self._connection() as db:
            db.execute(
                "INSERT INTO consumer_knowledge_grants"
                "(tenant_id,environment,knowledge_base_id,granted_by,granted_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,environment,knowledge_base_id) " + conflict,
                (tenant_id, environment, knowledge_base_id, granted_by, time.time()),
            )

    def revoke(self, tenant_id: str, environment: str, knowledge_base_id: str) -> bool:
        with self._connection() as db:
            return (
                db.execute(
                    "UPDATE consumer_knowledge_grants SET revoked_at=? "
                    "WHERE tenant_id=? AND environment=? AND knowledge_base_id=? AND revoked_at IS NULL",
                    (time.time(), tenant_id, environment, knowledge_base_id),
                ).rowcount
                == 1
            )


def audience(rows, granted: set[str]):
    """把 kb-service 的目录行标上可分发属性。

    **没有授权行就是内部库**（fail closed）。上游行里若有 `audience` 字段也不采信：
    那个字段不是我们维护的，采信它等于把判据交给一个没有被授权的地方。
    """
    return [
        {
            **row,
            "audience": AUDIENCE_CUSTOMER if row.get("knowledge_base_id") in granted else AUDIENCE_INTERNAL,
        }
        for row in rows
    ]


class ConsumerEndpoints:
    """每租户的 CPMS 连接：**从已批准的环境连接登记**，不做全租户兜底。

    为什么需要它：`charging_endpoint` 只读一个环境变量（`tenant:url,...`）。新客户开通时
    没人会去改那个环境变量 —— 于是新客户"开通成功、发布成功"，而第一次查订单就
    `TENANT_ENDPOINT_UNRESOLVED`。反过来"没有登记就回退到某个全局 URL"则是另一种错：
    那等于把某个客户的业务连接发给所有客户。

    所以登记行是明确的：`(租户, 环境) -> base_url`，来源是**已批准的环境连接**
    （`AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 的裸 URL 形式，或它的按租户映射里属于
    这个租户的那一条）。两者都拿不到就拒绝开通这一端，并给出可操作的码。
    """

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        ensure_private_directory(self.path.parent)
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS consumer_endpoints (
                    tenant_id TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    registered_by TEXT NOT NULL,
                    registered_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, environment)
                )
                """
            )
        protect_private_file(self.path)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, tenant_id: str, environment: str) -> str:
        with self._connection() as db:
            row = db.execute(
                "SELECT base_url FROM consumer_endpoints WHERE tenant_id=? AND environment=?",
                (tenant_id, environment),
            ).fetchone()
        return str(row["base_url"]) if row else ""

    def register(self, tenant_id: str, environment: str, base_url: str, registered_by: str) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO consumer_endpoints"
                "(tenant_id,environment,base_url,registered_by,registered_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(tenant_id,environment) DO UPDATE SET"
                " base_url=excluded.base_url, registered_by=excluded.registered_by,"
                " registered_at=excluded.registered_at",
                (tenant_id, environment, base_url, registered_by, time.time()),
            )


def approved_endpoint(value: str, tenant_id: str) -> str:
    """从已批准的环境连接里取出**这个租户**的地址；取不到返回空串。

    裸 URL 表示"本环境所有租户走同一个 CPMS"（这是一个**已批准的连接**，不是兜底猜测）；
    带 `tenant:url` 映射时只认这个租户那一条，不拿别人的地址顶替。
    """
    from aiops_diagnostics.gateway.gateway_config import parse_charging_endpoints

    legacy, endpoints = parse_charging_endpoints(value or "")
    if endpoints:
        return endpoints.get(tenant_id, "")
    return legacy or ""


def consumer_endpoint(settings, tenant_id: str) -> str:
    """Java 和 Chatflow 共用已登记的租户/环境连接，兼容现役配置。"""
    from aiops_diagnostics.gateway.gateway_config import charging_endpoint

    database = getattr(settings, "database_file", "")
    managed = ConsumerEndpoints(database).get(tenant_id, settings.execution_environment) if database else ""
    return managed or charging_endpoint(settings.charging_status_base_url, tenant_id)
