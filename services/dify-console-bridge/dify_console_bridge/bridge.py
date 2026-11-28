"""Dify 控制台里那颗「发布到 AI-Ops」按钮的**后端桥**（ADR-0021）。

本模块保留 HTTP 入口（:class:`ConsolePublishBridgeHandler`、:func:`make_server`、
:func:`main`）以及既有公共符号导出；配置/常量解析位于 :mod:`.config`，
出站 HTTP 与身份/知识客户端位于 :mod:`.client`，发布/试运行/原生管家预览编排位于
:mod:`.workflows`。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import urllib.parse
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    request_json,
)
from aiops_diagnostics.common.endpoint_policy import require_secure_endpoint
from aiops_diagnostics.common.http_auth import (
    CONSOLE_ACCESS_TOKEN_COOKIE,
    CONSOLE_CSRF_COOKIE,
    CONSOLE_CSRF_HEADER,
    ConsoleSession,
    console_session_from_headers,
)
from aiops_diagnostics.common.platform_constants import (
    DIFY_PLATFORM_WORKSPACE_ID,
    PLATFORM_USER_TYPE,
)

from . import client as _client
from . import workflows as _workflows
from .client import (
    _console_access_token_from_cookie,
    _gateway_reason,
    _knowledge_open_payload,
    _knowledge_sync_payload,
    _request,
    _workspace_for,
)
from .config import (
    _CONSOLE_ASSET_PREFIX,
    _CONSOLE_ROUTES,
    _CONTENT_LENGTH_RE,
    _KNOWLEDGE_OPEN_ROUTE,
    _KNOWLEDGE_SYNC_ROUTE,
    _LOGGER,
    _PUBLISH_ROUTE,
    _TRIAL_PAYLOAD_KEYS,
    _TRIAL_RESULT_PAYLOAD_KEYS,
    _TRIAL_RESULT_ROUTE,
    _TRIAL_ROUTE,
    DEFAULT_ALLOWED_ROLES,
    DIFY_APP_PATH,
    DIFY_WORKSPACE_SUMMARY_PATH,
    KNOWN_ROLES,
    MAX_REQUEST_BODY_BYTES,
    MAX_TRIAL_BODY_BYTES,
    MAX_TRIAL_RESULT_TEXT,
    MAX_UPSTREAM_RESPONSE_BYTES,
    NATIVE_CANCEL_ROUTE,
    NATIVE_POLL_SECONDS,
    NATIVE_PREVIEW_ROUTE,
    NATIVE_TASK_PREFIX,
    NATIVE_TIMEOUT_SECONDS,
    PUBLISH_PATH,
    TRIAL_PATH,
    TRIAL_TERMINAL_STATUSES,
    BridgeSettings,
    ConsolePublishError,
    ConsolePublishResult,
    TrialReport,
    _escape,
    _KnowledgeRefused,
    _origin_and_path,
    _Rejected,
    _str_field,
    app_id_from_referer,
    normalize_origin,
    parse_allowed_roles,
)
from .workflows import (
    _interpret_publish_outcome,
    _native_order,
    _native_refusal,
    _trial_report_text,
)

__all__ = [
    "CONSOLE_CSRF_HEADER",
    "DEFAULT_ALLOWED_ROLES",
    "DIFY_APP_PATH",
    "DIFY_PLATFORM_WORKSPACE_ID",
    "DIFY_WORKSPACE_SUMMARY_PATH",
    "KNOWN_ROLES",
    "PLATFORM_USER_TYPE",
    "MAX_REQUEST_BODY_BYTES",
    "MAX_TRIAL_BODY_BYTES",
    "MAX_TRIAL_RESULT_TEXT",
    "MAX_UPSTREAM_RESPONSE_BYTES",
    "NATIVE_CANCEL_ROUTE",
    "NATIVE_POLL_SECONDS",
    "NATIVE_PREVIEW_ROUTE",
    "NATIVE_TASK_PREFIX",
    "NATIVE_TIMEOUT_SECONDS",
    "PUBLISH_PATH",
    "TRIAL_PATH",
    "TRIAL_TERMINAL_STATUSES",
    "BridgeSettings",
    "ConsolePublishBridgeHandler",
    "ConsolePublishError",
    "ConsolePublishResult",
    "ConsoleSession",
    "TrialReport",
    "_Rejected",
    "_gateway_reason",
    "_interpret_publish_outcome",
    "_knowledge_open_payload",
    "_knowledge_sync_payload",
    "_native_cancel",
    "_native_gateway",
    "_native_order",
    "_native_preview",
    "_native_refusal",
    "_origin_and_path",
    "_request",
    "_str_field",
    "_trial_report_text",
    "_workspace_for",
    "app_id_from_referer",
    "console_session_from_headers",
    "main",
    "make_server",
    "normalize_origin",
    "parse_allowed_roles",
    "publish_current_app",
    "read_trial",
    "submit_trial",
]


def _identity_and_publish_verdict(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession,
    timeout: float,
) -> tuple[dict[str, str], bool] | None:
    return _client._identity_and_publish_verdict(
        login_proxy_url,
        console_session=console_session,
        timeout=timeout,
        request_fn=_request,
    )


def _identity_and_publish_verdict_for_identity(
    login_proxy_url: str,
    *,
    console_session: ConsoleSession,
    timeout: float,
) -> tuple[dict[str, str], bool] | None:
    return _client._identity_and_publish_verdict_for_identity(
        login_proxy_url,
        console_session=console_session,
        timeout=timeout,
        request_fn=_request,
    )


def publish_current_app(**kwargs: Any) -> ConsolePublishResult:
    kwargs.setdefault("request_fn", _request)
    return _workflows.publish_current_app(**kwargs)


def submit_trial(**kwargs: Any) -> ConsolePublishResult:
    kwargs.setdefault("request_fn", _request)
    return _workflows.submit_trial(**kwargs)


def read_trial(**kwargs: Any) -> TrialReport:
    kwargs.setdefault("request_fn", _request)
    return _workflows.read_trial(**kwargs)


def _native_gateway(handler: Any, session: ConsoleSession, path: str, *, body=None):
    return _workflows._native_gateway(handler, session, path, body=body, request_fn=_request)


def _native_preview(handler: Any) -> None:
    _workflows._native_preview(
        handler,
        request_fn=_request,
        native_gateway_fn=_native_gateway,
        poll_seconds=NATIVE_POLL_SECONDS,
        timeout_seconds=NATIVE_TIMEOUT_SECONDS,
    )


def _native_cancel(handler: Any) -> None:
    _workflows._native_cancel(
        handler,
        request_fn=_request,
        native_gateway_fn=_native_gateway,
    )


class ConsolePublishBridgeHandler(BaseHTTPRequestHandler):
    """控制台同源 HTTP 处理器（stdlib、有界、不做任意代理转发）。"""

    settings: ClassVar[BridgeSettings]
    environment_credentials: ClassVar[dict[str, dict[str, str]]] = {}

    def _request_id(self) -> str:
        if getattr(self, "headers", None) is None:
            return ""
        raw = (self._single_header("X-Request-Id") or "").strip()
        return raw if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", raw) else ""

    def _send_request_id_header(self) -> None:
        rid = self._request_id()
        if rid:
            self.send_header("X-Request-Id", rid)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的协议
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if path in _CONSOLE_ROUTES:
                self._get_console_page(parsed)
                return
            if path.startswith(_CONSOLE_ASSET_PREFIX):
                self._get_console_asset(path.removeprefix(_CONSOLE_ASSET_PREFIX))
                return
            if path != _KNOWLEDGE_OPEN_ROUTE:
                raise _Rejected(404, "not found")
            self._select_environment()
            self._get_knowledge_open()
        except _Rejected as exc:
            self._html(exc.status, exc.reason)
        except _KnowledgeRefused as exc:
            self._html(exc.status, exc.message)
        except ConsolePublishError as exc:
            self.log_message("knowledge open bridge failed: %s", exc)
            self._html(502, "知识库入口暂时不可用，请稍后再试")

    def _get_console_page(self, parsed: urllib.parse.SplitResult) -> None:
        """提供独立管理台 HTML；无 Dify 会话 cookie 时 307 跳转 `/signin`。"""
        session = _console_access_token_from_cookie(self._single_header("Cookie"))
        if session is None:
            raw_target = "/aiops/environments" if parsed.path in {"/aiops", "/aiops/"} else parsed.path
            target = raw_target + (f"?{parsed.query}" if parsed.query else "")
            location = f"/signin?redirect={urllib.parse.quote(target, safe='')}"
            self.send_response(307)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self._send_request_id_header()
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            return

        index_file = Path(self.settings.console_dist_dir) / "index.html"
        if not index_file.is_file():
            self._html(503, "管理工作台前端产物尚未就绪，请检查 aiops-dify-edge 构建")
            return
        body = index_file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self._send_request_id_header()
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _get_console_asset(self, raw_rel_path: str) -> None:
        """只读提供独立管理台 `/aiops/assets/*` 静态构建产物（严格防路径穿越）。"""
        decoded = urllib.parse.unquote(raw_rel_path)
        if not decoded or "\x00" in decoded or decoded.startswith("/") or "\\" in decoded:
            raise _Rejected(404, "not found")
        parts = decoded.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise _Rejected(404, "not found")

        assets_root = (Path(self.settings.console_dist_dir) / "assets").resolve(strict=False)
        candidate = (assets_root / Path(*parts)).resolve(strict=False)
        if candidate == assets_root or not candidate.is_relative_to(assets_root) or not candidate.is_file():
            raise _Rejected(404, "not found")

        content_type_map = {
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".html": "text/html; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }
        ctype = (
            content_type_map.get(candidate.suffix.lower())
            or mimetypes.guess_type(candidate.name)[0]
            or "application/octet-stream"
        )
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的协议
        path = urllib.parse.urlsplit(self.path).path
        try:
            self._select_environment()
            if path == "/__aiops/manage":
                from .self_service_bridge import handle

                handle(self)
                return
            if path == NATIVE_PREVIEW_ROUTE:
                _native_preview(self)
                return
            if path == NATIVE_CANCEL_ROUTE:
                _native_cancel(self)
                return
            if path == _TRIAL_ROUTE:
                self._post_trial()
                return
            if path == _TRIAL_RESULT_ROUTE:
                self._post_trial_result()
                return
            if path == _KNOWLEDGE_SYNC_ROUTE:
                self._post_knowledge_sync()
                return
            operations = {
                _PUBLISH_ROUTE: "publish",
                "/__aiops/retract": "retract",
                "/__aiops/restore": "restore",
                "/__aiops/versions": "versions",
            }
            if path not in operations:
                raise _Rejected(404, "not found")
            session, referer = self._check_browser_request()
            version_no = None
            if operations[path] == "restore":
                payload = self._read_bounded_json(MAX_REQUEST_BODY_BYTES, frozenset({"version_no"}))
                version_no = payload.get("version_no")
                if type(version_no) is not int or version_no < 1:
                    raise _Rejected(400, "请选择有效的历史版本")
            else:
                self._read_empty_body()
            result = publish_current_app(
                dify_base_url=self.settings.dify_base_url,
                gateway_base_url=self.settings.gateway_base_url,
                publish_key=self.settings.publish_key,
                console_session=session,
                console_origin=self.settings.console_origin,
                workspace_id=self.settings.workspace_id,
                referer=referer,
                allowed_roles=self.settings.allowed_roles,
                login_proxy_url=self.settings.login_proxy_url,
                gateway_internal_token=self.settings.gateway_internal_token,
                timeout=self.settings.timeout,
                operation=operations[path],
                version_no=version_no,
            )
        except _Rejected as exc:
            if path.startswith("/__aiops/operator-preview"):
                self._json(exc.status, {"code": "OPERATOR_PREVIEW_REJECTED", "message": exc.reason})
                return
            self._json(exc.status, {"published": False, "reason": exc.reason, "action": ""})
            return
        except ConsolePublishError as exc:
            if path.startswith("/__aiops/operator-preview"):
                self._json(502, {"code": "OPERATOR_PREVIEW_UNAVAILABLE", "message": "预览链路暂时不可用"})
                return
            self.log_message("console publish bridge failed: %s", exc)
            self._json(502, {"published": False, "reason": "发布链路暂时不可用，请稍后再试", "action": ""})
            return
        self._json(result.status, result.public())

    def _select_environment(self):
        """唯一选址点：活动登录响应给环境与目录地址，服务凭据只从本地取。"""
        path = urllib.parse.urlsplit(self.path).path
        if not self.settings.environment_credentials_file:
            session = _console_access_token_from_cookie(self._single_header("Cookie"))
            if self.settings.login_proxy_url and session:
                code, identity = _request(
                    self.settings.login_proxy_url + "/__aiops/publish/check",
                    method="POST",
                    body={"session": session.access_token},
                    timeout=self.settings.timeout,
                )
                if code != 200 or not isinstance(identity, dict) or not identity.get("company_user_id"):
                    raise _Rejected(401, "环境登录已失效，请重新登录")
                if identity.get("environment"):
                    raise _Rejected(403, "当前环境管理连接尚未准备好")
                if identity.get("business_admitted") is False and path != "/__aiops/manage":
                    raise _Rejected(403, "当前环境未开通或已停用，仅允许环境目录管理")
            return
        origin = normalize_origin(self._single_header("Origin"))
        referer = _origin_and_path(self._single_header("Referer") or "")
        if (
            (self.command == "POST" and origin != self.settings.console_origin)
            or (self.command == "POST" and not referer)
            or (referer and referer[0] != self.settings.console_origin)
            or self._single_header("Sec-Fetch-Site") not in (None, "same-origin")
        ):
            raise _Rejected(403, "请从Dify控制台操作")
        if self.command == "GET" or path == _KNOWLEDGE_SYNC_ROUTE:
            session = _console_access_token_from_cookie(self._single_header("Cookie"))
        else:
            session = console_session_from_headers(
                self._single_header("Cookie"), self._single_header(CONSOLE_CSRF_HEADER)
            )
        if session is None:
            raise _Rejected(401, "请重新登录Dify")
        code, identity = _request(
            self.settings.login_proxy_url + "/__aiops/publish/check",
            method="POST",
            body={"session": session.access_token},
            timeout=self.settings.timeout,
        )
        if code != 200 or not isinstance(identity, dict) or not identity.get("company_user_id"):
            raise _Rejected(401, "环境登录已失效，请重新登录")
        if identity.get("business_admitted") is False and path != "/__aiops/manage":
            raise _Rejected(403, "当前环境未开通或已停用，仅允许环境目录管理")
        try:
            value = identity.get("gateway_url")
            if not isinstance(value, str):
                raise ValueError("missing environment endpoint")
            gateway = require_secure_endpoint(value, what="环境Gateway")
        except (TypeError, ValueError):
            raise _Rejected(502, "环境连接不可用") from None
        identifier = identity.get("environment")
        row = self.environment_credentials.get(identifier) if isinstance(identifier, str) else None
        if row is None and self.settings.control_plane_managed_envs:
            # 受管客户环境统一经控制面网关代管：严守目标地址必须与本地配置控制面一致，绝不外泄控制面密钥。
            expected_gateway = require_secure_endpoint(self.settings.gateway_base_url, what="控制面Gateway")
            if gateway == expected_gateway:
                row = self.environment_credentials.get("41") or next(
                    iter(self.environment_credentials.values()), None
                )
        if row is None:
            raise _Rejected(403, "当前环境管理连接尚未准备好")
        self.settings = replace(
            self.settings,
            gateway_base_url=gateway,
            publish_key=row["publish_key"],
            trial_key=row["trial_key"],
            gateway_internal_token=row["gateway_internal_token"],
        )

    def _post_knowledge_sync(self) -> None:
        """让网关把该租户的 RAGFlow 库收敛成 Dify dataset（ADR-0122 / #1056）。"""
        if not self.settings.login_proxy_url or not self.settings.gateway_internal_token:
            raise _Rejected(404, "not found")
        session = _console_access_token_from_cookie(self._single_header("Cookie"))
        if session is None:
            raise _Rejected(401, "请先在 Dify 中登录")
        verdict = _identity_and_publish_verdict_for_identity(
            self.settings.login_proxy_url, console_session=session, timeout=self.settings.timeout
        )
        if verdict is None:
            raise _Rejected(401, "Dify 会话无效或已过期，请重新登录")
        headers, _allowed = verdict
        tenant_id = headers.get("X-AIOps-Initiator-Tenant", "").strip()
        if not tenant_id:
            raise _Rejected(403, "你的账号还没有绑定公司租户，请联系管理员")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        workspace_id = (query.get("workspace") or [""])[0].strip() if query else ""
        if not workspace_id:
            self.log_message("knowledge sync: no workspace in request; nothing synced")
            self._json(200, {"changed": 0, "created": 0})
            return
        try:
            from .self_service_bridge import authorize_workspace

            authorize_workspace(self, session, workspace_id)
            changed = _knowledge_sync_payload(
                self.settings.gateway_base_url,
                self.settings.gateway_internal_token,
                workspace_id=workspace_id,
                timeout=self.settings.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 前端弹窗路径不因抖动阻断
            self.log_message("knowledge sync bridge failed: %s", exc)
            self._json(200, {"changed": 0, "created": 0})
            return
        self.log_message("knowledge sync: tenant=%s changed=%s", tenant_id, changed)
        self._json(200, {"changed": changed, "created": changed})

    def _post_trial(self) -> None:
        """试运行提交（#781 / #782 的部署层）。"""
        if not self.settings.trial_key:
            raise _Rejected(404, "not found")
        session, referer = self._check_browser_request()
        app_id = app_id_from_referer(referer, console_origin=self.settings.console_origin)
        if app_id is None:  # pragma: no cover - _check_browser_request 已保证
            raise _Rejected(403, "无法确定当前是哪个智能体（请从编排页点这个按钮）")
        payload = self._read_bounded_json(MAX_TRIAL_BODY_BYTES, _TRIAL_PAYLOAD_KEYS)
        order_no, question = payload.get("order_no", ""), payload.get("question", "")
        if (
            not isinstance(order_no, str)
            or not isinstance(question, str)
            or len(question) > 4000
            or not (order_no.strip() or question.strip())
        ):
            raise _Rejected(400, "请填写要试运行的问题或订单号")
        if question.strip() and not order_no.strip():
            order_no = _native_order(question)
        try:
            result = submit_trial(
                gateway_base_url=self.settings.gateway_base_url,
                trial_key=self.settings.trial_key,
                app_id=app_id,
                order_no=order_no.strip(),
                question=question.strip(),
                login_proxy_url=self.settings.login_proxy_url,
                console_session=session,
                timeout=self.settings.timeout,
            )
        except ConsolePublishError as exc:
            self.log_message("console trial bridge failed: %s", exc)
            self._json(502, {"submitted": False, "reason": "试运行链路暂时不可用，请稍后再试"})
            return
        self._json(
            result.status,
            {
                "submitted": result.published,
                "reason": result.reason,
                "action": result.action,
                **({"trial_id": result.trial_id} if result.trial_id else {}),
            },
        )

    def _post_trial_result(self) -> None:
        """读一次试运行的结果（结果面板那一条）。"""
        if not self.settings.trial_key:
            raise _Rejected(404, "not found")
        session, referer = self._check_browser_request()
        app_id = app_id_from_referer(referer, console_origin=self.settings.console_origin)
        if app_id is None:  # pragma: no cover - _check_browser_request 已保证
            raise _Rejected(403, "无法确定当前是哪个智能体（请从编排页点这个按钮）")
        payload = self._read_bounded_json(MAX_TRIAL_BODY_BYTES, _TRIAL_RESULT_PAYLOAD_KEYS)
        trial_id = payload.get("trial_id")
        if not isinstance(trial_id, str) or not trial_id.strip():
            raise _Rejected(400, "缺少 trial_id")
        try:
            report = read_trial(
                gateway_base_url=self.settings.gateway_base_url,
                trial_key=self.settings.trial_key,
                trial_id=trial_id.strip(),
                login_proxy_url=self.settings.login_proxy_url,
                console_session=session,
                timeout=self.settings.timeout,
            )
        except ConsolePublishError as exc:
            self.log_message("console trial result bridge failed: %s", exc)
            self._json(502, {"read": False, "reason": "试运行结果暂时读不到，请稍后再试"})
            return
        self._json(report.status, report.public())

    def _sse(self, event: dict) -> None:
        self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
        self.wfile.flush()

    def _single_header(self, name: str) -> str | None:
        """安全敏感头必须恰好一次；重复时拒绝，免得桥与代理对"取哪一个"理解不同。"""
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise _Rejected(400, "duplicate request header")
        return values[0] if values else None

    def _get_knowledge_open(self) -> None:
        """知识库入口（#984 / PRD #981）。"""
        if not self.settings.login_proxy_url or not self.settings.gateway_internal_token:
            raise _Rejected(404, "not found")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        session = _console_access_token_from_cookie(self._single_header("Cookie"))
        if session is None:
            raise _Rejected(401, "请先在 Dify 中登录")
        verdict = _identity_and_publish_verdict_for_identity(
            self.settings.login_proxy_url, console_session=session, timeout=self.settings.timeout
        )
        if verdict is None:
            raise _Rejected(401, "Dify 会话无效或已过期，请重新登录")
        headers, _allowed = verdict
        tenant_id = headers.get("X-AIOps-Initiator-Tenant", "").strip()
        if not tenant_id:
            raise _Rejected(403, "你的账号还没有绑定公司租户，请联系管理员")
        workspace_id = (query.get("workspace") or [""])[0].strip() if query else ""
        if not workspace_id:
            raise _Rejected(400, "缺少当前工作区，请刷新 Dify 页面后重试")
        from .self_service_bridge import authorize_workspace

        authorize_workspace(self, session, workspace_id)
        opened = _knowledge_open_payload(
            self.settings.gateway_base_url,
            self.settings.gateway_internal_token,
            workspace_id=workspace_id,
            timeout=self.settings.timeout,
        )
        self.log_message(
            "knowledge open: target=%s cookies=%d",
            opened.get("target"),
            len(opened.get("cookies") or []),
        )
        self.send_response(303)
        self.send_header("Location", str(opened["target"]))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for cookie in opened.get("cookies") or []:
            self._relay_cookie(cookie)
        self.end_headers()

    def _current_dify_workspace(self, session: ConsoleSession) -> str:
        """问 Dify"当前这个会话停在哪个 workspace"。"""
        origin = urllib.parse.urlsplit(self.settings.dify_base_url)
        if not origin.scheme or not origin.netloc:
            return ""
        cookie = f"{session.cookie_prefix}{CONSOLE_ACCESS_TOKEN_COOKIE}={session.access_token}"
        if session.csrf_token:
            cookie += f"; {session.cookie_prefix}{CONSOLE_CSRF_COOKIE}={session.csrf_token}"

        def failed(failure: HttpFailure) -> Exception:
            return ConsolePublishError(f"dify workspace: {failure.status or failure.detail}")

        payload = request_json(
            RequestSpec(
                url=join_url(self.settings.dify_base_url, "/console/api/workspaces/current/summary"),
                headers={
                    "Accept": "application/json",
                    "Cookie": cookie,
                    CONSOLE_CSRF_HEADER: session.csrf_token,
                },
                timeout=self.settings.timeout,
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
            return ""
        return str(payload.get("id") or "").strip()

    def _relay_cookie(self, cookie: Any) -> None:
        """把网关给的 cookie 属性逐字转成 ``Set-Cookie``。"""
        if not isinstance(cookie, dict):
            return
        name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
        if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
            self.log_message("knowledge open: skipping a malformed cookie")
            return
        parts = [f"{name}={value}"]
        if isinstance(domain, str) and domain:
            parts.append(f"Domain={domain}")
        parts.append("Path=/")
        if cookie.get("httponly"):
            parts.append("HttpOnly")
        if cookie.get("samesite"):
            parts.append(f"SameSite={cookie['samesite']}")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _html(self, status: int, message: str) -> None:
        """给**浏览器顶层导航**用的失败回应：一句人能读的话，不是 JSON。"""
        body = (
            "<!doctype html><meta charset=utf-8><title>知识库入口</title>"
            '<div style="font:16px/1.6 system-ui;max-width:36rem;margin:12vh auto;padding:0 1rem">'
            f'<h1 style="font-size:1.1rem">无法打开知识库</h1><p>{_escape(message)}</p>'
            '<p style="color:#888;font-size:.85rem">关闭本页回到 Dify 重试；问题持续请联系管理员。</p>'
            "</div>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_request_id_header()
        self.end_headers()
        self.wfile.write(body)

    def _check_browser_request(self) -> tuple[ConsoleSession, str]:
        """浏览器侧的门，**一条没过就不问任何上游**。"""
        origin = self._single_header("Origin")
        if origin is None or normalize_origin(origin) != self.settings.console_origin:
            raise _Rejected(403, "请从 Dify 控制台内点发布按钮")
        fetch_site = self._single_header("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site != "same-origin":
            raise _Rejected(403, "请从 Dify 控制台内点发布按钮")
        referer = self._single_header("Referer")
        if (
            referer is None
            or app_id_from_referer(referer, console_origin=self.settings.console_origin) is None
        ):
            raise _Rejected(403, "无法确定当前是哪个智能体（请从编排页点这个按钮）")
        content_type = self._single_header("Content-Type")
        if not content_type or content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise _Rejected(415, "请求必须是 application/json")
        session = console_session_from_headers(
            self._single_header("Cookie"), self._single_header(CONSOLE_CSRF_HEADER)
        )
        if session is None:
            raise _Rejected(401, "Dify 登录凭据不完整，请重新登录后再试")
        return session, referer

    def _read_empty_body(self) -> None:
        """只允许空 body 或 ``{}``，有界读取。"""
        if self._single_header("Transfer-Encoding") is not None:
            raise _Rejected(400, "transfer encoding is not supported")
        raw_length = self._single_header("Content-Length")
        if raw_length is None:
            length = 0
        elif not raw_length.isascii() or not raw_length.isdecimal():
            raise _Rejected(400, "invalid content length")
        elif _CONTENT_LENGTH_RE.fullmatch(raw_length) is None:
            raise _Rejected(413, "payload too large")
        else:
            length = int(raw_length)
        if length > MAX_REQUEST_BODY_BYTES:
            raise _Rejected(413, "payload too large")
        if length == 0:
            return
        self.connection.settimeout(5.0)
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError):
            raise _Rejected(400, "incomplete request body") from None
        if len(raw) != length:
            raise _Rejected(400, "incomplete request body")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _Rejected(400, "invalid request body") from None
        if payload != {}:
            raise _Rejected(400, "request body must be empty")

    def _read_bounded_json(self, limit: int, allowed_keys: frozenset[str]) -> dict[str, Any]:
        """有界读一个 JSON object，且**只允许** ``allowed_keys`` 里的键。"""
        if self._single_header("Transfer-Encoding") is not None:
            raise _Rejected(400, "transfer encoding is not supported")
        raw_length = self._single_header("Content-Length")
        if raw_length is None:
            raise _Rejected(411, "content length required")
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise _Rejected(400, "invalid content length")
        if _CONTENT_LENGTH_RE.fullmatch(raw_length) is None:
            raise _Rejected(413, "payload too large")
        length = int(raw_length)
        if length > limit:
            raise _Rejected(413, "payload too large")
        if length == 0:
            raise _Rejected(400, "request body must not be empty")
        self.connection.settimeout(5.0)
        try:
            raw = self.rfile.read(length)
        except (OSError, TimeoutError):
            raise _Rejected(400, "incomplete request body") from None
        if len(raw) != length:
            raise _Rejected(400, "incomplete request body")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _Rejected(400, "invalid request body") from None
        if not isinstance(payload, dict):
            raise _Rejected(400, "request body must be an object")
        extra = set(payload) - allowed_keys
        if extra:
            raise _Rejected(400, "unexpected field in request body")
        return payload

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_request_id_header()
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        rid = self._request_id()
        prefix = f"console-publish-bridge [rid={rid}]: " if rid else "console-publish-bridge: "
        _LOGGER.info(prefix + fmt, *args)


def make_server(settings: BridgeSettings) -> ThreadingHTTPServer:
    from .environments import credentials

    rows = credentials(settings.environment_credentials_file) if settings.environment_credentials_file else {}
    if rows and not settings.login_proxy_url:
        raise ValueError("environment routing requires an active login proxy")
    keys = set()
    for row in rows.values():
        if (
            set(row) != {"id", "publish_key", "trial_key", "gateway_internal_token"}
            or not all(isinstance(v, str) and len(v) >= 32 for k, v in row.items() if k != "id")
            or row["publish_key"] == row["trial_key"]
            or keys.intersection((row["publish_key"], row["trial_key"]))
        ):
            raise ValueError("invalid environment bridge credentials")
        keys.update((row["publish_key"], row["trial_key"]))
    handler = type(
        "_BoundConsolePublishHandler",
        (ConsolePublishBridgeHandler,),
        {"settings": settings, "environment_credentials": rows},
    )
    return ThreadingHTTPServer((settings.listen_host, settings.listen_port), handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = make_server(BridgeSettings.from_env())
    _LOGGER.info("console publish bridge listening on %s:%s", *server.server_address)
    server.serve_forever()
