#!/usr/bin/env python3
"""开窗前固定**预期值**；开窗后用它**独立核对**页面结果。

两件事要分开说，这是本文件存在的理由：

* **固定预期**（窗口未开）：只取"该主体最新的本人订单 + 它在 CPMS 的当前状态"，
  作为之后的比对基准。此时读不到 CPMS 是**未验证**，不是失败。
* **独立核对**（拿到页面结果之后）：把页面/QA 给出的订单号、状态码、来源交给
  `--expect-order` / `--expect-status-code`，再独立读一次 CPMS 比对。
  **不一致就是失败**，不是"记录一下"。

排序证明用**独立判据**，不用同一个 `list_orders` 换个 `page_size` 再看首行：
那两次走的是同一个 `ORDER BY created_time DESC, id DESC`，首行相同只能证明
"这个选择器是确定的"，不能证明"它真的按时间/主键降序"。独立判据是：对本主体可见的
两行，用 `created_time`（同一秒时再用 `order_no` 的 `id` 部分）比较，且**必须严格递减**。

退出码（三个 consumer-status 脚本统一）：
  0 通过 / 1 判据不过 / 2 前提缺失、无法判定

历史缺陷（本文件的第一版）：捕获 CPMS 异常后仍 `return 0`；用了不存在的
`caller.user_id`（真实字段是 `caller.subject.c_user_id`）；没有按租户解析端点映射
（真实接口是 `charging_endpoint(...)`，不是直接拿 `charging_status_base_url`）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

EXIT_PASS, EXIT_FAIL, EXIT_UNVERIFIED = 0, 1, 2


def _resolve_caller(settings, runtime, session: str):
    from aiops_diagnostics.third_session_auth import (
        RedisThirdSessionResolver,
        ThirdSessionSettings,
    )

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
    return resolver.resolve(
        settings.third_session_service_token,
        required_scope="aiops:orders:read",
        third_session=session,
        platform_entry="consumer",
    )


def _sort_key(row: dict) -> tuple[str, int]:
    """(created_time, **id**)。

    生产 SQL 是 ``ORDER BY created_time DESC, id DESC``，第二排序键是**主键 id**。
    上一版用了 ``order_no`` 当第二键 —— 那是错的：同一秒里 `id=2/order_no=A` 排在
    `id=1/order_no=Z` 前面是**正确**的（id 更大），旧判据却按订单号字符串把它判反。
    `order_no` 是业务号（46 亿级数字串），与自增主键没有大小关系。
    """
    raw_id = row.get("id")
    try:
        return (str(row.get("created_time", "")), int(raw_id))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("row has no usable id") from None


def sort_verdict(rows: list[dict]) -> dict:
    """排序判据的**结论**，不是布尔。空/单行/缺 id 都不能声称"多行排序已验证"。"""
    if len(rows) < 2:
        return {
            "verdict": "unverified",
            "reason": "insufficient_rows",
            "rows_compared": len(rows),
        }
    try:
        keys = [_sort_key(r) for r in rows]
    except ValueError:
        return {
            "verdict": "unverified",
            "reason": "missing_id",
            "rows_compared": len(rows),
        }
    if all(keys[i] > keys[i + 1] for i in range(len(keys) - 1)):
        return {
            "verdict": "pass",
            "rows_compared": len(rows),
            "criterion": "(created_time, id) 严格递减",
        }
    return {
        "verdict": "fail",
        "reason": "not_strictly_descending",
        "rows_compared": len(rows),
        "keys": [[k[0], k[1]] for k in keys],
    }


def compare_with_cpms(*, page: dict, cpms: dict, expect_status_code: object = None) -> dict:
    """页面/QA 与 CPMS 的一致性判据。返回 {"consistent": bool, "reasons": [...]}。"""
    reasons: list[str] = []
    if str(cpms.get("order_no")) != str(page.get("order_no")):
        reasons.append("order_no_mismatch")
    if str(cpms.get("source")) != str(page.get("source")):
        reasons.append("source_mismatch")
    if expect_status_code is not None:
        if str(cpms.get("status_code")) != str(expect_status_code):
            reasons.append("status_code_mismatch")
    else:
        reasons.append("status_code_not_compared")
    return {"consistent": not reasons, "reasons": reasons}


class Deps:
    """外部依赖的注入口：解析器、SQL、CPMS 读取、端点解析。

    测试把它们换成替身并统计调用次数 —— 「CPMS 异常分支被改成 EXIT_PASS」
    这种改法，上一版测试完全看不见（它只读常量）。
    """

    def __init__(self, *, resolve_caller, list_orders, get_orders, read_status, endpoint):
        self.resolve_caller = resolve_caller
        self.list_orders = list_orders
        self.get_orders = get_orders
        self.read_status = read_status
        self.endpoint = endpoint
        self.cpms_calls = 0


def default_deps(*, config_file: str, session: str) -> Deps:
    from aiops_diagnostics.charging_status import read_charging_status
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings, charging_endpoint
    from aiops_diagnostics.query_scope import resolve_query_scope
    from aiops_diagnostics.sources import scoped_mysql_source

    settings = GatewayServerSettings.from_env()
    runtime = Settings.from_config(pathlib.Path(config_file))

    def resolve_caller():
        return _resolve_caller(settings, runtime, session)

    def _source(caller):
        return scoped_mysql_source(runtime, resolve_query_scope(caller))

    def list_orders(caller, *, page_size: int):
        with _source(caller) as source:
            return source.list_orders(page=1, page_size=page_size)

    def get_orders(caller, order_no: str):
        with _source(caller) as source:
            return source.get_orders(order_no)

    def read_status(endpoint, order_no, *, tenant_id, user_id):
        return read_charging_status(endpoint, order_no, tenant_id=tenant_id, user_id=user_id)

    def endpoint(tenant_id: str) -> str:
        return charging_endpoint(settings.charging_status_base_url, tenant_id)

    return Deps(
        resolve_caller=resolve_caller,
        list_orders=list_orders,
        get_orders=get_orders,
        read_status=read_status,
        endpoint=endpoint,
    )


def main(argv: list[str] | None = None, *, deps: Deps | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", default="/etc/aiops-41/production.env")
    parser.add_argument("--session-file", default=None)
    parser.add_argument("--out", default="")
    parser.add_argument("--page-size", type=int, default=5)
    # 独立核对：拿到页面/QA 的结果后，把它交进来比对。
    parser.add_argument("--expect-order", default=None)
    parser.add_argument("--expect-source", default=None)
    parser.add_argument("--expect-status-code", default=None)
    args = parser.parse_args(argv)

    session = (os.environ.get("AIOPS_CHECK_SESSION") or "").strip()
    if not session and args.session_file:
        session = pathlib.Path(args.session_file).read_text().strip()
    if not session:
        print("缺少 AIOPS_CHECK_SESSION", file=sys.stderr)
        return EXIT_UNVERIFIED

    deps = deps or default_deps(config_file=args.config_file, session=session)
    try:
        caller = deps.resolve_caller()
    except Exception as exc:  # noqa: BLE001
        print(f"会话解析失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_UNVERIFIED

    c_user_id = str(getattr(getattr(caller, "subject", None), "c_user_id", "") or "")
    if not c_user_id or bool(getattr(caller, "delegated", False)):
        print("主体不是本人（缺 C 端身份或为代查），不建立对照", file=sys.stderr)
        return EXIT_UNVERIFIED
    tenant_id = caller.effective_tenant_id

    rows = deps.list_orders(caller, page_size=args.page_size)
    if not rows:
        print("该主体没有本人订单", file=sys.stderr)
        return EXIT_UNVERIFIED

    order_no = args.expect_order or str(rows[0]["order_no"])

    # 指定单**不在第一页**不等于不属于本人：按号单独核归属（范围下推同样生效）。
    chosen = next((r for r in rows if str(r["order_no"]) == order_no), None)
    ownership = {"checked_by": "list_page"}
    if chosen is None:
        owned = [r for r in deps.get_orders(caller, order_no) if str(r["order_no"]) == order_no]
        if not owned:
            print("订单不在该主体可见范围内 ⇒ 拒绝", file=sys.stderr)
            return EXIT_FAIL
        chosen = owned[0]
        ownership = {"checked_by": "by_order_no"}

    ordering = sort_verdict(rows)

    # ---- 端点：按租户解析；缺映射 ⇒ 未验证，绝不回退 OPS ------------------
    try:
        endpoint = deps.endpoint(tenant_id)
    except ValueError:
        print(
            "个人状态地址缺失或该租户未注册（TENANT_ENDPOINT_UNRESOLVED）；不回退运维 OPS 端点 ⇒ 未验证",
            file=sys.stderr,
        )
        return EXIT_UNVERIFIED

    try:
        deps.cpms_calls += 1
        cpms = deps.read_status(endpoint, order_no, tenant_id=tenant_id, user_id=c_user_id)
    except Exception as exc:  # noqa: BLE001
        print(f"CPMS 读取失败：{type(exc).__name__} ⇒ 未验证", file=sys.stderr)
        return EXIT_UNVERIFIED

    # ---- 固定预期 vs 一致性通过：两件事分别记 ---------------------------
    comparison = {"state": "not_compared", "note": "只固定了预期值，未做比对"}
    verdict = EXIT_PASS
    if args.expect_source or args.expect_status_code is not None:
        diff = compare_with_cpms(
            page={"order_no": order_no, "source": args.expect_source or ""},
            cpms=cpms,
            expect_status_code=args.expect_status_code,
        )
        comparison = {"state": "compared", **diff}
        if not diff["consistent"]:
            verdict = EXIT_FAIL

    if ordering["verdict"] == "fail":
        verdict = EXIT_FAIL
    elif ordering["verdict"] == "unverified" and verdict == EXIT_PASS:
        # 行数不够：**不声称**多行排序已验证。
        verdict = EXIT_UNVERIFIED

    out: dict[str, object] = {
        "checked_at_utc": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "tenant": tenant_id,
        "order_no_prefix": order_no[:6] + "…",
        "expected_fixed": {
            "order_no": order_no,
            "observed_at": cpms.get("observed_at"),
            "status_code": cpms.get("status_code"),
            "source": cpms.get("source"),
        },
        "ordering": ordering,
        "ownership": ownership,
        "comparison": comparison,
        "cpms_calls": deps.cpms_calls,
        "model_requests_sent": 0,
    }
    payload = json.dumps(out, ensure_ascii=False, indent=1)
    print(payload)
    if args.out:
        pathlib.Path(args.out).write_text(payload + "\n")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
