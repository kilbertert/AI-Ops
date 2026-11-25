"""Dify 控制台发布、试运行与原生管家预览工作流编排。"""

from __future__ import annotations

import contextlib
import hmac
import re
import time as _time_module
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from aiops_diagnostics.common import order_reference
from aiops_diagnostics.common.http_auth import ConsoleSession
from aiops_diagnostics.common.platform_constants import (
    DIFY_PLATFORM_WORKSPACE_ID,
    PLATFORM_USER_TYPE,
)
from aiops_diagnostics.identity.order_identity import is_order_identifier

from .client import (
    RequestFn,
    _gateway_reason,
    _identity_and_publish_verdict,
    _initiator_headers_for,
    _request,
    _workspace_for,
)
from .config import (
    _LOGGER,
    _PUBLISHED_ACTIONS,
    _REFUSED_ACTIONS,
    _REFUSED_NO_WORKSPACE,
    _REFUSED_PUBLISH_PERMISSION,
    _REFUSED_ROLE,
    _REFUSED_SESSION,
    _REFUSED_WORKSPACE,
    _UNCONFIRMED,
    DEFAULT_ALLOWED_ROLES,
    DIFY_APP_PATH,
    DIFY_WORKSPACE_SUMMARY_PATH,
    MAX_TRIAL_RESULT_TEXT,
    NATIVE_POLL_SECONDS,
    NATIVE_TASK_PREFIX,
    NATIVE_TIMEOUT_SECONDS,
    PUBLISH_PATH,
    TRIAL_PATH,
    TRIAL_TERMINAL_STATUSES,
    ConsolePublishError,
    ConsolePublishResult,
    TrialReport,
    _Rejected,
    _str_field,
    app_id_from_referer,
)


def _interpret_publish_outcome(outcome: Any, app_id: str) -> ConsolePublishResult:
    """网关 2xx 的回答**逐字段**核对后才算成功（形状取自 ``_publish_bound_app``）。"""
    action = _str_field(outcome, "action")
    if _str_field(outcome, "app_id") != app_id:
        _LOGGER.warning("console publish: gateway answer does not name the requested app")
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    if action in _REFUSED_ACTIONS:
        _LOGGER.info("console publish refused by publish gate: action=%s", action)
        return ConsolePublishResult(False, "发布门拒绝了这次改动，请联系工程查看原因", status=409)
    version = outcome.get("version_no")
    if action not in _PUBLISHED_ACTIONS or type(version) is not int or version < 1:
        _LOGGER.warning("console publish: gateway answer has an unexpected shape")
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    if action == "unchanged":
        return ConsolePublishResult(True, f"线上已是最新（版本 {version}），无需改动", action, status=200)
    return ConsolePublishResult(True, f"已发布到线上（版本 {version}）", action, status=200)


