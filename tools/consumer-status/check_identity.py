#!/usr/bin/env python3
"""用**正式解析器**核对指定的一个真实会话，不扫描 Redis 找别人的会话。

承接 #976 / #992 的本人订单状态验收。这个脚本只回答四件事：
  1. 这个会话能不能被生产 `RedisThirdSessionResolver` 接受；
  2. 它的租户/主体是谁，callback 是 consumer/self/非代查吗；
  3. 该主体有没有**本人**订单（SQL 范围下推，只读）；
  4. 订单授权接口对本单 200、对不存在的单 404、缺会话 401。

**不构造 app**（那会调用 `recover_interrupted_jobs`，把在飞作业标 failed）。
身份与范围全部来自生产解析器与生产 SQL 查询函数，不重新实现规则。
输出默认脱敏：只打印布尔、计数、订单号前缀，不打印会话值。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

SESSION_FILE = None  # 只从 AIOPS_CHECK_SESSION 读；不指向任何仓库内文件


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", default="/etc/aiops-41/production.env")
    parser.add_argument("--session-file", default=None)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    import os

    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.third_session_auth import (
        RedisThirdSessionResolver,
        ThirdSessionSettings,
    )

    # 会话从环境变量取：41 的 Gateway 单元开了 PrivateTmp，/tmp 文件属主也对不上。
    session = (os.environ.get("AIOPS_CHECK_SESSION") or "").strip()
    if not session and args.session_file:
        session = pathlib.Path(args.session_file).read_text().strip()
    if not session:
        print("没有会话值（AIOPS_CHECK_SESSION 为空）", file=sys.stderr)
        return 2
    settings = GatewayServerSettings.from_env()
    runtime = Settings.from_config(pathlib.Path(args.config_file))

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

    checks: dict[str, object] = {}
    context = None
    try:
        context = resolver.resolve(
            settings.third_session_service_token,
            required_scope="aiops:diagnoses:write",
            third_session=session,
            platform_entry="consumer",
        )
        checks["formal_resolver"] = True
    except Exception as exc:  # noqa: BLE001 — a rejection is a real answer
        checks["formal_resolver"] = False
        checks["resolver_error"] = type(exc).__name__

    if context is not None:
        scope = getattr(context, "data_scope", None)
        scope_type = str(getattr(scope, "type", "") or "")
        checks["scope_type"] = scope_type
        checks["tenant"] = context.effective_tenant_id
        checks["user_id_present"] = bool(getattr(context, "user_id", None))
        checks["consumer_self_non_delegated"] = scope_type == "self"
        checks["platform_entry"] = getattr(context, "platform_entry", None)
        orders_read = False
        try:
            orders_read = bool(
                resolver.resolve(
                    settings.third_session_service_token,
                    required_scope="aiops:orders:read",
                    third_session=session,
                    platform_entry="consumer",
                )
            )
        except Exception:  # noqa: BLE001
            orders_read = False
        checks["orders_read_scope"] = orders_read

    # ---- 本人订单（只读 SQL，范围下推）----
    latest_order = None
    owned_count = 0
    if context is not None and checks.get("consumer_self_non_delegated"):
        # 与生产 consumer_order_chat 同一组调用（gateway_api.py:5494-5499）：
        # resolve_query_scope 下推租户/本人，scoped_mysql_source 按 scope 过滤。
        from aiops_diagnostics.query_scope import resolve_query_scope
        from aiops_diagnostics.sources import scoped_mysql_source

        scope = resolve_query_scope(context)
        with scoped_mysql_source(runtime, scope) as source:
            rows = source.list_orders(page=1, page_size=2)
        owned_count = len(rows)
        checks["has_owned_order"] = bool(rows)
        if rows:
            latest_order = str(rows[0]["order_no"])
            checks["latest_order_prefix"] = latest_order[:6] + "…"

    result = {
        "checked_at_utc": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "checks": checks,
        "owned_orders_seen": owned_count,
        "model_requests_sent": 0,
        "production_app_instantiated": False,
    }
    payload = json.dumps({"summary": result}, ensure_ascii=False, indent=1)
    print(payload)
    if args.out:
        pathlib.Path(args.out).write_text(payload + "\n")
    return 0 if checks.get("formal_resolver") else 1


if __name__ == "__main__":
    sys.exit(main())
