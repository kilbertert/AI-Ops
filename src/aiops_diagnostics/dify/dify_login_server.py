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
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from aiops_diagnostics.dify.dify_login_company import CompanyAuthSettings, fetch_captcha
from aiops_diagnostics.dify.dify_login_provision import DifyConsoleSettings
from aiops_diagnostics.dify.dify_login_proxy import (
    CAPTCHA_ROUTE,
    CONSOLE_COOKIE_PREFIXED_NAMES,
    LOGIN_ROUTE,
    LOGOUT_ROUTE,
    PUBLISH_CHECK_ROUTE,
    CompanyLoginRejected,
    CompanyUnreachable,
    DifyLoginError,
    PermissionDenied,
    verify_console_jwt,
)
from aiops_diagnostics.dify.dify_login_service import (
    PLATFORM_USER_TYPE,
    STATUS_ACTIVE,
    LoginPolicy,
    LoginService,
    MappingStore,
)

_LOGGER = logging.getLogger("aiops.dify_login_proxy.server")

MAX_LOGIN_BODY_BYTES = 4096

#: 退避的全局键名与倍数。见 :class:`_Backoff` 的 docstring：来源键可被本机进程伪造，
#: 这条与来源无关的键把伪造的收益封在 `倍数 × 阈值` 以内。
GLOBAL_BACKOFF_KEY = "global"
GLOBAL_BACKOFF_MULTIPLIER = 5
_RANDOM_STR_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


class NotReady(RuntimeError):
    """TLS 未就绪 —— 代理**不得**对外提供登录页。"""


#: 登录成功后跳回哪里 —— 站内相对路径的长度上限。
_MAX_REDIRECT_CHARS = 512


