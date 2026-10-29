#!/usr/bin/env python3
"""列出该会话可见的本人订单（只读，最多 5 条），用来挑第二张真实订单做对照。

会话从 `AIOPS_CHECK_SESSION` 读。与生产 `consumer_order_chat` 同一条选单路径：
`resolve_query_scope` 下推租户/本人，`scoped_mysql_source` 过滤。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


def main() -> int:
    session = (os.environ.get("AIOPS_CHECK_SESSION") or "").strip()
    if not session:
        print("缺少 AIOPS_CHECK_SESSION", file=sys.stderr)
        return 2

    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.identity.query_scope import resolve_query_scope
    from aiops_diagnostics.identity.third_session_auth import (
        RedisThirdSessionResolver,
        ThirdSessionSettings,
    )
    from aiops_diagnostics.sources import scoped_mysql_source

    settings = GatewayServerSettings.from_env()
    runtime = Settings.from_config(pathlib.Path("/etc/aiops-41/production.env"))
    resolver = RedisThirdSessionResolver(
        ThirdSessionSettings(
            host=runtime.redis.host,
            port=runtime.redis.port,
            database=runtime.redis.database,
            username=runtime.redis.user,
            password=runtime.redis.password,
            service_token=settings.third_session_service_token,
            key_prefix=settings.third_session_key_prefix,
        )
    )
    caller = resolver.resolve(
        settings.third_session_service_token,
        required_scope="aiops:orders:read",
        third_session=session,
        platform_entry="consumer",
    )
    with scoped_mysql_source(runtime, resolve_query_scope(caller)) as source:
        rows = source.list_orders(page=1, page_size=5)
    print(
        json.dumps(
            [{"order_no": str(r["order_no"]), "created": str(r.get("created_time", ""))} for r in rows],
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
