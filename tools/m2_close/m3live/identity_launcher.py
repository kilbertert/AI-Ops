#!/usr/bin/env python3
"""隔离桩上的 Gateway 启动器：**只替换调用者身份**，订单授权仍是真实判定。

为什么需要它（与 `tools/m2_close/gateway_launcher.py` 的区别）：

| 替身 | `gateway_launcher.py`（合成验收用） | 本文件（真实只读链路用） |
|---|---|---|
| 调用者身份 | 固定 `ScopeContext` | 固定 `ScopeContext`（**同样构造**） |
| 订单授权 | SQLite `synthetic_orders` 表 | **真实 `ScopedOrderAuthorizer`**：按调用者范围查业务库 |
| 读业务 | 回环桩 CPMS | **真实 CPMS**（由 gateway.env 指向） |

也就是说：这里唯一被替换的是「谁来发起这次请求」。它能成立的**前提**是调用者身份里的
租户与 C 端用户是**真实存在、且真的拥有那个订单**的 —— 否则真实授权器会给出 404，
链路根本不会往下走。这不是放宽判据；它让真实判据继续生效。

**为什么要构造身份**：本部署只启用 `thirdSession` 会话链，而 Redis 里是真实客户会话。
借用任何一条都是冒充真实客户，不做（见 `docs/validation.md` 的既有记录）。构造身份
意味着「本次请求以某个已声明的范围发起」，并在报告里**逐条写明**这一环是构造的。

用法（容器内）：

    AIOPS_M2_LOCAL_TOKEN=…  AIOPS_M2_TENANT=…  AIOPS_M2_C_USER=… \\
        python tools/m2_close/m3live/identity_launcher.py

`AIOPS_M2_LOCAL_TOKEN` 只是「谁能发起这条构造身份」的握手令牌，不是任何真实凭据。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import uvicorn

from aiops_diagnostics.caller_auth import CallerAuthError
from aiops_diagnostics.deployment_environment import require_deployment_identity
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

TOKEN = os.environ["AIOPS_M2_LOCAL_TOKEN"]
TENANT = os.environ["AIOPS_M2_TENANT"]
C_USER = os.environ["AIOPS_M2_C_USER"]

#: 构造身份**只**授予这一个 scope：这次试运行要用的读取入口需要的那个。
#: 早先的实现把路由要什么就授予什么（`permissions={required_scope}`），于是拿着握手令牌
#: 就能进管理路由（`aiops:agents:manage` / `aiops:shortcuts:manage`）—— 一把「只读验证」
#: 的钥匙开到了管理面。**白名单**而不是「照给」：不在集合里的 scope 一律拒绝。
ALLOWED_SCOPES = frozenset({"aiops:orders:read"})


@dataclass
class ConstructedIdentity:
    """把握手令牌换成固定的 ``ScopeContext``；其余一律不碰。

    ``data_scope=self`` 是 consumer 入口的既有范围：按本人记录过滤。真实
    `ScopedOrderAuthorizer` 会拿这个范围去业务库核对订单归属 —— 这一步仍是真的。
    """

    def resolve(self, token: str, *, required_scope: str, **kwargs: Any) -> ScopeContext:
        if token != TOKEN:
            raise CallerAuthError("invalid access token", code="caller_auth.invalid")
        if required_scope not in ALLOWED_SCOPES:
            # 只读验证不需要管理 scope；照给等于让握手令牌开管理面。
            raise CallerAuthError("scope not granted to the constructed identity", code="caller_auth.invalid")
        subject = SubjectRecord(b_user_id="", c_user_id=C_USER, tenant_id=TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=TENANT,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=ALLOWED_SCOPES,
            resolved_at=datetime.now(UTC),
        )


def main() -> int:
    # **隔离门禁先于一切**：`create_gateway_app()` 打开库之前就会写状态，而本启动器
    # 绕过了 `gateway_server serve`（那里面才有这道门）。自己补上，判据与那里同一份实现 ——
    # 少了它，「隔离栈」就只是名字。
    from aiops_diagnostics.isolation_guard import IsolationError, enforce_from_env

    try:
        enforce_from_env()
    except IsolationError as exc:
        print(json.dumps({"refused": f"isolation: {exc}"}), file=sys.stderr, flush=True)
        return 2

    settings = GatewayServerSettings.from_env()
    # 部署身份必填（#879）：本启动器同样绕过 `gateway_server serve`。判据只有一处实现。
    try:
        require_deployment_identity(settings.environment)
    except ValueError as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr, flush=True)
        return 2
    store = GatewayStore(settings.database_file)
    # **不传 order_authorizer**：工厂会用真实的 `ScopedOrderAuthorizer`（读业务库）。
    # 这条是本节与 `gateway_launcher.py` 的关键区别，改动它会悄悄退化成合成验收。
    app = create_gateway_app(settings=settings, store=store, caller_resolver=ConstructedIdentity())
    authorizer = app.state.gateway.order_authorizer
    if type(authorizer).__name__ != "ScopedOrderAuthorizer":
        print(
            json.dumps(
                {"refused": "order authorizer is not the real one", "actual": type(authorizer).__name__}
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(
        json.dumps(
            {
                "substituted": {
                    "caller_identity": "constructed ScopeContext (no session borrowed)",
                },
                "real": [
                    "delegated route",
                    "internal claim route",
                    "completion route",
                    "agent run store / lifecycle",
                    "order authorization (ScopedOrderAuthorizer -> business DB)",
                    "read_charging_status (real CPMS)",
                    "execution manifest identity",
                ],
                "scope": {"tenant": TENANT, "c_user": C_USER, "data_scope": "self"},
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
