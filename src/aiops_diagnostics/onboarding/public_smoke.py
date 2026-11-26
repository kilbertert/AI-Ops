"""标准化三能力验收问句与发布后公网正式 HTTPS 4 项冒烟（ADR-0357 P0-2）。

1. 固化三能力标准验收问句模板，确保订单验收显式要求“充电状态、结束原因与关键过程统计”，
   防止因问句遗漏导致模型跳过 ``cpms_order_process_stats`` 触发假失败；
2. 正式发布后对客户公网域名 ``https://<host>`` 自动执行 4 项端到端冒烟：
   - 匿名 ``POST /v1/assistant/questions`` 必须返回 ``HTTP 401``；
   - 客户既有路由 ``GET /upms/tenant/list`` 必须返回 ``HTTP 200``；
   - 可选 C 端（内存瞬时 ``third-session``）提交 ``202`` 并轮询至 ``200 completed``；
   - 可选管家端（内存瞬时 ``Bearer`` + ``X-Business-Entry: operator``）
     提交 ``202`` 并轮询至 ``200 completed``。
   所有凭据仅在内存中瞬时使用，严禁持久化或回显至返回结果。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

HttpCaller = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, str]]


def build_standard_validation_questions(
    *,
    order_no: str,
    device_id: str,
    start: str,
    end: str,
    consumer_order_no: str,
    consumer_faq_prompt: str = "充电时枪拔不出来怎么处理？",
) -> dict[str, dict[str, str]]:
    """构造覆盖全工具证据要求的标准三能力验收问句集合。"""
    order_id = order_no.strip()
    dev_id = device_id.strip()
    c_order = consumer_order_no.strip()
    if not order_id or not dev_id or not c_order or not start.strip() or not end.strip():
        raise ValueError("validation sample identifiers and time window are required")
    return {
        "consumer": {
            "order_no": c_order,
            "question": f"{consumer_faq_prompt.strip()} 顺便帮我查一下订单 {c_order} 的当前状态。",
        },
        "order": {
            "order_no": order_id,
            "question": (f"请核查订单 {order_id} 的充电状态、结束原因，并给出电压/电流/功率等关键过程统计。"),
        },
        "offline": {
            "device_id": dev_id,
            "start": start.strip(),
            "end": end.strip(),
            "question": (
                f"请查询设备 {dev_id} 在 {start.strip()} 至 {end.strip()} "
                "期间的离线记录并统计离线次数与时长。"
            ),
        },
    }


def _default_http_caller(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> tuple[int, str]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        return 0, json.dumps({"error": type(exc).__name__})


def _poll_formal_qa(
    caller: HttpCaller,
    base: str,
    headers: dict[str, str],
    question: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    sleep_fn: Callable[[float], None],
) -> dict[str, Any]:
    post_headers = {**headers, "Content-Type": "application/json"}
    st, txt = caller("POST", f"{base}/v1/assistant/questions", post_headers, {"question": question})
    if st not in (200, 202):
        return {
            "passed": False,
            "http_status": st,
            "reason": "POST_REJECTED",
            "qa_id": "",
        }
    try:
        data = json.loads(txt)
    except ValueError:
        return {"passed": False, "http_status": st, "reason": "INVALID_JSON", "qa_id": ""}
    qa_id = str(data.get("qa_id") or "")
    status = str(data.get("status") or "")
    if not qa_id:
        return {"passed": False, "http_status": st, "reason": "MISSING_QA_ID", "qa_id": ""}
    if status == "completed" and not data.get("error"):
        return {"passed": True, "http_status": st, "status": "completed", "qa_id": qa_id}

    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_http = st
    last_status = status
    while time.monotonic() < deadline:
        sleep_fn(poll_interval_seconds)
        pst, ptxt = caller("GET", f"{base}/v1/assistant/questions/{qa_id}", headers, None)
        last_http = pst
        if pst >= 400 or pst == 0:
            return {
                "passed": False,
                "http_status": pst,
                "reason": "POLL_HTTP_ERROR",
                "qa_id": qa_id,
            }
        try:
            pdata = json.loads(ptxt)
        except ValueError:
            return {"passed": False, "http_status": pst, "reason": "INVALID_POLL_JSON", "qa_id": qa_id}
        last_status = str(pdata.get("status") or "")
        if last_status == "completed":
            has_error = bool(pdata.get("error"))
            return {
                "passed": not has_error,
                "http_status": pst,
                "status": last_status,
                "qa_id": qa_id,
                **({"reason": "QA_COMPLETED_WITH_ERROR"} if has_error else {}),
            }
        if last_status in {"failed", "cancelled", "error"}:
            return {
                "passed": False,
                "http_status": pst,
                "status": last_status,
                "reason": "QA_TERMINAL_FAILURE",
                "qa_id": qa_id,
            }
    return {
        "passed": False,
        "http_status": last_http,
        "status": last_status,
        "reason": "POLL_TIMEOUT",
        "qa_id": qa_id,
    }


def run_post_publish_https_smoke(
    gateway_url: str,
    tenant_id: str,
    *,
    consumer_session: str | None = None,
    operator_token: str | None = None,
    consumer_question: str = "充电时枪拔不出来怎么处理？",
    operator_question: str = "请列出当前租户可用的诊断能力说明。",
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 2.0,
    http_caller: HttpCaller | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """发布后自动执行公网正式 HTTPS 4 项冒烟校验；仅允许 HTTPS，严禁回显任何凭据。"""
    parsed = urlsplit(gateway_url.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("formal post-publish smoke requires clean https gateway_url")
    base = gateway_url.strip().rstrip("/")
    tid = tenant_id.strip()
    if not tid:
        raise ValueError("tenant_id is required")

    caller = http_caller or _default_http_caller
    checks: list[dict[str, Any]] = []

    # 1. 匿名 POST /v1/assistant/questions 必须返回 401
    anon_st, _ = caller(
        "POST",
        f"{base}/v1/assistant/questions",
        {"Content-Type": "application/json"},
        {"question": "smoke-anonymous-check"},
    )
    checks.append(
        {
            "id": "public-https-anon-rejected",
            "passed": anon_st == 401,
            "expected_http": 401,
            "actual_http": anon_st,
        }
    )

    # 2. 既有客户路由 GET /upms/tenant/list 必须返回 200
    upms_st, _ = caller("GET", f"{base}/upms/tenant/list", {"Accept": "application/json"}, None)
    checks.append(
        {
            "id": "public-https-customer-route-intact",
            "passed": upms_st == 200,
            "expected_http": 200,
            "actual_http": upms_st,
        }
    )

    # 3. 可选 C 端正式 HTTPS 问答（内存瞬时 third-session，绝不落盘或回显）
    if consumer_session and consumer_session.strip():
        c_res = _poll_formal_qa(
            caller,
            base,
            {"third-session": consumer_session.strip(), "tenant-id": tid},
            consumer_question,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            sleep_fn=sleep_fn,
        )
        checks.append({"id": "public-https-consumer-qa", **c_res})

    # 4. 可选管家端正式 HTTPS 问答（内存瞬时 Bearer，绝不落盘或回显）
    if operator_token and operator_token.strip():
        raw_tok = operator_token.strip()
        if raw_tok.lower().startswith("bearer "):
            raw_tok = raw_tok[7:].strip()
        o_res = _poll_formal_qa(
            caller,
            base,
            {
                "Authorization": f"Bearer {raw_tok}",
                "X-Business-Entry": "operator",
                "tenant-id": tid,
            },
            operator_question,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            sleep_fn=sleep_fn,
        )
        checks.append({"id": "public-https-operator-qa", **o_res})

    all_passed = all(bool(item.get("passed")) for item in checks)
    return {
        "schema": "aiops-post-publish-https-smoke/1",
        "gateway_url": base,
        "tenant_id": tid,
        "passed": all_passed,
        "checks": checks,
        "business_accuracy": "unconfirmed",
    }