def publish_current_app(
    *,
    dify_base_url: str,
    gateway_base_url: str,
    publish_key: str,
    console_session: ConsoleSession,
    console_origin: str,
    workspace_id: str,
    referer: str | None,
    allowed_roles: frozenset[str] = DEFAULT_ALLOWED_ROLES,
    login_proxy_url: str = "",
    gateway_internal_token: str = "",
    timeout: float = 15.0,
    operation: str = "publish",
    version_no: int | None = None,
    request_fn: RequestFn = _request,
) -> ConsolePublishResult:
    """运营点了按钮之后，这一层做的全部事情。顺序不能换。"""
    app_id = app_id_from_referer(referer, console_origin=console_origin)
    if app_id is None:
        return ConsolePublishResult(False, "无法确定当前是哪个智能体（请从编排页点这个按钮）")

    dify_base = dify_base_url.rstrip("/")
    dify_headers = console_session.dify_headers()

    http_status, summary = request_fn(
        dify_base + DIFY_WORKSPACE_SUMMARY_PATH, headers=dify_headers, timeout=timeout
    )
    if not 200 <= http_status < 300:
        return ConsolePublishResult(False, _REFUSED_SESSION)
    role = _str_field(summary, "role")
    current_workspace = _str_field(summary, "id")
    if role is None or current_workspace is None:
        _LOGGER.info("console publish refused: workspace summary has no role/id")
        return ConsolePublishResult(False, _REFUSED_SESSION)
    initiator_headers: dict[str, str] = {}
    pinned_workspace = workspace_id
    if login_proxy_url:
        verdict = _identity_and_publish_verdict(
            login_proxy_url,
            console_session=console_session,
            timeout=timeout,
            request_fn=request_fn,
        )
        if verdict is None:
            _LOGGER.info("console publish refused: publish-permission check unavailable")
            return ConsolePublishResult(False, "暂时无法核对发布权限，请稍后重试")
        initiator_headers, allowed = verdict
        if not allowed:
            _LOGGER.info("console publish refused: no company publish permission")
            return ConsolePublishResult(False, _REFUSED_PUBLISH_PERMISSION)
        if gateway_internal_token:
            tenant_id = initiator_headers.get("X-AIOps-Initiator-Tenant", "")
            if initiator_headers.get("X-AIOps-Initiator-Type", "").strip() == PLATFORM_USER_TYPE:
                tenant_id = DIFY_PLATFORM_WORKSPACE_ID
            resolved = _workspace_for(
                gateway_base_url,
                gateway_internal_token,
                tenant_id=tenant_id,
                timeout=timeout,
            )
            if resolved is None:
                _LOGGER.info("console publish refused: the initiator's tenant has no workspace")
                return ConsolePublishResult(False, _REFUSED_NO_WORKSPACE)
            pinned_workspace = resolved
    elif role not in allowed_roles:
        _LOGGER.info("console publish refused: role %r not allowed", role[:32])
        return ConsolePublishResult(False, _REFUSED_ROLE)

    if not hmac.compare_digest(current_workspace.encode("utf-8"), pinned_workspace.encode("utf-8")):
        _LOGGER.info("console publish refused: current workspace is not the pinned one")
        return ConsolePublishResult(False, _REFUSED_WORKSPACE)

    app_url = dify_base + DIFY_APP_PATH.format(app_id=urllib.parse.quote(app_id, safe=""))
    http_status, app = request_fn(app_url, headers=dify_headers, timeout=timeout)
    if not 200 <= http_status < 300 or _str_field(app, "id") != app_id:
        return ConsolePublishResult(False, _REFUSED_SESSION)

    http_status, outcome = request_fn(
        gateway_base_url.rstrip("/")
        + {
            "publish": PUBLISH_PATH,
            "retract": "/v1/dify/retract",
            "restore": "/v1/dify/restore",
            "versions": "/v1/dify/versions",
        }[operation],
        method="POST",
        headers={
            "Authorization": f"Bearer {publish_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **initiator_headers,
        },
        body={"app_id": app_id, **({"version_no": version_no} if operation == "restore" else {})},
        timeout=timeout,
    )
    if not 200 <= http_status < 300:
        error = outcome.get("error") if isinstance(outcome, Mapping) else None
        code = (_str_field(error, "code") or "").strip()
        _LOGGER.info("console publish refused by gateway: HTTP %s code=%s", http_status, code or "-")
        return ConsolePublishResult(
            False,
            _gateway_reason(error, "发布未通过我们的发布门，请联系工程查看原因"),
        )
    if operation == "retract":
        ok = (
            isinstance(outcome, dict)
            and outcome.get("app_id") == app_id
            and outcome.get("state") == "retracted"
        )
        return ConsolePublishResult(
            ok,
            "已撤回正式版本" if ok else _UNCONFIRMED,
            action="retracted" if ok else "",
            status=200 if ok else 502,
        )
    if operation == "versions":
        if not isinstance(outcome, dict) or outcome.get("app_id") != app_id:
            return ConsolePublishResult(False, _UNCONFIRMED, status=502)
        versions = outcome.get("versions")
        if not isinstance(versions, list):
            return ConsolePublishResult(False, _UNCONFIRMED, status=502)
        return ConsolePublishResult(True, "可恢复的历史版本", status=200, versions=tuple(versions))
    return _interpret_publish_outcome(outcome, app_id)


