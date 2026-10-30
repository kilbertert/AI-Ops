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

SCOPE_DIAGNOSES = "aiops:diagnoses:write"
SCOPE_ORDERS_READ = "aiops:orders:read"


def _resolve(resolver, session: str, scope_name: str):
    """用**已构造好的** resolver 解析一次。

    令牌不在这里取：它属于 resolver 的构造参数（生产侧由 `default_deps` 注进去），
    测试侧由替身自己认。把令牌从这里读，就等于测试也必须伪造一个 Settings。
    """
    return resolver.resolve(
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


class Deps:
    """外部依赖的唯一注入口。

    两侧都要能换：**生产**用真解析器与真 SQL；**测试**换成隔离替身并统计调用次数。
    上一版的测试只读常量和自造变量，于是"把无订单分支改成 EXIT_PASS"这种改法
    测试完全看不见（Codex 复现过）。判据必须能被**真实执行路径**驱动。
    """

    def __init__(self, *, make_resolver, make_source, session: str, service_token: str):
        self.make_resolver = make_resolver
        self.make_source = make_source
        self.session = session
        # 令牌只在这里落一次：生产由 Settings 注入，测试由替身自认。
        self.service_token = service_token
        self.business_query_calls = 0

    def resolver(self):
        return self.make_resolver()

    def list_orders(self, caller) -> list[dict]:
        self.business_query_calls += 1
        return self.make_source(caller)


def default_deps(*, config_file: str, session: str) -> Deps:
    """生产实现：真 `RedisThirdSessionResolver` + 真 `scoped_mysql_source`。"""
    from aiops_diagnostics.common.config import Settings
    from aiops_diagnostics.data.sources import scoped_mysql_source
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.identity.query_scope import resolve_query_scope
    from aiops_diagnostics.identity.third_session_auth import (
        RedisThirdSessionResolver,
        ThirdSessionSettings,
    )

    settings = GatewayServerSettings.from_env()
    runtime = Settings.from_config(pathlib.Path(config_file))

    def make_resolver():
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

        class _Bound:
            def resolve(self, *, required_scope, third_session, platform_entry):
                return resolver.resolve(
                    settings.third_session_service_token,
                    required_scope=required_scope,
                    third_session=third_session,
                    platform_entry=platform_entry,
                )

        return _Bound()

    def make_source(caller) -> list[dict]:
        with scoped_mysql_source(runtime, resolve_query_scope(caller)) as source:
            return source.list_orders(page=1, page_size=30)

    return Deps(
        make_resolver=make_resolver,
        make_source=make_source,
        session=session,
        service_token=settings.third_session_service_token,
    )


def main(argv: list[str] | None = None, *, deps: Deps | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", default="/etc/aiops-41/production.env")
    parser.add_argument("--session-file", default=None)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    # 会话从环境变量取：41 的 Gateway 单元开了 PrivateTmp，/tmp 落盘的文件
    # `runuser -u aiops41` 读不到。
    session = (os.environ.get("AIOPS_CHECK_SESSION") or "").strip()
    if not session and args.session_file:
        session = pathlib.Path(args.session_file).read_text().strip()
    if not session:
        print("没有会话值（AIOPS_CHECK_SESSION 为空）", file=sys.stderr)
        return EXIT_UNVERIFIED

    deps = deps or default_deps(config_file=args.config_file, session=session)
    resolver = deps.resolver()

    checks: dict[str, object] = {}

    # ---- 1) 诊断入口解析 -------------------------------------------------
    try:
        diagnosing = _resolve(resolver, deps.session, SCOPE_DIAGNOSES)
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
        reading = _resolve(resolver, deps.session, SCOPE_ORDERS_READ)
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
    rows = deps.list_orders(diagnosing)
    checks["business_query_attempted"] = True
    checks["business_query_calls"] = deps.business_query_calls
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
