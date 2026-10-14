"""登录代理的 HTTP 面（#772 B / #778）：三跳，全部在回环上。

    GET  /__aiops/login/captcha?randomStr=<key>   → 公司验证码 JPEG（透传）
    POST /__aiops/login                            → 公司账号密码 → Dify 会话 cookie
    POST /__aiops/login/logout                     → 清 cookie（Dify 原生登出）

## 三条硬前置

1. **TLS 未就绪则拒绝服务。** 公司密码不得经明文 HTTP 传输。就绪判据是配置的
   `AIOPS_LOGIN_PROXY_PUBLIC_ORIGIN` 以 `https://` 开头 —— 与 Dify 自己判断
   `is_secure()` 的方式一致（看配置字符串，不看请求）。未就绪时**启动即失败**，
   不是一个能跑但危险的状态。
2. **只监听回环。** 对外那一跳由宿主 nginx 同源反代（与发布桥同一拓扑）。
3. **不记录、不落盘公司密码与令牌。** 成功与失败都只记公司用户 id 与结果码；
   密码/令牌从不出现在日志、异常消息或响应里。

## 失败要对运营可读，但不能漏内部

公司失败 → 一句话中文原因；Dify 开户失败 → 「开户未完成」；无编辑权限码 → 明确说明
缺哪个权限。**任何一条都不回显密码、令牌或上游原始报文。**
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from aiops_diagnostics.dify_login_company import CompanyAuthSettings, fetch_captcha
from aiops_diagnostics.dify_login_provision import DifyConsoleSettings
from aiops_diagnostics.dify_login_proxy import (
    CAPTCHA_ROUTE,
    LOGIN_ROUTE,
    LOGOUT_ROUTE,
    CompanyLoginRejected,
    CompanyUnreachable,
    DifyLoginError,
    PermissionDenied,
)
from aiops_diagnostics.dify_login_service import LoginPolicy, LoginService, MappingStore

_LOGGER = logging.getLogger("aiops.dify_login_proxy.server")

MAX_LOGIN_BODY_BYTES = 4096
_RANDOM_STR_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


class NotReady(RuntimeError):
    """TLS 未就绪 —— 代理**不得**对外提供登录页。"""


def assert_public_origin_is_https(origin: str) -> str:
    """TLS 硬前置：对外入口必须是 https，否则拒绝启动。

    只看配置字符串（与 Dify 的 `is_secure()` 同一条判据），因为代理无法从一次请求
    判断它前面那一跳是否 TLS 终止 —— 那正是它要求**部署**保证的东西。
    """
    normalized = origin.strip()
    if not normalized.startswith("https://"):
        raise NotReady("AIOPS_LOGIN_PROXY_PUBLIC_ORIGIN 必须是 https:// —— 公司密码不得经明文传输")
    return normalized.rstrip("/")


@dataclass(frozen=True, slots=True)
class LoginProxySettings:
    listen_host: str
    listen_port: int
    public_origin: str
    company: CompanyAuthSettings
    dify: DifyConsoleSettings
    data_dir: str
    edit_permission: str
    publish_permission: str = ""
    owner_user_id: str = ""
    #: 失败退避：同一来源在窗口内的失败次数达到阈值即拒绝，直到窗口过去。
    backoff_threshold: int = 5
    backoff_window_seconds: int = 300

    def __post_init__(self) -> None:
        assert_public_origin_is_https(self.public_origin)
        if not self.edit_permission:
            raise ValueError("AIOPS_LOGIN_PROXY_EDIT_PERMISSION is required")

    @classmethod
    def from_env(cls) -> LoginProxySettings:
        def required(name: str) -> str:
            value = (os.environ.get(name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        return cls(
            listen_host=os.environ.get("AIOPS_LOGIN_PROXY_HOST", "127.0.0.1"),
            listen_port=int(os.environ.get("AIOPS_LOGIN_PROXY_PORT", "10119")),
            public_origin=required("AIOPS_LOGIN_PROXY_PUBLIC_ORIGIN"),
            company=CompanyAuthSettings(
                base_url=required("AIOPS_LOGIN_PROXY_COMPANY_URL").rstrip("/"),
                client_type=os.environ.get("AIOPS_LOGIN_PROXY_COMPANY_CLIENT_TYPE", "admin"),
                info_client_type=os.environ.get("AIOPS_LOGIN_PROXY_COMPANY_INFO_CLIENT_TYPE", ""),
                encode_key=required("AIOPS_LOGIN_PROXY_COMPANY_AES_KEY"),
                basic_auth=os.environ.get("AIOPS_LOGIN_PROXY_COMPANY_BASIC_AUTH", ""),
            ),
            dify=DifyConsoleSettings(
                base_url=required("AIOPS_LOGIN_PROXY_DIFY_URL").rstrip("/"),
                secret_key=required("AIOPS_LOGIN_PROXY_DIFY_SECRET_KEY"),
                deployment_edition=os.environ.get("AIOPS_LOGIN_PROXY_DIFY_EDITION", "COMMUNITY"),
                access_expire_minutes=int(os.environ.get("AIOPS_LOGIN_PROXY_ACCESS_MINUTES", "60")),
            ),
            data_dir=os.environ.get("AIOPS_LOGIN_PROXY_DATA_DIR", "/var/lib/aiops-login-proxy"),
            edit_permission=required("AIOPS_LOGIN_PROXY_EDIT_PERMISSION"),
            publish_permission=os.environ.get("AIOPS_LOGIN_PROXY_PUBLISH_PERMISSION", ""),
            owner_user_id=os.environ.get("AIOPS_LOGIN_PROXY_DIFY_OWNER_ACCOUNT_ID", ""),
            backoff_threshold=int(os.environ.get("AIOPS_LOGIN_PROXY_BACKOFF_THRESHOLD", "5")),
            backoff_window_seconds=int(os.environ.get("AIOPS_LOGIN_PROXY_BACKOFF_WINDOW", "300")),
        )


class _Backoff:
    """按来源与账号记失败次数；窗口内超阈即拒。

    进程内存的粗略实现（重启即清零）：这一层的目的是挡住持续猜密码，不是做计费级限流；
    真要跨重启，映射表旁边加一列即可，现在不需要。
    """

    def __init__(self, threshold: int, window_seconds: int) -> None:
        self.threshold = max(1, threshold)
        self.window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def too_many(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                recent = [t for t in self._hits.get(key, []) if now - t < self.window]
                self._hits[key] = recent
                if len(recent) >= self.threshold:
                    return True
        return False

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                self._hits.setdefault(key, []).append(now)

    def clear(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._hits.pop(key, None)


class LoginProxyHandler(BaseHTTPRequestHandler):
    """Three routes; everything else is a 404 that says nothing."""

    settings: LoginProxySettings
    service: LoginService
    backoff: _Backoff
    server_version = "aiops-dify-login-proxy"

    # ── 路由 ────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == CAPTCHA_ROUTE:
            self._captcha(parsed)
            return
        self._json(404, {"ok": False, "reason": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == LOGIN_ROUTE:
            self._login()
            return
        if parsed.path == LOGOUT_ROUTE:
            self._logout()
            return
        self._json(404, {"ok": False, "reason": "not found"})

    # ── 三跳 ────────────────────────────────────────────────────────────────

    def _captcha(self, parsed: Any) -> None:
        from urllib.parse import parse_qs

        raw = (parse_qs(parsed.query).get("randomStr") or [""])[0]
        if not _RANDOM_STR_RE.match(raw):
            self._json(400, {"ok": False, "reason": "randomStr 不合法"})
            return
        try:
            body, content_type = fetch_captcha(self.settings.company, raw)
        except CompanyUnreachable:
            # 验证码取不到是**上游不可用**，不是"你输错了"。
            self._json(502, {"ok": False, "reason": "公司验证码服务暂时不可用"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _login(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._json(400, {"ok": False, "reason": str(exc)})
            return
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        random_str = str(payload.get("randomStr", "")).strip()
        code = str(payload.get("code", "")).strip()
        if not username or not password or not random_str or not code:
            self._json(400, {"ok": False, "reason": "账号、密码、验证码均不能为空"})
            return
        source = self.client_address[0] if self.client_address else "unknown"
        account_key = f"acct:{username.lower()}"
        if self.backoff.too_many(f"src:{source}", account_key):
            self._json(429, {"ok": False, "reason": "尝试过于频繁，请稍后再试"})
            return
        try:
            session, _summary = self.service.login(
                username=username, password=password, random_str=random_str, code=code
            )
        except (CompanyLoginRejected, CompanyUnreachable, PermissionDenied, DifyLoginError) as exc:
            self.backoff.record_failure(f"src:{source}", account_key)
            status = 403 if isinstance(exc, (CompanyLoginRejected, PermissionDenied)) else 502
            # 只回一句面向运营的原因；**不回显**异常里的上游细节。
            self._json(status, {"ok": False, "reason": _public_reason(exc)})
            return
        except Exception:  # noqa: BLE001 - 兜底：绝不把内部栈或敏感值回给浏览器
            self.backoff.record_failure(f"src:{source}", account_key)
            _LOGGER.exception("dify-login: unexpected failure")
            self._json(500, {"ok": False, "reason": "登录未完成，请稍后重试或联系管理员"})
            return
        self.backoff.clear(f"src:{source}", account_key)
        # 会话 cookie + 跳转控制台。**secure=True**：对外入口已由前置断言保证是 https。
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for cookie in session.cookies(secure=True):
            self.send_header("Set-Cookie", cookie)
        body = json.dumps({"ok": True, "redirect": "/"}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _logout(self) -> None:
        # Dify 原生登出由前端调它自己的 `/console/api/logout`；代理这里只负责清掉
        # 我们下发的三枚 cookie，避免"退出后 cookie 还在"。
        self.send_response(200)
        for name in ("access_token", "refresh_token", "csrf_token"):
            self.send_header("Set-Cookie", f"{name}=; Path=/; Max-Age=0")
        body = b'{"ok": true}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length <= 0 or length > MAX_LOGIN_BODY_BYTES:
            raise ValueError("请求体为空或过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        return payload

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler 默认把请求行打进 stderr —— 里面可能带查询串。
        # 只打我们自己给的那句，且调用方已保证不含凭据。
        _LOGGER.info("dify-login-proxy: " + fmt, *args)


def _public_reason(exc: Exception) -> str:
    if isinstance(exc, PermissionDenied):
        return str(exc)
    if isinstance(exc, CompanyLoginRejected):
        return "公司账号或密码不正确，或验证码无效"
    if isinstance(exc, CompanyUnreachable):
        return "公司登录服务暂时不可用，请稍后重试"
    return "登录未完成，请稍后重试或联系管理员"


def build_service(settings: LoginProxySettings) -> LoginService:
    store = MappingStore(Path(settings.data_dir) / "mappings.db")
    return LoginService(
        company=settings.company,
        dify=settings.dify,
        store=store,
        policy=LoginPolicy(
            edit_permission=settings.edit_permission,
            publish_permission=settings.publish_permission,
        ),
        owner_user_id=settings.owner_user_id,
    )


def make_server(settings: LoginProxySettings) -> ThreadingHTTPServer:
    service = build_service(settings)
    handler = type(
        "_BoundLoginProxyHandler",
        (LoginProxyHandler,),
        {
            "settings": settings,
            "service": service,
            "backoff": _Backoff(settings.backoff_threshold, settings.backoff_window_seconds),
        },
    )
    return ThreadingHTTPServer((settings.listen_host, settings.listen_port), handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = LoginProxySettings.from_env()
    except NotReady as exc:
        # TLS 前置不满足 ⇒ **拒绝服务**，而不是降级跑起来。
        _LOGGER.error("dify-login-proxy refuses to start: %s", exc)
        raise SystemExit(2) from exc
    server = make_server(settings)
    _LOGGER.info("dify login proxy listening on %s:%s", *server.server_address)
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover - process entry
    main()


__all__ = [
    "LoginProxySettings",
    "NotReady",
    "assert_public_origin_is_https",
    "build_service",
    "make_server",
]