def submit_trial(
    *,
    gateway_base_url: str,
    trial_key: str,
    app_id: str,
    order_no: str,
    question: str = "",
    login_proxy_url: str = "",
    console_session: ConsoleSession | None = None,
    timeout: float = 15.0,
    request_fn: RequestFn = _request,
) -> ConsolePublishResult:
    """把一次试运行**提交**给网关（#781 / #782 的部署层）。"""
    initiator_headers = _initiator_headers_for(
        login_proxy_url,
        console_session=console_session,
        timeout=timeout,
        request_fn=request_fn,
    )
    if console_session is not None:
        initiator_headers["X-AIOps-Console-Session"] = console_session.access_token
    http_status, answer = request_fn(
        gateway_base_url.rstrip("/") + TRIAL_PATH,
        method="POST",
        headers={
            "Authorization": f"Bearer {trial_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            **initiator_headers,
        },
        body={"app_id": app_id, "order_no": order_no, **({"question": question} if question else {})},
        timeout=timeout,
    )
    error = answer.get("error") if isinstance(answer, dict) else None
    error_code = (_str_field(error, "code") or "").strip()
    if http_status == 404:
        if error_code == "DIFY_APP_NOT_BOUND":
            return ConsolePublishResult(
                False, "这个智能体没有登记成 AI-Ops 的绑定来源，无法试运行", status=404
            )
        if error_code == "ORDER_NOT_FOUND":
            return ConsolePublishResult(
                False,
                "这张订单不在你的可见范围内（或不存在）。试运行只对你自己能看到的订单生效",
                status=404,
            )
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    if http_status in {401, 403}:
        return ConsolePublishResult(False, _gateway_reason(error, _UNCONFIRMED), status=502)
    if http_status == 429:
        return ConsolePublishResult(False, "已经有一次试运行在跑，请等它结束再试", status=429)
    if not 200 <= http_status < 300 or not isinstance(answer, dict):
        _LOGGER.warning("console trial: gateway answered status=%s", http_status)
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    trial_id = _str_field(answer, "trial_id")
    trial_status = _str_field(answer, "status")
    if not trial_id or not trial_status:
        _LOGGER.warning("console trial: gateway answer has an unexpected shape")
        return ConsolePublishResult(False, _UNCONFIRMED, status=502)
    return ConsolePublishResult(
        True,
        f"已提交试运行（{trial_id}），状态：{trial_status}",
        trial_id=trial_id,
        status=202,
    )


def _trial_report_text(answer: Mapping[str, Any], status_text: str) -> str:
    """把网关的结果压成一段**运营能读**的文字。"""
    if status_text not in TRIAL_TERMINAL_STATUSES:
        return f"试运行进行中（{status_text}）"
    if status_text != "completed":
        reason = str(answer.get("error_message") or "").strip()
        code = str(answer.get("error_code") or "").strip()
        text = f"试运行结束，状态：{status_text}"
        if reason:
            text += f"：{reason}"
        if code:
            text += f"（{code}）"
        return text[:MAX_TRIAL_RESULT_TEXT]
    result = answer.get("result")
    report = result.get("report") if isinstance(result, Mapping) else None
    if not isinstance(report, Mapping):
        return "试运行完成，但结果里没有可读的报告"
    summary = str(report.get("summary") or "").strip()
    if report.get("callback_version") == 6 and summary:
        return summary
    root_cause = str(report.get("root_cause") or "").strip()
    parts = [part for part in (summary, root_cause) if part]
    text = " ／ ".join(parts) or "试运行完成，报告为空"
    evidence = report.get("evidence_ids")
    if isinstance(evidence, list) and evidence:
        text += f"（证据 {len(evidence)} 条：{', '.join(str(item) for item in evidence[:5])}）"
    failed = report.get("failed_sources")
    if isinstance(failed, list) and failed:
        text += f"（未取到的数据源：{', '.join(str(item) for item in failed[:5])}）"
    return text[:MAX_TRIAL_RESULT_TEXT]


