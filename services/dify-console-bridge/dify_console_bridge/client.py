"""Dify 控制台发布桥的出站 HTTP、会话解析与上游身份/知识查询客户端。"""

from __future__ import annotations

import re
import time as _time_module
from collections.abc import Callable, Mapping
from typing import Any

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    json_body,
    request_json,
)
from aiops_diagnostics.common.http_auth import (
    CONSOLE_ACCESS_TOKEN_COOKIE,
    ConsoleSession,
    build_internal_token_headers,
)
from aiops_diagnostics.common.http_auth import (
    is_console_token as _is_token,
)

from .config import (
    _GATEWAY_REFUSALS,
    _LOGGER,
    _MAX_REASON_CHARS,
    _PUBLISH_CHECK_PATH,
    MAX_UPSTREAM_RESPONSE_BYTES,
    ConsolePublishError,
    _KnowledgeRefused,
    _str_field,
    _WorkspaceLookupFailed,
)

RequestFn = Callable[..., tuple[int, Any]]


def _console_access_token_from_cookie(cookie_header: str | None) -> ConsoleSession | None:
    """只取 ``access_token``，**不要求 CSRF**（#984）。"""
    if not cookie_header:
        return None
    found: dict[str, list[str]] = {prefix + CONSOLE_ACCESS_TOKEN_COOKIE: [] for prefix in ("", "__Host-")}
    for chunk in cookie_header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if name in found:
            found[name].append(value if sep else "")
    plain = bool(found[CONSOLE_ACCESS_TOKEN_COOKIE])
    prefixed = bool(found["__Host-" + CONSOLE_ACCESS_TOKEN_COOKIE])
    if plain == prefixed:
        return None
    prefix = "__Host-" if prefixed else ""
    access = found[prefix + CONSOLE_ACCESS_TOKEN_COOKIE]
    if len(access) != 1 or not _is_token(access[0]):
        return None
    return ConsoleSession(access_token=access[0], csrf_token="", cookie_prefix=prefix)


def _any_json(payload: Any, mapping: ErrorMapping) -> Any:
    """不套信封：解码后的 JSON 原样交给调用方，**形状由调用方逐字段判**。"""
    del mapping
    return payload


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any]:
    """一跳有界 HTTP，走仓库**唯一**的出站骨架（``bounded_http``）。"""
    failures: list[HttpFailure] = []

    def _capture(failure: HttpFailure) -> Exception:
        failures.append(failure)
        return ConsolePublishError(f"{method} {url} 失败：HTTP {failure.status}")

    def _transport(failure: HttpFailure) -> Exception:
        failures.append(failure)
        return ConsolePublishError(f"{method} {url} 失败：{failure.kind}")

    mapping = ErrorMapping(
        auth_rejected=_capture,
        http_error=_capture,
        unavailable=_transport,
        invalid_body=_transport,
        invalid_envelope=_transport,
    )
    spec = RequestSpec(
        url=url,
        method=method,
        headers=dict(headers or {}),
        body=json_body(dict(body)) if body is not None else None,
        timeout=timeout,
        max_read_bytes=MAX_UPSTREAM_RESPONSE_BYTES,
        follow_redirects=False,
    )
    try:
        payload = request_json(spec, mapping=mapping, envelope=_any_json)
    except ConsolePublishError:
        if not failures or failures[0].status is None:
            raise
        failure = failures[0]
        return failure.status, (
            {"error": {"code": failure.code, "message": failure.detail}}
            if failure.code or failure.detail
            else None
        )
    return 200, payload


def _gateway_reason(error: Any, fallback: str) -> str:
    """把网关的 ``error`` 翻成一句运营能照着做的话，翻不出来就用 ``fallback``。"""
    if not isinstance(error, Mapping):
        return fallback
    code = (_str_field(error, "code") or "").strip()
    if code not in _GATEWAY_REFUSALS:
        return fallback
    written = _GATEWAY_REFUSALS[code]
    if written:
        return written
    detail = (_str_field(error, "message") or "").strip()
    if detail and not re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", detail):
        return detail[:_MAX_REASON_CHARS]
    return fallback


