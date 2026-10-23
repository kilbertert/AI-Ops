#!/usr/bin/env python3
"""开窗前先取**独立对照值**：最新本人订单 + 该单 CPMS 状态。

这是验收的锚点。**窗口开着时不去用它下判断** —— 否则看到什么记什么，
判据就退化成"和实现一致就算对"。这里先跑一次，把预期值固定下来。

只读：一次 scope 下推的 SQL 选单 + 一次 CPMS GET。零模型调用。
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

    from aiops_diagnostics.charging_status import read_charging_status
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.query_scope import resolve_query_scope
    from aiops_diagnostics.sources import scoped_mysql_source
    from aiops_diagnostics.third_session_auth import (
        RedisThirdSessionResolver,
        ThirdSessionSettings,
    )

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

    scope = resolve_query_scope(caller)
    with scoped_mysql_source(runtime, scope) as source:
        latest_rows = source.list_orders(page=1, page_size=1)
        # 再取一遍 page_size=3 证明第一条确实是排序第一条，不是唯一一条。
        top3 = source.list_orders(page=1, page_size=3)

    if not latest_rows:
        print(json.dumps({"error": "no owned order"}, ensure_ascii=False))
        return 1

    order_no = str(latest_rows[0]["order_no"])

    # CPMS 独立读取：用 OPS 那套地址？不行 —— 那是整租户身份。
    # 个人状态地址取的是个人 CPMS；这里用与生产同一条个人链路所需的配置，
    # 窗口未开时 charging_status_base_url 为空，因此**先只记录地址来源**。
    base = settings.charging_status_base_url
    cpms = None
    cpms_note = None
    if base:
        try:
            cpms = read_charging_status(
                base,
                order_no,
                tenant_id=caller.effective_tenant_id,
                user_id=str(getattr(caller, "user_id", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            cpms_note = f"{type(exc).__name__}: {exc}"[:200]
    else:
        cpms_note = "个人状态地址未配置（窗口未开），本轮先记录订单选择"

    out = {
        "checked_at_utc": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "tenant": caller.effective_tenant_id,
        "scope_type": str(getattr(getattr(caller, "data_scope", None), "type", "") or ""),
        "latest_order_no": order_no,
        "top3_order_nos_prefix": [str(r["order_no"])[:6] + "…" for r in top3],
        "top3_created_time": [str(r.get("created_time", "")) for r in top3],
        "top3_is_latest_first": order_no == str(top3[0]["order_no"]),
        "cpms": cpms,
        "cpms_note": cpms_note,
        "model_requests_sent": 0,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    # 只打印；不写任何临时文件 —— 订单号属于私有数据，调用方自己决定存哪。
    return 0


if __name__ == "__main__":
    sys.exit(main())