def safe_redirect_path(raw: Any) -> str:
    """把调用方给的 `redirect` 折成一个**站内**相对路径；不合法一律回落 ``/``。

    登录页会把 Dify 送来的 `redirect_url`（例如 `/apps`，来自
    `GET /auth/refresh?redirect_url=%2Fapps` 那条 303）原样带进来，所以这个值是
    **外部输入**。它是开放重定向（钓鱼跳板）的唯一入口，因此判据必须在这里、
    而不是在页面的 JS 里 —— 页面能被绕过，服务端不能。

    **只认单个 `/` 开头**，因为 `//evil.com` 与 `/\\evil.com` 都是浏览器认的
    **协议相对 URL**（会跳到外站）。用白名单正着写，不用黑名单 —— "先去掉再判断"
    那类启发式正是绕过重定向白名单的常见手法。
    """
    if not isinstance(raw, str):
        return "/"
    text = raw.strip()
    if not text or len(text) > _MAX_REDIRECT_CHARS:
        return "/"
    if not text.startswith("/") or text.startswith("//"):
        return "/"
    # 反斜杠与 CR/LF：前者被部分浏览器当分隔符，后者是响应头注入的形状。
    if "\\" in text or "\r" in text or "\n" in text:
        return "/"
    return text


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
    #: **按租户开户**（#883）。三个键成对：
    #: - ``gateway_url`` + ``gateway_internal_token``：问网关"这个租户的 workspace 是哪一个"
    #:   （ADR-0067 的只读端点；凭据是**内部令牌**，不是 Dify 的钥匙）；
    #: - ``inner_api_base_url`` + ``inner_api_key``：把已有账号 join 进那个 workspace。
    #:
    #: **全空 ⇒ 维持单一固定工作区的旧行为**（V1 暂定，ADR-0056），一个还没采纳多 workspace
    #: 的环境因此照常工作。**只配一半是启动错误**：半配置的表现是"解析得到、join 不了"或
    #: 反过来，而两种都看起来像"登录成功了、只是看不到东西"。
    gateway_url: str = ""
    gateway_internal_token: str = field(repr=False, default="")
    inner_api_base_url: str = ""
    inner_api_key: str = field(repr=False, default="")
    operator_trial_key: str = field(repr=False, default="")
    #: 发布桥/Try-run 复用的公司令牌缓存寿命。``None`` ⇒ 跟随控制台会话。
    #: 配得比会话短会重新引入"页面还能开、试运行却 403"的错配，所以只允许显式配。
    operator_token_seconds: int | None = None

    def __post_init__(self) -> None:
        assert_public_origin_is_https(self.public_origin)
        if self.operator_trial_key and len(self.operator_trial_key) < 32:
            raise ValueError("operator trial key must contain at least 32 characters")
        if not self.edit_permission:
            raise ValueError("AIOPS_LOGIN_PROXY_EDIT_PERMISSION is required")
        # 四个键成对（#883）：全配 ⇒ 按租户开户；全空 ⇒ 单一固定工作区。只配一半是
        # 启动错误 —— 半配置在运行时的表现是"登进去了但看不到任何东西"。
        gateway = bool(self.gateway_url) and bool(self.gateway_internal_token)
        inner = bool(self.inner_api_base_url) and bool(self.inner_api_key)
        if gateway != inner:
            raise ValueError(
                "按租户开户需要四键成对：AIOPS_LOGIN_PROXY_GATEWAY_URL/_INTERNAL_TOKEN "
                "与 AIOPS_LOGIN_PROXY_INNER_API_BASE_URL/_KEY 要么全配、要么全空"
            )

    @classmethod
    def from_env(cls) -> LoginProxySettings:
        def required(name: str) -> str:
            value = (os.environ.get(name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        def _optional_int(raw: str | None) -> int | None:
            """空 ⇒ ``None``（跟随会话）；非法值必须启动失败，不能悄悄退回默认。"""
            if raw is None or not raw.strip():
                return None
            value = int(raw)
            if value <= 0:
                raise ValueError("AIOPS_LOGIN_PROXY_OPERATOR_TOKEN_SECONDS must be positive")
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
                # 必填而不是默认值（#864）：它是 Dify 账号邮箱的一部分，漏配的后果是
                # **两个环境撞到同一个账号**（越权），而不是启动失败。所以宁可起不来。
                environment=required("AIOPS_LOGIN_PROXY_ENVIRONMENT"),
            ),
            data_dir=os.environ.get("AIOPS_LOGIN_PROXY_DATA_DIR", "/var/lib/aiops-login-proxy"),
            edit_permission=required("AIOPS_LOGIN_PROXY_EDIT_PERMISSION"),
            publish_permission=os.environ.get("AIOPS_LOGIN_PROXY_PUBLISH_PERMISSION", ""),
            owner_user_id=os.environ.get("AIOPS_LOGIN_PROXY_DIFY_OWNER_ACCOUNT_ID", ""),
            backoff_threshold=int(os.environ.get("AIOPS_LOGIN_PROXY_BACKOFF_THRESHOLD", "5")),
            backoff_window_seconds=int(os.environ.get("AIOPS_LOGIN_PROXY_BACKOFF_WINDOW", "300")),
            gateway_url=os.environ.get("AIOPS_LOGIN_PROXY_GATEWAY_URL", "").strip().rstrip("/"),
            gateway_internal_token=os.environ.get("AIOPS_LOGIN_PROXY_GATEWAY_INTERNAL_TOKEN", "").strip(),
            inner_api_base_url=os.environ.get("AIOPS_LOGIN_PROXY_INNER_API_BASE_URL", "").strip().rstrip("/"),
            inner_api_key=os.environ.get("AIOPS_LOGIN_PROXY_INNER_API_KEY", "").strip(),
            operator_trial_key=os.environ.get("AIOPS_LOGIN_PROXY_OPERATOR_TRIAL_KEY", "").strip(),
            operator_token_seconds=_optional_int(os.environ.get("AIOPS_LOGIN_PROXY_OPERATOR_TOKEN_SECONDS")),
        )


