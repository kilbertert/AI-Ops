"""对客知识库授权、节点选库、目录凭据与租户业务连接登记。

合并原 ``consumer_knowledge_grants`` 与 ``consumer_knowledge_selection``：
知识库是否允许对客分发（显式授权台账）与发布/运行时选库校验始终配套生效，收拢在单一模块内。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from contextlib import contextmanager
from pathlib import Path

from aiops_diagnostics.common.private_files import ensure_private_directory, protect_private_file
from aiops_diagnostics.gateway import gateway_db, gateway_schema

PROVIDER = "trendpower/consumer_knowledge/consumer_knowledge"
TOOL = "consumer_knowledge"

AUDIENCE_CUSTOMER = "customer"
AUDIENCE_INTERNAL = "internal"


class KnowledgeGrants:
    """对客知识的分发授权台账：显式登记 `(tenant_id, environment, knowledge_base_id)`。"""

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        ensure_private_directory(self.path.parent)
        gateway_schema.ensure(self.path)
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False):
        with gateway_db.connection(self.path, write=write) as connection:
            yield connection

    def granted(self, tenant_id: str, environment: str) -> set[str]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT knowledge_base_id FROM consumer_knowledge_grants "
                "WHERE tenant_id=? AND environment=? AND revoked_at IS NULL",
                (tenant_id, environment),
            ).fetchall()
        return {row["knowledge_base_id"] for row in rows}

    def generation(self, tenant_id: str, environment: str, knowledge_base_id: str) -> float | None:
        """当前显式授权世代；撤权后旧凭据不能因再次授权而复活。"""
        with self._connection() as db:
            row = db.execute(
                "SELECT granted_at FROM consumer_knowledge_grants WHERE tenant_id=? "
                "AND environment=? AND knowledge_base_id=? AND revoked_at IS NULL",
                (tenant_id, environment, knowledge_base_id),
            ).fetchone()
        return float(row["granted_at"]) if row else None

    def grant(
        self,
        tenant_id: str,
        environment: str,
        knowledge_base_id: str,
        granted_by: str,
        *,
        backfill: bool = False,
    ) -> None:
        conflict = (
            "DO NOTHING"
            if backfill
            else (
                "DO UPDATE SET revoked_at=NULL, granted_by=excluded.granted_by, "
                "granted_at=MAX(excluded.granted_at,consumer_knowledge_grants.granted_at+0.000001)"
            )
        )
        with self._connection(write=True) as db:
            db.execute(
                "INSERT INTO consumer_knowledge_grants"
                "(tenant_id,environment,knowledge_base_id,granted_by,granted_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,environment,knowledge_base_id) " + conflict,
                (tenant_id, environment, knowledge_base_id, granted_by, time.time()),
            )

    def revoke(self, tenant_id: str, environment: str, knowledge_base_id: str) -> bool:
        with self._connection(write=True) as db:
            return (
                db.execute(
                    "UPDATE consumer_knowledge_grants SET revoked_at=? "
                    "WHERE tenant_id=? AND environment=? AND knowledge_base_id=? AND revoked_at IS NULL",
                    (time.time(), tenant_id, environment, knowledge_base_id),
                ).rowcount
                == 1
            )


def audience(rows, granted: set[str]):
    """把 kb-service 的目录行标上可分发属性；无授权行一律按内部库处理（fail closed）。"""
    return [
        {
            **row,
            "audience": AUDIENCE_CUSTOMER if row.get("knowledge_base_id") in granted else AUDIENCE_INTERNAL,
        }
        for row in rows
    ]


class ConsumerEndpoints:
    """每租户的 CPMS 连接登记（从已批准的环境连接写入，不做全租户兜底）。"""

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        ensure_private_directory(self.path.parent)
        gateway_schema.ensure(self.path)
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False):
        with gateway_db.connection(self.path, write=write) as connection:
            yield connection

    def get(self, tenant_id: str, environment: str) -> str:
        with self._connection() as db:
            row = db.execute(
                "SELECT base_url FROM consumer_endpoints WHERE tenant_id=? AND environment=?",
                (tenant_id, environment),
            ).fetchone()
        return str(row["base_url"]) if row else ""

    def register(self, tenant_id: str, environment: str, base_url: str, registered_by: str) -> None:
        with self._connection(write=True) as db:
            db.execute(
                "INSERT INTO consumer_endpoints"
                "(tenant_id,environment,base_url,registered_by,registered_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(tenant_id,environment) DO UPDATE SET"
                " base_url=excluded.base_url, registered_by=excluded.registered_by,"
                " registered_at=excluded.registered_at",
                (tenant_id, environment, base_url, registered_by, time.time()),
            )


def approved_endpoint(value: str, tenant_id: str) -> str:
    """从已批准的环境连接里取出这个租户的地址；取不到返回空串。"""
    from aiops_diagnostics.gateway.gateway_config import parse_charging_endpoints

    legacy, endpoints = parse_charging_endpoints(value or "")
    if endpoints:
        return endpoints.get(tenant_id, "")
    return legacy or ""


def approved_business_endpoint(settings, tenant_id: str) -> str:
    """开通只使用已启用环境目录；未迁移部署保持旧地址/清单兼容。"""
    from aiops_diagnostics.onboarding import business_environments

    if getattr(settings, "environment_registry_file", ""):
        from aiops_diagnostics.onboarding.registry import for_settings

        return for_settings(settings).require(tenant_id)["cpms_base_url"]
    pinned = approved_endpoint(getattr(settings, "charging_status_base_url", ""), tenant_id)
    if pinned:
        return pinned
    entries = business_environments.load(getattr(settings, "business_environments_file", ""))
    return business_environments.approved_base_url(entries)


def consumer_endpoint(settings, tenant_id: str) -> str:
    """Java 和 Chatflow 共用已登记的租户/环境连接，兼容现役配置。"""
    from aiops_diagnostics.gateway.gateway_config import charging_endpoint

    if getattr(settings, "environment_registry_file", ""):
        return approved_business_endpoint(settings, tenant_id)
    database = getattr(settings, "database_file", "")
    managed = ConsumerEndpoints(database).get(tenant_id, settings.execution_environment) if database else ""
    return managed or charging_endpoint(settings.charging_status_base_url, tenant_id)


# ---------------------------------------------------------------------------
# 节点选库与只读目录凭据（原 consumer_knowledge_selection）
# ---------------------------------------------------------------------------


def catalogue_token(secret, environment, workspace):
    if not secret or not environment or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace):
        raise ValueError("catalogue credential configuration unavailable")
    encoded = base64.urlsafe_b64encode(workspace.encode()).decode().rstrip("=")
    digest = hmac.new(
        secret.encode(), f"consumer-kb-catalogue:1:{environment}:{workspace}".encode(), hashlib.sha256
    ).hexdigest()
    return f"ckc_{encoded}.{digest}"


def catalogue_workspace(token, secret, environment):
    try:
        encoded, _ = token.removeprefix("ckc_").split(".")
        workspace = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        expected = catalogue_token(secret, environment, workspace)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("catalogue credential refused") from exc
    if not hmac.compare_digest(expected.encode(), token.encode()):
        raise ValueError("catalogue credential refused")
    return workspace


def selected_libraries(node):
    config = node.get("tool_configurations", {}).get("knowledge_base_ids", {})
    if not isinstance(config, dict):
        raise ValueError("知识库选择必须是节点配置常量")
    values = config.get("value")
    if (
        config.get("type") != "constant"
        or not isinstance(values, list)
        or not 1 <= len(values) <= 20
        or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v) for v in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("请选择一个或多个知识库；库列表必须是节点配置常量")
    return tuple(values)


def publication_libraries(source):
    """目录故障不是可展示的合同正文，只公开固定原因。"""
    from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchUnavailable

    try:
        return source() if callable(source) else source
    except (ValueError, TypeError, KnowledgeSearchUnavailable) as exc:
        raise ValueError("知识库目录暂不可用，请稍后重试") from exc


def require_owned(selected, rows):
    """选中的库必须属于本租户且允许对客（fail closed）。"""
    owned = {row["knowledge_base_id"] for row in rows}
    if not set(selected) <= owned:
        raise ValueError("所选知识库已删除或不属于当前租户，请重新选择")
    customer = {row["knowledge_base_id"] for row in rows if row.get("audience") == AUDIENCE_CUSTOMER}
    if not set(selected) <= customer:
        raise ValueError("所选知识库未授权给客户端使用（内部资料不能交给消费者），请重新选择")
