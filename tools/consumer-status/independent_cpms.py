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


def _sort_key(row: dict) -> tuple[str, str]:
    """(created_time, id 部分)。同一秒时用主键区分，与 SQL 的 ORDER BY 同序。"""
    return (str(row.get("created_time", "")), str(row.get("order_no", "")))


def strict_descending(rows: list[dict]) -> bool:
    """独立判据：相邻两行必须**严格**递减。相等说明排序没生效或主键不唯一。"""
    keys = [_sort_key(r) for r in rows]
    return all(keys[i] > keys[i + 1] for i in range(len(keys) - 1))


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-file", default="/etc/aiops-41/production.env")
    parser.add_argument("--session-file", default=None)
    parser.add_argument("--out", default="")
    # 独立核对：拿到页面/QA 的结果后，把它交进来比对。
    parser.add_argument("--expect-order", default=None)
    parser.add_argument("--expect-source", default=None)
    parser.add_argument("--expect-status-code", default=None)
    args = parser.parse_args()

    session = (os.environ.get("AIOPS_CHECK_SESSION") or "").strip()
    if not session and args.session_file:
        session = pathlib.Path(args.session_file).read_text().strip()
    if not session:
        print("缺少 AIOPS_CHECK_SESSION", file=sys.stderr)
        return EXIT_UNVERIFIED

    from aiops_diagnostics.charging_status import read_charging_status
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_config import GatewayServerSettings, charging_endpoint
    from aiops_diagnostics.query_scope import resolve_query_scope
    from aiops_diagnostics.sources import scoped_mysql_source

    settings = GatewayServerSettings.from_env()
    runtime = Settings.from_config(pathlib.Path(args.config_file))
    try:
        caller = _resolve_caller(settings, runtime, session)
    except Exception as exc:  # noqa: BLE001
        print(f"会话解析失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_UNVERIFIED

    c_user_id = str(getattr(getattr(caller, "subject", None), "c_user_id", "") or "")
    if not c_user_id or bool(getattr(caller, "delegated", False)):
        print("主体不是本人（缺 C 端身份或为代查），不建立对照", file=sys.stderr)
        return EXIT_UNVERIFIED

    with scoped_mysql_source(runtime, resolve_query_scope(caller)) as source:
        rows = source.list_orders(page=1, page_size=3)
    if not rows:
        print("该主体没有本人订单", file=sys.stderr)
        return EXIT_UNVERIFIED

    order_no = args.expect_order or str(rows[0]["order_no"])
    chosen = next((r for r in rows if str(r["order_no"]) == order_no), None)
    if chosen is None:
        # 指定的单不在本人可见范围内 —— 这是**拒绝**，不是"未验证"。
        print(f"订单 {order_no[:6]}… 不在该主体可见范围内", file=sys.stderr)
        return EXIT_FAIL

    # ---- 端点：必须按租户解析，缺映射就是未验证，绝不回退 OPS ------------
    try:
        endpoint = charging_endpoint(settings.charging_status_base_url, caller.effective_tenant_id)
    except ValueError:
        print(
            "个人状态地址缺失或该租户未注册（TENANT_ENDPOINT_UNRESOLVED）；不回退运维 OPS 端点 ⇒ 未验证",
            file=sys.stderr,
        )
        return EXIT_UNVERIFIED

    try:
        cpms = read_charging_status(
            endpoint,
            order_no,
            tenant_id=caller.effective_tenant_id,
            user_id=c_user_id,
        )
    except Exception as exc:  # noqa: BLE001
        # 读不到就是**没验**；旧版在这里 return 0，等于把"没验"报成"通过"。
        print(f"CPMS 读取失败：{type(exc).__name__} ⇒ 未验证", file=sys.stderr)
        return EXIT_UNVERIFIED

    ordering_ok = strict_descending(rows)
    out: dict[str, object] = {
        "checked_at_utc": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "tenant": caller.effective_tenant_id,
        "order_no_prefix": order_no[:6] + "…",
        "ordering": {
            "rows_compared": len(rows),
            "created_time": [str(r.get("created_time", "")) for r in rows],
            "strictly_descending": ordering_ok,
            "criterion": "对本人可见行比较 (created_time, order_no)，必须严格递减",
        },
        "cpms": cpms,
        "model_requests_sent": 0,
    }

    verdict = EXIT_PASS
    if not ordering_ok:
        out["ordering_failure"] = "可见行不是严格递减 —— 排序判据不成立"
        verdict = EXIT_FAIL

    # ---- 独立核对：只有给了预期值才比对 ---------------------------------
    if args.expect_order or args.expect_source or args.expect_status_code is not None:
        page = {
            "order_no": args.expect_order or order_no,
            "source": args.expect_source or "",
        }
        diff = compare_with_cpms(page=page, cpms=cpms, expect_status_code=args.expect_status_code)
        out["comparison"] = diff
        if not diff["consistent"]:
            verdict = EXIT_FAIL
    else:
        out["comparison"] = {"consistent": None, "note": "只固定了预期值，未做比对"}

    payload = json.dumps(out, ensure_ascii=False, indent=1)
    print(payload)
    if args.out:
        pathlib.Path(args.out).write_text(payload + "\n")
    return verdict


if __name__ == "__main__":
    sys.exit(main())