def read_trial(
    *,
    gateway_base_url: str,
    trial_key: str,
    trial_id: str,
    login_proxy_url: str = "",
    console_session: ConsoleSession | None = None,
    timeout: float = 15.0,
    request_fn: RequestFn = _request,
) -> TrialReport:
    """把一次试运行的状态与结果读回来（结果面板那一条）。"""
    if login_proxy_url and console_session is None:
        return TrialReport(False, "会话缺失，无法核对身份", status=403)
    initiator_headers = _initiator_headers_for(
        login_proxy_url,
        console_session=console_session,
        timeout=timeout,
        request_fn=request_fn,
    )
    if console_session is not None:
        initiator_headers["X-AIOps-Console-Session"] = console_session.access_token
    http_status, answer = request_fn(
        gateway_base_url.rstrip("/") + f"{TRIAL_PATH}/{urllib.parse.quote(trial_id, safe='')}",
        method="GET",
        headers={
            "Authorization": f"Bearer {trial_key}",
            "Accept": "application/json",
            **initiator_headers,
        },
        timeout=timeout,
    )
    if http_status == 404:
        return TrialReport(False, "读不到这次试运行（可能已过期，或不属于你的可见范围）", status=404)
    if http_status in {401, 403}:
        error = answer.get("error") if isinstance(answer, Mapping) else None
        return TrialReport(False, _gateway_reason(error, _UNCONFIRMED), status=502)
    if not 200 <= http_status < 300 or not isinstance(answer, dict):
        _LOGGER.warning("console trial result: gateway answered status=%s", http_status)
        return TrialReport(False, _UNCONFIRMED, status=502)
    status_text = _str_field(answer, "status")
    if not status_text:
        _LOGGER.warning("console trial result: gateway answer has no status")
        return TrialReport(False, _UNCONFIRMED, status=502)
    return TrialReport(
        True,
        _trial_report_text(answer, status_text),
        status=200,
        job_status=status_text,
        terminal=status_text in TRIAL_TERMINAL_STATUSES,
    )


def _native_session(handler: Any, *, request_fn: RequestFn = _request):
    if not handler.settings.trial_key or not handler.settings.login_proxy_url:
        raise _Rejected(404, "管家预览尚未启用")
    session, referer = handler._check_browser_request()
    app_id = app_id_from_referer(referer, console_origin=handler.settings.console_origin)
    code, app = request_fn(
        handler.settings.dify_base_url + "/console/api/apps/" + app_id,
        headers=session.dify_headers(),
        timeout=handler.settings.timeout,
    )
    if code != 200 or not isinstance(app, dict) or app.get("id") != app_id:
        raise _Rejected(403, "当前会话无权预览此应用")
    return session, app_id


def _native_gateway(
    handler: Any, session: ConsoleSession, path: str, *, body=None, request_fn: RequestFn = _request
):
    return request_fn(
        handler.settings.gateway_base_url + "/v1/dify/trial" + path,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": "Bearer " + handler.settings.trial_key,
            "X-AIOps-Console-Session": session.access_token,
            "Content-Type": "application/json",
        },
        body=body,
        timeout=handler.settings.timeout,
    )


def _native_refusal(answer: Any) -> dict[str, Any]:
    error = answer.get("error", {}) if isinstance(answer, dict) else {}
    code = str(error.get("code") or "OPERATOR_PREVIEW_UNAVAILABLE")
    if code.startswith("OPERATOR_TRIAL_LOGIN") or code == "OPERATOR_TRIAL_IDENTITY_MISMATCH":
        message = "公司登录已失效或无法核验，请重新登录后重试"
    elif code in {"ORDER_NOT_FOUND", "CPMS_SCOPE_MISMATCH", "OPERATOR_TRIAL_SCOPE_MISMATCH"}:
        message = "订单不存在或不在当前账号可见范围内"
    elif code == "OPERATOR_SITE_SCOPE_REQUIRED":
        message = "当前账号未分配可诊断场站，请联系公司管理员分配或切换账号"
    elif code == "CHAT_ROUTE_RETRACTED":
        message = "管家节点路线已撤回，请联系管理员恢复"
    else:
        message = "管家预览未完成，请联系管理员核对节点与授权"
    return {"event": "error", "code": code, "message": message}


