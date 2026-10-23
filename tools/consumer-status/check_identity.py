#!/usr/bin/env python3
"""用**正式解析器**核对指定的一个真实会话，不扫描 Redis 找别人的会话。

只回答四件事，且**每条都为假就当场停**：
  1. 这个会话能被生产 `RedisThirdSessionResolver` 接受吗；
  2. 它真的是 consumer / self / **非代查** / 有 C 端主体吗；
  3. `aiops:diagnoses:write` 与 `aiops:orders:read` 两次解析的**范围指纹一致**吗；
  4. 该主体有没有**本人**订单。

**不构造 app**（那会调用 `recover_interrupted_jobs`，把在飞作业标 failed）。
身份与范围全部来自生产解析器与生产 SQL 查询函数，不重新实现规则。

退出码（三个 consumer-status 脚本统一）：
  0 通过 / 1 判据不过 / 2 前提缺失、无法判定

历史缺陷（本文件的第一版）：只看 `scope_type == "self"` 就判
`consumer_self_non_delegated`，于是 `delegated=true`、`orders:read` 被拒、
范围指纹不同、乃至**没有本人订单**都仍然 `return 0`。判据比事实宽，就会报假通过。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

EXIT_PASS, EXIT_FAIL, EXIT_UNVERIFIED = 0, 1, 2


def _resolve(resolver, settings, session: str, scope_name: str):
    return resolver.resolve(
        settings.third_session_service_token,
        required_scope=scope_name,
        third_session=session,
        platform_entry="consumer",
    )


def identity_shape(*, scope_type: str, delegated: bool, c_user_id: str) -> dict[str, object]:
    """身份判据的**唯一实现**。

    抽成纯函数不是为了让代码好看：第一版把这三条写在 `main()` 里，测试只能在
    测试里**重算一遍同样的表达式**，于是"判据被改松"这件事测试根本看不见
    （变异复验时它照样全绿）。判据必须只有一处，测试驱动那一处。
    """
    c_present = bool(c_user_id)
    # 三条**同时**成立才算"本人、非代查"：只看 scope_type 会把代查也读成 self。
    return {
        "scope_type": scope_type,
        "delegated": delegated,
        "subject_c_user_id_present": c_present,
        "consumer_self_non_delegated": (scope_type == "self" and not delegated and c_present),
    }


def prerequisite_failures(
    *, shape: dict[str, object], orders_read_scope: bool, fingerprints_match: bool
) -> list[str]:
    """前提清单。任一为假都**不得**进入业务查询，也不得报通过。"""
    checks = {
        **shape,
        "orders_read_scope": orders_read_scope,
        "orders_read_fingerprint_matches": fingerprints_match,
    }
    return [
        name
        for name in (
            "consumer_self_non_delegated",
            "subject_c_user_id_present",
            "orders_read_scope",
            "orders_read_fingerprint_matches",
        )
        if not checks.get(name)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", default="/etc/aiops-41/production.env")
    parser.add_argument("--session-file", default=None)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.third_session_auth import (
        RedisThirdSessionResolver,
        ThirdSessionSettings,
    )

    # 会话从环境变量取：41 的 Gateway 单元开了 PrivateTmp，/tmp 落盘的文件
    # `runuser -u aiops41` 读不到。
    session = (os.environ.get("AIOPS_CHECK_SESSION") or "").strip()
    if not session and args.session_file:
        session = pathlib.Path(args.session_file).read_text().strip()
    if not session:
        print("没有会话值（AIOPS_CHECK_SESSION 为空）", file=sys.stderr)
        return EXIT_UNVERIFIED

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

    # ---- 1) 诊断入口解析 -------------------------------------------------
    try:
        diagnosing = _resolve(resolver, settings, session, "aiops:diagnoses:write")
    except Exception as exc:  # noqa: BLE001 — 拒绝是答案
        checks["formal_resolver"] = False
        checks["resolver_error"] = type(exc).__name__
        _emit(checks, args.out, verdict=EXIT_FAIL)
        return EXIT_FAIL
    checks["formal_resolver"] = True

    # ---- 2) 身份形状：三条都要真 -----------------------------------------
    scope = getattr(diagnosing, "data_scope", None)
    scope_type = str(getattr(scope, "type", "") or "")
    subject = getattr(diagnosing, "subject", None)
    c_user_id = str(getattr(subject, "c_user_id", "") or "")
    checks.update(
        identity_shape(
            scope_type=scope_type,
            delegated=bool(getattr(diagnosing, "delegated", False)),
            c_user_id=c_user_id,
        )
    )
    checks["tenant"] = diagnosing.effective_tenant_id

    # ---- 3) 两次解析的范围指纹必须一致 -----------------------------------
    try:
        reading = _resolve(resolver, settings, session, "aiops:orders:read")
        checks["orders_read_scope"] = True
        checks["orders_read_fingerprint_matches"] = reading.scope_fingerprint == diagnosing.scope_fingerprint
    except Exception as exc:  # noqa: BLE001
        checks["orders_read_scope"] = False
        checks["orders_read_fingerprint_matches"] = False
        checks["orders_read_error"] = type(exc).__name__

    # ---- 前提不成立就地终止：不在残缺身份上继续查业务 ---------------------
    failures = prerequisite_failures(
        shape={
            k: checks.get(k)
            for k in (
                "scope_type",
                "delegated",
                "subject_c_user_id_present",
                "consumer_self_non_delegated",
            )
        },
        orders_read_scope=bool(checks.get("orders_read_scope")),
        fingerprints_match=bool(checks.get("orders_read_fingerprint_matches")),
    )
    if failures:
        checks["prerequisite_failures"] = failures
        checks["business_query_attempted"] = False
        _emit(checks, args.out, verdict=EXIT_FAIL)
        return EXIT_FAIL

    # ---- 4) 本人订单（只读 SQL，范围下推；与生产同一组调用）--------------
    from aiops_diagnostics.query_scope import resolve_query_scope
    from aiops_diagnostics.sources import scoped_mysql_source

    with scoped_mysql_source(runtime, resolve_query_scope(diagnosing)) as source:
        rows = source.list_orders(page=1, page_size=2)
    checks["business_query_attempted"] = True
    checks["has_owned_order"] = bool(rows)
    checks["owned_orders_seen"] = len(rows)
    if not rows:
        # 没有本人订单 ⇒ 验收前提不成立。**不能**报"通过"。
        checks["prerequisite_failures"] = ["has_owned_order"]
        _emit(checks, args.out, verdict=EXIT_UNVERIFIED)
        return EXIT_UNVERIFIED

    _emit(checks, args.out, verdict=EXIT_PASS)
    return EXIT_PASS


def _emit(checks: dict[str, object], out: str, *, verdict: int) -> None:
    verdict_name = {EXIT_PASS: "pass", EXIT_FAIL: "fail", EXIT_UNVERIFIED: "unverified"}[verdict]
    result = {
        "checked_at_utc": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "verdict": verdict_name,
        "exit_code": verdict,
        "checks": checks,
        "model_requests_sent": 0,
        "production_app_instantiated": False,
    }
    payload = json.dumps({"summary": result}, ensure_ascii=False, indent=1)
    print(payload)
    if out:
        pathlib.Path(out).write_text(payload + "\n")


if __name__ == "__main__":
    sys.exit(main())