class _Backoff:
    """按来源与账号记失败次数；窗口内超阈即拒。

    进程内存的粗略实现（重启即清零）：这一层的目的是挡住持续猜密码，不是做计费级限流；
    真要跨重启，映射表旁边加一列即可，现在不需要。

    ## 来源必须是**运营的地址**，不是 nginx 的

    这个进程只监听回环，所有请求都经宿主 nginx 同源反代进来，因此
    ``self.client_address`` **恒为 127.0.0.1**。按它计数等于把"来源"这个维度
    抹平成常量：一个运营连错五次，**所有运营**在窗口内都被 429（2026-10-19 实测）。
    真正的修法在 nginx 那一层（realip 把运营地址写回 ``$remote_addr``），应用这一层
    读 nginx 覆盖写的 ``X-Real-IP``。

    只认 nginx 设的那一个头，不认 ``X-Forwarded-For``：后者是**追加**语义，客户端自带
    的值留在链里，采信它等于给"换一个假 IP 绕过退避"开门。

    ## 全局上限：来源这条键可以被**本机**进程伪造

    能连上 10119 的本机进程同样能自带 ``X-Real-IP``。轮换它 + 轮换用户名可以同时绕过
    ``src:`` 与 ``acct:`` 两条键（Devin, PR #851）。应用层分不出"是 nginx 还是本机
    进程"，所以不假装能分出来 —— 另记一条**与来源无关**的全局键，把伪造的收益封在
    ``GLOBAL_BACKOFF_MULTIPLIER`` 倍阈值以内，而不是无限次。
    """

    def __init__(self, threshold: int, window_seconds: int) -> None:
        self.threshold = max(1, threshold)
        #: 全局键的上限：来源与账号两条键都按 threshold 计，全局键按它的这个倍数计。
        #: 取 5 是因为正常使用里"同一窗口内所有人加起来连错 5×阈值次"不会发生
        #: （阈值默认 5 ⇒ 25 次），而拿它去挡伪造是够的；真出现误伤，运维能在 journal
        #: 里看到 `reason=GLOBAL` 并据此调大。
        self.global_threshold = self.threshold * GLOBAL_BACKOFF_MULTIPLIER
        self.window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = recent
        return recent

    def too_many(self, *keys: str) -> str | None:
        """超阈时返回触发的键（供日志用），否则 ``None``。"""
        now = time.monotonic()
        with self._lock:
            for key in keys:
                if len(self._recent(key, now)) >= self.threshold:
                    return key
            if len(self._recent(GLOBAL_BACKOFF_KEY, now)) >= self.global_threshold:
                return GLOBAL_BACKOFF_KEY
        return None

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in (*keys, GLOBAL_BACKOFF_KEY):
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
        if parsed.path == PUBLISH_CHECK_ROUTE:
            self._publish_check()
            return
        if parsed.path == "/__aiops/internal/operator-token":
            self._operator_token()
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
        # ── TLS 硬前置，**按请求判** ──────────────────────────────────────
        # 模块 docstring 与 `assert_public_origin_is_https` 说的都是同一件事：
        # 公司密码不得经明文 HTTP 传输。但那一条看的是**配置字符串**，不看这一次请求；
        # 而 10008 是明文入口（`listen 10008`，公网可达），它和 TLS 入口共用同一份
        # nginx 配置 ⇒ 口令会照常被收下、AES 加密后发往公司。
        #
        # 而且这个入口**不可能登录成功**：会话 cookie 恒定 `Secure` + `__Host-`，
        # 浏览器在 http 源上把它们**静默丢弃** ⇒ 登录 200、跳转 /、立刻弹回 /signin。
        # 所以"接受口令"换不来任何东西，只换来一次明文传输。
        #
        # 判据取 nginx 的 `X-Forwarded-Proto`（覆盖写）。缺失按非 TLS 处理：
        # 这个进程只监听回环、回环上只有宿主 nginx，缺失说明有人直连 —— 那也不该收口令。
        if not self._arrived_over_tls():
            _LOGGER.warning(
                "dify-login: refused a plaintext login from %s",
                self.client_address[0] if self.client_address else "unknown",
            )
            self._json(
                400,
                {
                    "ok": False,
                    "reason": "此入口是明文 HTTP，不能提交公司口令。请使用 https://dify.ranlei.work 登录",
                },
            )
            return
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
        source = self._client_source()
        account_key = f"acct:{username.lower()}"
        blocked = self.backoff.too_many(f"src:{source}", account_key)
        if blocked:
            _LOGGER.warning("dify-login: throttled key=%s source=%s", blocked, source)
            self._json(429, {"ok": False, "reason": "尝试过于频繁，请稍后再试"})
            return
        try:
            session, _summary = self.service.login(
                username=username, password=password, random_str=random_str, code=code
            )
        except (CompanyLoginRejected, CompanyUnreachable, PermissionDenied, DifyLoginError) as exc:
            self.backoff.record_failure(f"src:{source}", account_key)
            status = 403 if isinstance(exc, (CompanyLoginRejected, PermissionDenied)) else 502
            _LOGGER.warning(
                "dify-login: rejected code=%s status=%s source=%s",
                getattr(exc, "code", "UNKNOWN"),
                status,
                source,
            )
            # 只回一句面向运营的原因；**不回显**异常里的上游细节。
            self._json(status, {"ok": False, "reason": _public_reason(exc)})
            return
        except Exception:  # noqa: BLE001 - 兜底：绝不把内部栈或敏感值回给浏览器
            self.backoff.record_failure(f"src:{source}", account_key)
            _LOGGER.exception("dify-login: unexpected failure source=%s", source)
            self._json(500, {"ok": False, "reason": "登录未完成，请稍后重试或联系管理员"})
            return
        self.backoff.clear(f"src:{source}", account_key)
        # 会话 cookie + 跳转控制台。**secure=True**：对外入口已由前置断言保证是 https。
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for cookie in session.cookies(secure=True):
            self.send_header("Set-Cookie", cookie)
        body = json.dumps(
            {"ok": True, "redirect": safe_redirect_path(payload.get("redirect"))},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _publish_check(self) -> None:
        """会话身份查询：验会话 → 查映射 → 答**这个人是谁** + 能不能发布。

        **只回答案与身份，不回权限码全集**：桥需要的是"是谁"（审计与试运行范围）与
        "能不能发布"；把运营的权限码清单交出去会扩大本层泄漏面，而它没有任何用处。

        ## 身份与放行是**两件事**

        `allowed` 单独表达发布权；身份五字段只要映射存在且 active 就回，与 `allowed`
        无关。这个形状是刻意的：试运行与发布用**同一个**调用取身份，但它们要的不是
        同一种授权 —— 试运行只要"我是谁"来算订单范围（#782），发布还要"我能不能发"。
        所以**没有发布权的人仍然拿得到自己的身份**，桥那边不会因为
        `allowed=false` 就把身份一起丢掉（那正是这一段要防的误用）。

        会话无效 / 无映射 / 映射被禁用这三态**只回 `allowed=false` 加原因码** ——
        没有身份可给，也不该在这里区分更多细节。
        """
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._json(400, {"allowed": False, "reason": str(exc)})
            return
        session_token = str(payload.get("session", ""))
        claims = verify_console_jwt(session_token, self.settings.dify.secret_key)
        if claims is None:
            self._json(200, {"allowed": False, "reason": "SESSION_INVALID"})
            return
        account_id = str(claims.get("user_id") or "")
        row = self.service.store.by_dify_account(account_id) if account_id else None
        if row is None:
            self._json(200, {"allowed": False, "reason": "NO_MAPPING"})
            return
        if row.get("status") != STATUS_ACTIVE:
            self._json(200, {"allowed": False, "reason": "MAPPING_DISABLED"})
            return
        permissions = self.service.store.permissions_of(row)
        # 发布权与准入**同一条边界**（ADR-0139）：租户账号在自己的 workspace 里发布 ——
        # ADR-0073 已把发布钉死在发起人所在的 workspace，跨租户在结构上发不出去。
        # 平台账号仍在共用的平台 workspace 里，仍然要全局权限码。
        #
        # `workspace_joined=1` 是"这个人已经在这个 workspace 里"的本地事实（ADR-0072）。
        # 它是一次快照，但会话寿命（ADR-0136）给出了同一个窗口：这枚会话最长活多久，
        # 这条快照就最多旧多久。
        row_type = str(row.get("user_type") or "")
        tenant_account = bool(str(row.get("tenant_id") or "")) and row_type != PLATFORM_USER_TYPE
        allowed = self.service.policy.may_publish(
            permissions,
            tenant_account=tenant_account,
            has_own_workspace=tenant_account and bool(row.get("workspace_joined")),
        )
        self._json(
            200,
            {
                "allowed": allowed,
                "reason": "" if allowed else "MISSING_PUBLISH_PERMISSION",
                # 审计用：桥把这些原样转给网关的发布请求头。
                "company_user_id": str(row.get("company_user_id") or ""),
                "company_username": str(row.get("company_username") or ""),
                "dify_account_id": account_id,
                # #782：试运行要按发起人的公司身份算范围，所以把这两个一起给桥。
                "company_tenant_id": str(row.get("tenant_id") or ""),
                "company_user_type": str(row.get("user_type") or ""),
            },
        )

    def _operator_token(self) -> None:
        import hmac

        key = self.settings.operator_trial_key
        presented = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not key or not hmac.compare_digest(key.encode(), presented.encode()):
            self._json(403, {"error": "refused"})
            return
        try:
            body = self._read_json()
            session = body.get("session", "")
            result = self.service.operator_token(session) if isinstance(session, str) else None
        except (ValueError, TypeError):
            result = None
        self._json(200 if result else 403, result or {"error": "login_required"})

    def _logout(self) -> None:
        from http.cookies import SimpleCookie

        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            for name in ("access_token", "__Host-access_token"):
                if name in cookies:
                    self.service.forget_operator_token(cookies[name].value)
        except Exception:  # noqa: BLE001 - 畸形 cookie 不阻止退出。
            pass
        # Dify 原生登出由前端调它自己的 `/console/api/logout`；代理这里只负责清掉
        # 我们下发的三枚 cookie，避免"退出后 cookie 还在"。
        #
        # 名字必须带 `__Host-`：清一枚**另一个名字**的 cookie 什么都不做，而"退出后
        # 还能用"正是这一段要防的事（#776 把控制台收到 https 后名字就变了）。
        # `Secure` 一并带上 —— 与登录时下发的属性一致，浏览器才会认成同一枚。
        self.send_response(200)
        for name in CONSOLE_COOKIE_PREFIXED_NAMES:
            self.send_header("Set-Cookie", f"{name}=; Path=/; Secure; Max-Age=0")
        body = b'{"ok": true}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _client_source(self) -> str:
        """这次登录的**来源**，用于退避计数。

        优先 ``X-Real-IP``（宿主 nginx 覆盖写的那一个），缺失才回落到对端地址。

        ## 这个头可被**本机**进程伪造 —— 所以退避另有一条全局上限

        这个进程只监听回环，能连上它的本机进程同样能自带 ``X-Real-IP``。轮换这个头
        加轮换用户名，就能同时绕过 ``src:`` 与 ``acct:`` 两条键（Devin, PR #851）。

        从应用层分不出"是 nginx 还是本机进程"，所以不假装能分出来：**再记一条与来源
        无关的全局键**，让伪造的收益被封在 ``GLOBAL_BACKOFF_MULTIPLIER`` 倍阈值以内，
        而不是无限次。真正的隔离仍由 nginx 那一层的 realip 提供（它把真实地址写回
        ``$remote_addr``，见 ``dify-console-10008.conf`` 顶部）。
        """
        forwarded = (self.headers.get("X-Real-IP") or "").strip()
        if forwarded:
            return forwarded[:64]
        address = self.client_address[0] if self.client_address else ""
        return address or "unknown"

    def _arrived_over_tls(self) -> bool:
        """这次请求是否经 TLS 到达。

        取 nginx 覆盖写的 ``X-Forwarded-Proto``（TLS 入口那块的 `location /` 设了它，
        值来自 `$scheme`）。缺失 ⇒ 非 TLS ⇒ **拒绝收口令**（fail-closed）。

        为什么不看 `AIOPS_LOGIN_PROXY_PUBLIC_ORIGIN`：那一条证明的是"我们**有一个**
        TLS 入口"，证明不了"**这一次**请求是从它进来的" —— 而 10008 明文入口与它共用
        同一份 nginx 配置，正是从这个缺口进去的。
        """
        return (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https"

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
    if isinstance(exc, (PermissionDenied, CompanyLoginRejected)):
        # 这两类的 message **本身就是要给运营看的那句话**（由 `_reason_from_message`
        # 从公司提示分流而来，永远是本仓库的固定字符串，不含上游原文或凭据）。
        # 这里再套一层"或验证码无效"的通用文案会把已知成因重新抹平 ——
        # 运营分不清是自己密码打错还是验证码过期。
        return str(exc)
    if isinstance(exc, CompanyUnreachable):
        return "公司登录服务暂时不可用，请稍后重试"
    return "登录未完成，请稍后重试或联系管理员"


def build_service(settings: LoginProxySettings) -> LoginService:
    store = MappingStore(Path(settings.data_dir) / "mappings.db")
    # 四键全配 ⇒ 按租户解析 + join；全空 ⇒ 保持单一固定工作区（ADR-0056 的 V1 暂定）。
    # 两半都由 `__post_init__` 保证成对，所以这里只需要判一次。
    directory = None
    inner = None
    if settings.gateway_url and settings.inner_api_base_url:
        from aiops_diagnostics.dify.dify_inner_api import DifyInnerApi, InnerApiSettings
        from aiops_diagnostics.dify.dify_workspace_directory import GatewayWorkspaceClient

        directory = GatewayWorkspaceClient(
            base_url=settings.gateway_url,
            secret=settings.gateway_internal_token,
        )
        inner = DifyInnerApi(
            InnerApiSettings(base_url=settings.inner_api_base_url, api_key=settings.inner_api_key)
        )
    return LoginService(
        company=settings.company,
        dify=settings.dify,
        store=store,
        policy=LoginPolicy(
            edit_permission=settings.edit_permission,
            publish_permission=settings.publish_permission,
        ),
        owner_user_id=settings.owner_user_id,
        workspace_directory=directory,
        inner_api=inner,
        retain_operator_token=bool(settings.operator_trial_key),
        operator_token_seconds=settings.operator_token_seconds,
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