def _native_order(query: str) -> str:
    if not order_reference.names_order(query):
        return ""
    candidates = order_reference.order_candidates(query)
    unsafe = (item for item in candidates if any(c.isdigit() for c in item))
    if any(not is_order_identifier(item) for item in unsafe):
        raise _Rejected(400, "订单号包含不支持的字符，请只提供一个完整有效的订单号")
    if not candidates:
        return ""
    if len(candidates) != 1:
        raise _Rejected(400, "请在本条消息中填写一个明确订单号；暂不支持“上一个订单”")
    return candidates[0]


def _native_preview(
    handler: Any,
    *,
    request_fn: RequestFn = _request,
    native_gateway_fn: Callable[..., tuple[int, Any]] | None = None,
    poll_seconds: float = NATIVE_POLL_SECONDS,
    timeout_seconds: float = NATIVE_TIMEOUT_SECONDS,
) -> None:
    gateway_fn = native_gateway_fn or (
        lambda h, s, p, **kw: _native_gateway(h, s, p, request_fn=request_fn, **kw)
    )
    session, app_id = _native_session(handler, request_fn=request_fn)
    payload = handler._read_bounded_json(
        65536,
        frozenset(
            {"app_id", "query", "pre_prompt", "tools", "response_mode", "conversation_id", "files", "inputs"}
        ),
    )
    if (
        payload.pop("response_mode", "streaming") != "streaming"
        or payload.pop("files", []) != []
        or payload.pop("inputs", {}) != {}
    ):
        raise _Rejected(400, "管家预览暂不接受文件和变量，请移除后重试")
    conversation_id = payload.pop("conversation_id", "") or ""
    if not isinstance(conversation_id, str) or (
        conversation_id and not re.fullmatch(r"conv_[a-f0-9]{32}", conversation_id)
    ):
        raise _Rejected(400, "预览会话无效，请新建会话")
    if payload.get("app_id") != app_id:
        raise _Rejected(403, "应用与当前页面不一致")
    query = payload.get("query")
    prompt, tools = payload.get("pre_prompt"), payload.get("tools")
    if (
        not isinstance(query, str)
        or not 0 < len(query) <= 4000
        or not isinstance(prompt, str)
        or not 0 < len(prompt) <= 8000
        or not isinstance(tools, list)
        or len(tools) > 32
    ):
        raise _Rejected(400, "预览问题、提示词或工具配置无效")
    order = ""
    http_status, answer = gateway_fn(
        handler,
        session,
        "",
        body={
            "app_id": app_id,
            "order_no": order,
            "question": query,
            "operator_preview": {"pre_prompt": prompt, "tools": tools},
            **({"conversation_id": conversation_id} if conversation_id else {}),
        },
    )
    trial_id = answer.get("trial_id") if isinstance(answer, dict) else None
    if http_status != 200 or not isinstance(trial_id, str) or not re.fullmatch(r"qa_[a-zA-Z0-9]+", trial_id):
        error = _native_refusal(answer)
        handler._json(http_status if 400 <= http_status < 500 else 502, error)
        return
    returned_conversation = answer.get("conversation_id")
    if not isinstance(returned_conversation, str) or not re.fullmatch(
        r"conv_[a-f0-9]{32}", returned_conversation
    ):
        with contextlib.suppress(ConsolePublishError):
            gateway_fn(handler, session, "/" + trial_id + "/cancel", body={"app_id": app_id})
        handler._json(
            502, {"code": "OPERATOR_PREVIEW_VERSION_MISMATCH", "message": "预览服务版本不匹配，请联系管理员"}
        )
        return
    message_id = str(uuid.uuid4())
    common = {
        "id": message_id,
        "message_id": message_id,
        "conversation_id": returned_conversation,
        "task_id": NATIVE_TASK_PREFIX + trial_id,
        "created_at": int(_time_module.time()),
    }
    finished = False
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler._sse({**common, "event": "message", "answer": ""})
        handler._sse(
            {
                **common,
                "event": "agent_thought",
                "position": 1,
                "thought": "",
                "tool": "",
                "tool_input": "",
                "observation": "",
                "message_files": [],
            }
        )
        deadline = _time_module.monotonic() + timeout_seconds
        last_progress = None
        while _time_module.monotonic() < deadline:
            http_status, result = gateway_fn(handler, session, "/" + trial_id)
            if http_status != 200 or not isinstance(result, dict):
                handler._sse(_native_refusal(result))
                return
            if result.get("status") in TRIAL_TERMINAL_STATUSES:
                if result["status"] == "completed":
                    if last_progress is not None:
                        handler._sse(
                            {
                                **common,
                                "id": message_id + "-answer",
                                "event": "agent_thought",
                                "position": 2,
                                "thought": "",
                                "tool": "",
                                "tool_input": "",
                                "observation": "",
                                "message_files": [],
                            }
                        )
                    handler._sse(
                        {
                            **common,
                            "event": "message",
                            "answer": "Java 节点试用（模型由节点平台配置）\n\n"
                            + _trial_report_text(result, "completed"),
                        }
                    )
                    handler._sse({**common, "event": "message_end", "metadata": {}})
                else:
                    handler._sse(
                        {
                            "event": "error",
                            "code": result.get("error_code") or "OPERATOR_PREVIEW_FAILED",
                            "message": _trial_report_text(result, result["status"]),
                        }
                    )
                finished = True
                return
            progress = result.get("progress") or {}
            phase = progress.get("phase")
            label = {
                "queued": "等待执行",
                "dispatch": "正在生成并按需取证",
                "tool": "正在查询业务记录",
                "generating": "正在整理回答",
                "review": "正在核对回答与证据",
                "resample": "正在重新生成回答",
            }.get(phase)
            elapsed = progress.get("elapsed_ms")
            if label and isinstance(elapsed, int) and elapsed >= 0:
                current = (phase, elapsed // 5000)
                if current != last_progress:
                    handler._sse(
                        {
                            **common,
                            "event": "agent_thought",
                            "position": 1,
                            "thought": f"执行进度：{label}（已用时 {elapsed // 1000} 秒）",
                            "tool": "",
                            "tool_input": "",
                            "observation": "",
                            "message_files": [],
                        }
                    )
                    last_progress = current
            handler.wfile.write(b": waiting\n\n")
            handler.wfile.flush()
            _time_module.sleep(poll_seconds)
        handler._sse(
            {"event": "error", "code": "OPERATOR_PREVIEW_TIMEOUT", "message": "预览超时，任务已请求取消"}
        )
    except ConsolePublishError:
        with contextlib.suppress(OSError):
            handler._sse(
                {
                    "event": "error",
                    "code": "OPERATOR_PREVIEW_UNAVAILABLE",
                    "message": "预览结果读取失败，任务已请求取消",
                }
            )
    except OSError:
        pass
    finally:
        handler.close_connection = True
        if not finished:
            try:
                cancelled, _ = gateway_fn(
                    handler, session, "/" + trial_id + "/cancel", body={"app_id": app_id}
                )
                if cancelled != 200:
                    handler.log_message("operator preview cancellation refused: %s", cancelled)
            except ConsolePublishError:
                handler.log_message("operator preview cancellation could not be confirmed")


def _native_cancel(
    handler: Any,
    *,
    request_fn: RequestFn = _request,
    native_gateway_fn: Callable[..., tuple[int, Any]] | None = None,
) -> None:
    gateway_fn = native_gateway_fn or (
        lambda h, s, p, **kw: _native_gateway(h, s, p, request_fn=request_fn, **kw)
    )
    session, app_id = _native_session(handler, request_fn=request_fn)
    payload = handler._read_bounded_json(4096, frozenset({"task_id"}))
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not re.fullmatch(r"aiops_qa_[a-zA-Z0-9]+", task_id):
        raise _Rejected(400, "预览任务无效")
    http_status, answer = gateway_fn(
        handler, session, "/" + task_id.removeprefix(NATIVE_TASK_PREFIX) + "/cancel", body={"app_id": app_id}
    )
    handler._json(http_status, {"result": "success"} if http_status == 200 else _native_refusal(answer))
