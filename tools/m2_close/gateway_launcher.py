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
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TOKEN = os.environ["AIOPS_M2_LOCAL_TOKEN"]
TENANT = os.environ.get("AIOPS_M2_TENANT", "TENANT-A")
C_USER = os.environ.get("AIOPS_M2_C_USER", "USER-A")
ORDERS_DB = Path(os.environ["AIOPS_M2_ORDERS_DB"])
#: 第二个调用者身份（可选）：用于「两个用户各自的订单交错执行」那一类判据。
OTHER_TOKEN = os.environ.get("AIOPS_M2_SECOND_TOKEN", "")
OTHER_C_USER = os.environ.get("AIOPS_M2_SECOND_C_USER", "USER-B")


@dataclass
class LocalResolver:
    """把 ``AIOPS_M2_LOCAL_TOKEN`` 换成固定身份；另有第二个令牌映射到另一个 C 端用户。

    第二个身份只为一个判据存在：**两个用户各自的订单交错执行时结果不串**（IC-04）。
    没有它，这条判据只能在同一身份下做，测不到「别人的单会不会串进来」。
    """

    def resolve(self, token: str, *, required_scope: str, **kwargs: Any) -> ScopeContext:
        from aiops_diagnostics.caller_auth import CallerAuthError

        if token == TOKEN:
            c_user = C_USER
        elif OTHER_TOKEN and token == OTHER_TOKEN:
            c_user = OTHER_C_USER
        else:
            raise CallerAuthError("invalid access token", code="caller_auth.invalid")
        subject = SubjectRecord(b_user_id="", c_user_id=c_user, tenant_id=TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=TENANT,
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
        from aiops_diagnostics.caller_auth import CallerAuthError

        if self.unavailable:
            raise CallerAuthError("unavailable", code="caller_auth.unavailable")
        if context.data_scope.type != "self" or not context.subject.c_user_id:
            return False
        connection = sqlite3.connect(ORDERS_DB)
        try:
            row = connection.execute(
                "SELECT user_id, tenant_id FROM synthetic_orders WHERE order_no = ?", (order_no,)
            ).fetchone()
        finally:
            connection.close()
        return bool(row and row[0] == context.subject.c_user_id and row[1] == context.effective_tenant_id)


def SQLiteConversationStore(database_file: Path):  # noqa: N802 - 构造器语义
    """本切片不碰会话存储：用仓库自己的实现指向同一个库，不新增替身。"""
    from aiops_diagnostics.conversation_store import ConversationStore

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
    app = create_gateway_app(
        settings=settings,
        store=store,
        caller_resolver=LocalResolver(),
        order_authorizer=SyntheticOrderAuthorizer(),
    )
    # 无关面（FAQ/会话）不参与本切片：内存实现足够，避免去连真实 Redis。
    app.state.gateway.conversation_store = SQLiteConversationStore(settings.database_file)
    print(
        json.dumps(
            {
                "substituted": {
                    "caller_identity": "fixed local token -> fixed consumer ScopeContext",
                    "order_authorization": "synthetic_orders table in the same SQLite file",
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
