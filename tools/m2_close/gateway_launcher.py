#!/usr/bin/env python3
"""M2-CLOSE-01 隔离实验里的 Gateway 启动器：**只替换外部依赖，不替换被测代码**。

被测链路一个都不替换：
``/v1/orders/{order}/charging-status/delegated``、``/v1/internal/charging-status/claims/{run}``、
``agent_runs`` 的授权记录与生命周期、``ScopedOrderAuthorizer`` 的授权判定、``read_charging_status``
的读取与裁剪，全部是本仓库的真实代码。

被替换的只有三样**外部依赖**，它们在这台隔离主机上本来就不存在：

1. **调用者身份**：真身是 Redis 里的 thirdSession。这里用 ``AIOPS_M2_LOCAL_TOKEN`` 换成一个
   固定的 ``ScopeContext``（``self`` 范围 + 一个 C 端用户）。这样 IC 才能在没有任何公司会话的
   情况下跑到「已验证身份」这一段之后。
2. **订单授权数据源**：真身是业务数据库。这里用同一个 SQLite 里的 ``synthetic_orders`` 表，
   判据与真授权器相同的一条：**订单的 ``user_id`` 等于主体的 C 端用户才可见**。
   它不是「谁问都说有」的替身 —— IC-03/IC-09 的负例靠它给出真实的拒绝。
3. **会话存储**：直接用仓库自己的 SQLite 实现指向同一个库 —— 本切片不碰会话，
   但它不该是一个会「假成功」的替身。
4. **选单数据源**（APP-RUNTIME-01 起）：聊天的「本人最新一单」由本人 SQL 范围选出，
   真身是充电业务库。这里用同一个 SQLite 的 ``synthetic_orders`` 表，排序判据与生产一致
   （``created_time DESC, id DESC``），范围判据也一致（``user_id = 本人的 C 端用户``）。
   它只替换**数据来源**，不替换选单逻辑本身 —— 选单代码仍是 ``gateway_api`` 里的那一段。
5. **平台身份目录**（APP-RUNTIME-01 起）：真身是公司 UPMS 的身份关系表。聊天入口在
   进入分支之前要判「这个主体在哪个内容域可用」，这里用 ``AIOPS_M2_PLATFORM_CONSUMER``
   打开一个只认 consumer 的目录替身。它替换的是**目录数据**，入口判据仍是同一份代码。
6. **路由分类器**：真身是 Codex 轻量分类的一轮模型调用。本切片不接真实供应商，
   所以只启用**确定性**的个人订单状态分支：该分支在分类之前判据就已确定，不发那一轮
   模型请求。其它分支（FAQ/宣传/零阶问答）在本切片里仍会尝试分类，因此本报告
   **不得**声称「路由分类在生产配置下已验收」。

任何一项被替换的事实都写进运行报告，不写成「端到端通过」。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn

from aiops_diagnostics.deployment_environment import require_deployment_identity
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

TOKEN = os.environ["AIOPS_M2_LOCAL_TOKEN"]
TENANT = os.environ.get("AIOPS_M2_TENANT", "TENANT-A")
C_USER = os.environ.get("AIOPS_M2_C_USER", "USER-A")
ORDERS_DB = Path(os.environ["AIOPS_M2_ORDERS_DB"])
#: 第二个调用者身份（可选）：用于「两个用户各自的订单交错执行」那一类判据。
OTHER_TOKEN = os.environ.get("AIOPS_M2_SECOND_TOKEN", "")
OTHER_C_USER = os.environ.get("AIOPS_M2_SECOND_C_USER", "USER-B")
#: 第二个身份所在的租户。缺省与 ``TENANT`` 相同（旧行为）；跨租户的判据必须显式给。
OTHER_TENANT = os.environ.get("AIOPS_M2_SECOND_TENANT", "") or TENANT
#: 第三个调用者身份（可选）：**另一个租户**的消费者。双租户交错验证要用它 ——
#: 两个身份同租户只能证明"单不串"，证明不了"一个租户的发布/撤回不碰另一个租户"。
THIRD_TOKEN = os.environ.get("AIOPS_M2_THIRD_TOKEN", "")
THIRD_C_USER = os.environ.get("AIOPS_M2_THIRD_C_USER", "USER-C")
THIRD_TENANT = os.environ.get("AIOPS_M2_THIRD_TENANT", "")


def _synthetic_order_source(_settings, scope):
    """本人范围内的订单列表（与生产同一条排序判据），数据来自 ``synthetic_orders``。

    只替换数据来源：范围判定（``user_id`` 等于本人）与排序（``created_time DESC, id DESC``）
    都照抄生产选单那一段，所以「最新一单」在这里是**被真的选出来**的，不是指定的。
    """
    import contextlib
    from types import SimpleNamespace

    connection = sqlite3.connect(ORDERS_DB)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT order_no, user_id, tenant_id, created_time, id FROM synthetic_orders "
                "WHERE user_id = ? AND tenant_id = ? ORDER BY created_time DESC, id DESC",
                (getattr(scope, "user_id", None), scope.tenant_id),
            )
        ]
    finally:
        connection.close()
    source = SimpleNamespace(
        list_orders=lambda **_: rows,
        get_orders=lambda no: [row for row in rows if row["order_no"] == no],
    )
    return contextlib.contextmanager(lambda: (yield source))()


class LocalPlatformDirectory:
    """只认 consumer 的平台目录替身（见模块文档第 5 条）。

    与真目录回答同一个问题（「该主体在此租户有哪些内容域」），只是没有数据库。
    operator 一律不给：本切片不碰管家端。
    """

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        from aiops_diagnostics.conversation.faq import PlatformRoleRecord

        return (PlatformRoleRecord("", c_user_id, tenant_id, "admin"),) if c_user_id else ()

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


@dataclass
class LocalResolver:
    """把 ``AIOPS_M2_LOCAL_TOKEN`` 换成固定身份；另有第二个令牌映射到另一个 C 端用户。

    第二个身份只为一个判据存在：**两个用户各自的订单交错执行时结果不串**（IC-04）。
    没有它，这条判据只能在同一身份下做，测不到「别人的单会不会串进来」。
    """

    def resolve(self, token: str, *, required_scope: str, **kwargs: Any) -> ScopeContext:
        from aiops_diagnostics.identity.caller_auth import CallerAuthError

        if token == TOKEN:
            c_user, tenant = C_USER, TENANT
        elif OTHER_TOKEN and token == OTHER_TOKEN:
            c_user, tenant = OTHER_C_USER, OTHER_TENANT
        elif THIRD_TOKEN and token == THIRD_TOKEN and THIRD_TENANT:
            c_user, tenant = THIRD_C_USER, THIRD_TENANT
        else:
            raise CallerAuthError("invalid access token", code="caller_auth.invalid")
        subject = SubjectRecord(b_user_id="", c_user_id=c_user, tenant_id=tenant)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
            resolved_at=datetime.now(UTC),
        )


class SyntheticOrderAuthorizer:
    """与真授权器同一条判据：``user_id`` 等于主体的 C 端用户才可见。

    表格在读的那一刻打开（不是启动时快照），这样 IC-09 可以在两次请求之间改授权，
    验证「授权变化后不释放旧记录的结果」。
    """

    unavailable = False

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        from aiops_diagnostics.identity.caller_auth import CallerAuthError

        if self.unavailable:
            raise CallerAuthError("unavailable", code="caller_auth.unavailable")
        connection = sqlite3.connect(ORDERS_DB)
        try:
            row = connection.execute(
                "SELECT user_id, tenant_id FROM synthetic_orders WHERE order_no = ?", (order_no,)
            ).fetchone()
        finally:
            connection.close()
        if row is None or row[1] != context.effective_tenant_id:
            return False
        # 范围语义照抄真授权器（`ScopedOrderAuthorizer` → `resolve_query_scope` 后按本人过滤）：
        #   * `self`  ⇒ 本人那一单；
        #   * `all`   ⇒ 整租户（不按用户过滤）；试运行的租户级调试范围就是这一条，
        #               不认它会让试用在这套隔离栈里永远 404 —— 那不是产品行为；
        #   * 站点级  ⇒ 本合成表没有站点列，**拒绝**（与真授权器 fail-closed 同形），
        #               不把"解析不出来"放宽成"整租户可见"。
        if context.data_scope.type == "self":
            return bool(context.subject.c_user_id) and row[0] == context.subject.c_user_id
        return context.data_scope.type == "all"


def SQLiteConversationStore(database_file: Path):  # noqa: N802 - 构造器语义
    """本切片不碰会话存储：用仓库自己的实现指向同一个库，不新增替身。"""
    from aiops_diagnostics.conversation.conversation_store import ConversationStore

    return ConversationStore(database_file)


def main() -> int:
    settings = GatewayServerSettings.from_env()
    # 部署身份必填（#879）：本启动器绕过 `gateway_server serve`，所以那道启动前置要自己过。
    # 判据与它同一份实现；少了这一行，「隔离栈」就成了唯一一个不知道自己在哪个环境的部署。
    try:
        require_deployment_identity(settings.environment)
    except ValueError as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr, flush=True)
        return 2
    store = GatewayStore(settings.database_file)
    platform_resolver = None
    if os.environ.get("AIOPS_M2_PLATFORM_CONSUMER") == "1":
        from aiops_diagnostics.conversation.faq import PlatformIdentityResolver

        platform_resolver = PlatformIdentityResolver(LocalPlatformDirectory())
    app = create_gateway_app(
        settings=settings,
        store=store,
        caller_resolver=LocalResolver(),
        order_authorizer=SyntheticOrderAuthorizer(),
        platform_resolver=platform_resolver,
    )
    # 无关面（FAQ/会话）不参与本切片：内存实现足够，避免去连真实 Redis。
    app.state.gateway.conversation_store = SQLiteConversationStore(settings.database_file)
    # 选单数据源（APP-RUNTIME-01）：只替换**数据**，不替换选单逻辑。见模块文档第 4 条。
    import aiops_diagnostics.gateway_api as _gateway_api

    _gateway_api.scoped_mysql_source = _synthetic_order_source
    print(
        json.dumps(
            {
                "substituted": {
                    "caller_identity": "fixed local token -> fixed consumer ScopeContext",
                    "order_authorization": "synthetic_orders table in the same SQLite file",
                    "order_selection_source": "synthetic_orders rows; range and ordering logic unchanged",
                    "routing_classifier": "not exercised by the deterministic status branch",
                    "platform_directory": (
                        "consumer-only synthetic directory"
                        if platform_resolver is not None
                        else "not substituted (real UPMS directory)"
                    ),
                    "conversation_store": "in-memory stub",
                },
                "real": [
                    "delegated route",
                    "internal claim route",
                    "agent run store / lifecycle",
                    "read_charging_status",
                    "field trimming",
                ],
                "bind": [settings.bind_host, settings.port],
                "environment": settings.execution_environment,
            }
        ),
        flush=True,
    )
    uvicorn.run(app, host=settings.bind_host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