def _workspace_for(
    gateway_base_url: str, internal_token: str, *, tenant_id: str, timeout: float
) -> str | None:
    """问网关"这个租户的 workspace 是哪一个"（#884 / ADR-0067 的只读端点）。"""

    def refused(failure: HttpFailure) -> Exception:
        return _WorkspaceLookupFailed(f"{failure.status or failure.detail}")

    try:
        payload = request_json(
            RequestSpec(
                url=join_url(gateway_base_url, "/v1/dify/workspace", query={"tenant_id": tenant_id}),
                headers=build_internal_token_headers(internal_token, 300, int(_time_module.time())),
                timeout=timeout,
            ),
            mapping=ErrorMapping(
                auth_rejected=refused,
                http_error=refused,
                unavailable=refused,
                invalid_body=refused,
                invalid_envelope=refused,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 查不通一律按"不能确认"处置
        _LOGGER.info("workspace lookup failed: %s: %s", type(exc).__name__, exc)
        return None
    workspace = str(payload.get("workspace_id") or "") if isinstance(payload, dict) else ""
    if not workspace:
        _LOGGER.info("workspace lookup returned no workspace_id: %r", payload)
    return workspace or None


def _identity_and_publish_verdict(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession,
    timeout: float,
    request_fn: RequestFn = _request,
) -> tuple[dict[str, str], bool] | None:
    """问代理两件事：这个会话**是谁**，以及他**能不能发布**（#780 / #782）。"""
    try:
        http_status, verdict = request_fn(
            login_proxy_url.rstrip("/") + _PUBLISH_CHECK_PATH,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body={"session": console_session.access_token},
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - 查不通一律按"不能确认"处置
        return None
    if not 200 <= http_status < 300 or not isinstance(verdict, dict):
        return None
    company_user_id = (_str_field(verdict, "company_user_id") or "").strip()
    company_tenant_id = (_str_field(verdict, "company_tenant_id") or "").strip()
    if not company_user_id or not company_tenant_id:
        _LOGGER.info("console identity: proxy did not resolve a company identity")
        return None
    headers = {"X-AIOps-Initiator": company_user_id}
    company_username = (_str_field(verdict, "company_username") or "").strip()
    if company_username:
        headers["X-AIOps-Initiator-Name"] = company_username
    dify_account_id = (_str_field(verdict, "dify_account_id") or "").strip()
    if dify_account_id:
        headers["X-AIOps-Dify-Account"] = dify_account_id
    headers["X-AIOps-Initiator-Tenant"] = company_tenant_id
    company_user_type = (_str_field(verdict, "company_user_type") or "").strip()
    if company_user_type:
        headers["X-AIOps-Initiator-Type"] = company_user_type
    return headers, verdict.get("allowed") is True


def _identity_and_publish_verdict_for_identity(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession,
    timeout: float,
    request_fn: RequestFn = _request,
) -> tuple[dict[str, str], bool] | None:
    """只要**身份**，不看发布权（#984）。"""
    return _identity_and_publish_verdict(
        login_proxy_url, console_session=console_session, timeout=timeout, request_fn=request_fn
    )


def _initiator_headers_for(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession | None,
    timeout: float,
    request_fn: RequestFn = _request,
) -> dict[str, str]:
    """发起人身份的头，**提交与读结果共用这一处**。"""
    if not login_proxy_url:
        return {}
    if console_session is None:  # pragma: no cover - 调用方保证成对给
        return {}
    verdict = _identity_and_publish_verdict(
        login_proxy_url, console_session=console_session, timeout=timeout, request_fn=request_fn
    )
    if verdict is None:
        _LOGGER.info("console trial: identity unavailable, falling back to the debug tenant")
        return {}
    identity_headers, _allowed = verdict
    return identity_headers


def _knowledge_sync_payload(
    gateway_base_url: str, internal_token: str, *, workspace_id: str, timeout: float
) -> int:
    """让网关收敛一次，返回它建/改/退休了几条 dataset（ADR-0122 / ADR-0125）。"""

    def failed(failure: HttpFailure) -> Exception:
        return ConsolePublishError(f"knowledge sync: {failure.status or failure.detail}")

    payload = request_json(
        RequestSpec(
            url=join_url(
                gateway_base_url,
                "/v1/internal/knowledge/sync",
                query={"workspace": workspace_id},
            ),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **build_internal_token_headers(internal_token, 300, int(_time_module.time())),
            },
            body=b"{}",
            timeout=timeout,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=failed,
            http_error=failed,
            unavailable=failed,
            invalid_body=failed,
            invalid_envelope=failed,
        ),
    )
    if not isinstance(payload, dict):
        raise ConsolePublishError("knowledge sync: gateway returned a non-object")
    for key in ("changed", "created"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _knowledge_open_payload(
    gateway_base_url: str, internal_token: str, *, workspace_id: str, timeout: float
) -> dict[str, Any]:
    """问网关"这个租户的 RAGFlow 入口在哪"，拿到后就跳过去（#982 的端点）。"""
    _REFUSALS = {
        409: "这个租户还没有开通 RAGFlow 知识库，请联系管理员开户后再试",
        410: "入口链接已过期，请回到 Dify 重新点击",
    }

    def failed(failure: HttpFailure) -> Exception:
        status = failure.status or 0
        message = _REFUSALS.get(status)
        if message:
            return _KnowledgeRefused(status, message)
        return ConsolePublishError(f"knowledge open: {status or failure.detail}")

    try:
        payload = request_json(
            RequestSpec(
                url=join_url(
                    gateway_base_url,
                    "/v1/internal/knowledge/open",
                    query={"workspace": workspace_id},
                ),
                method="GET",
                headers={
                    "Accept": "application/json",
                    **build_internal_token_headers(internal_token, 300, int(_time_module.time())),
                },
                timeout=timeout,
                follow_redirects=False,
            ),
            mapping=ErrorMapping(
                auth_rejected=failed,
                http_error=failed,
                unavailable=failed,
                invalid_body=failed,
                invalid_envelope=failed,
            ),
        )
    except _KnowledgeRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - 一步失败，交给调用点说人话
        raise ConsolePublishError(f"knowledge open: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("target"):
        raise ConsolePublishError("knowledge open: gateway returned no target")
    return payload
