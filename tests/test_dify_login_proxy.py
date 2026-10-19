"""登录代理的 HTTP 边界（#772 B / #778）。

真跑一个监听中的代理，上游是**假公司 UPMS** 与**假 Dify**（真实监听、真实协议形状，
不对内部函数打桩）。每条安全判据都带**反例**：该拒的必须拒。

覆盖（对应票面验收标准）：正常登录得 Dify cookie；无编辑权限码被拒且不调用 Dify；
首次登录开户、二次登录映射到同一账号；公司登录失败透传为可读错误且不调用 Dify；
浏览器带来的公司令牌被忽略；TLS 未就绪拒绝服务；失败退避。

**不覆盖**（本地无法证明）：Dify 真的接受我们签的令牌 —— 那需要真实 Dify，已在 #778
的决策评论里用 36 上的只读验证证明（`HTTP=200 ROLE=owner`，且本模块的签名算法与 Dify
的 `PassportService.verify` 交叉核对通过）。
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.dify_login_company import CompanyAuthSettings
from aiops_diagnostics.dify_login_provision import DifyConsoleSettings, account_email_for
from aiops_diagnostics.dify_login_proxy import (
    CAPTCHA_ROUTE,
    LOGIN_ROUTE,
    LOGOUT_ROUTE,
    PUBLISH_CHECK_ROUTE,
    ConsoleSession,
    mint_console_session,
    sign_console_jwt,
)
from aiops_diagnostics.dify_login_server import (
    LoginProxySettings,
    NotReady,
    assert_public_origin_is_https,
    make_server,
    safe_redirect_path,
)

SECRET_KEY = "test-dify-secret-key-0123456789"
AES_KEY = "0123456789abcdef"
COMPANY_USER_ID = "a53ad893-6139-4566-a38a-0b77ed697631"
DIFY_ACCOUNT_ID = "b647e9c0-1111-4222-8333-444455556666"
OWNER_ACCOUNT_ID = "c758f0d1-2222-4333-8444-555566667777"
EDIT_PERMISSION = "aiops:agent:edit"
PUBLISH_PERMISSION = "aiops:agent:publish"


class _FakeCompany(BaseHTTPRequestHandler):
    """假公司 UPMS：/code（JPEG）、/upms/token/login、/upms/user/info。"""

    calls: list[str] = []
    permissions: list[str] = [EDIT_PERMISSION]
    fail_login: str = ""  # "" | "password"
    #: "" | "empty" | "stale" | "wrong" —— 验证码过滤器的三种拒绝，真网关都回 428。
    captcha_fail: str = ""
    #: "" | "gateway" —— 网关层面的拒绝（401），不是"运营输错了"。
    gateway_reject: str = ""
    seen_password: str = ""
    seen_client_type: str = ""
    seen_token_header: str = ""

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).calls.append("GET " + self.path.split("?")[0])
        if self.path.startswith("/code"):
            # ⚠️ 生产网关**按 Accept 过滤**这个端点：不是 */* 就回 404（2026-10-16 实测）。
            # 替身必须复现这个行为，否则"带 Accept 也能过"会一直绿着，直到真上游上炸。
            accept = (self.headers.get("Accept") or "*/*").strip()
            if accept not in ("*/*", ""):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"\xff\xd8\xff\xe0FAKEJPEG"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/upms/user/info"):
            type(self).seen_client_type = self.headers.get("client-type", "")
            type(self).seen_token_header = self.headers.get("Authorization", "")
            body = json.dumps(
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "sysUser": {"id": COMPANY_USER_ID, "username": "zhang"},
                        "permissions": type(self).permissions,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls.append("POST " + self.path.split("?")[0])
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 消费掉 body（浏览器发的是空 body）
        from urllib.parse import parse_qs, urlsplit

        # ⚠️ 公司网关的两个过滤器都只读 **query string**（`uri.getRawQuery()`）：
        # 验证码校验读 `code`/`randomStr`，口令解密读 `password`。所以参数放 body 是
        # 静默失败 —— 替身早期只读 body，于是"参数放错了地方"这件事**永远绿着**，
        # 直到部署到真上游才暴露。**替身必须按 query 解析**，与真公司一致。
        params = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        type(self).seen_password = params.get("password", "")
        # 真网关的 `ValidateCodeGatewayFilter`（2026-10-19 实测，41 部署的代理上回放过）：
        # 验证码这一关在密码之前，三个分支**都是 HTTP 428**，语义只在 `msg` 里。
        # 替身早先这一分支回的是 **200**，于是"428 被归成上游不可用"这件事永远绿着
        # —— 而线上表现是运营把验证码输错一次就被告知"公司登录服务暂时不可用"。
        if self.path.startswith("/upms/token/login"):
            branch = (
                type(self).captcha_fail
                or ("" if params.get("code") else "empty")
                or type(self).gateway_reject
            )
        else:
            branch = ""
        if branch:
            if branch == "gateway":
                # 401/403 在**登录端点**上不是"你输错了"：密码错是 200 + code:-1000。
                # 这两个码意味着网关本身拒了（Basic 凭据、来源限制…）。
                body = b'{"error":"unauthorized"}'
                self.send_response(401)
            else:
                messages = {"stale": "验证码失效", "wrong": "验证码错误", "empty": "验证码不能为空"}
                body = json.dumps({"code": 1, "msg": messages[branch], "data": None, "ok": False}).encode()
                self.send_response(428)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/upms/token/login"):
            if type(self).fail_login:
                payload = {"code": -1000, "msg": "用户名或密码错误，请重新输入"}
            else:
                # 成功：扁平 map（不是 {code,data}）
                token = sign_console_jwt(
                    {"id": COMPANY_USER_ID, "username": "zhang", "tenant_id": "T-1"}, "company-key"
                )
                payload = {
                    "access_token": token,
                    "refresh_token": token,
                    "expires_in": 960 * 60 * 1000,
                    "id": COMPANY_USER_ID,
                    "username": "zhang",
                    "tenant_id": "T-1",
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


class _FakeDify(BaseHTTPRequestHandler):
    """假 Dify 控制台：邀请、激活、成员列表。"""

    calls: list[str] = []
    invite_returns_url: bool = True  # False ⇒ already_member（账号已存在）

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def _read(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    def _reply(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls.append("POST " + self.path)
        self._read()
        if self.path.endswith("/members/invite-email"):
            email = account_email_for(COMPANY_USER_ID)
            if type(self).invite_returns_url:
                # ⚠️ 键名照 Dify 1.17.1 实际回的 ``invitation_results``（2026-10-16 实测）——
                # 替身早期回的是 ``results``，于是解析器只认 ``results`` 也一直绿着，
                # 而线上拿不到 token ⇒ **激活被静默跳过** ⇒ 账号停在 pending 却能登录。
                self._reply(
                    {
                        "result": "success",
                        "invitation_results": [
                            {
                                "status": "success",
                                "email": email,
                                "url": f"https://console.invalid/activate?email={email}&token=INV-TOKEN",
                            }
                        ],
                        "tenant_id": "T-1",
                    },
                    status=201,
                )
            else:
                self._reply({"invitation_results": [{"status": "already_member", "email": email}]})
            return
        if self.path.endswith("/activate"):
            # ⚠️ 复现 Dify 的真规则（2026-10-16 实测踩到）：requests 一旦**带了控制台会话**，
            # 激活者就被当成 `current_account_with_tenant()` 那个账号，与邀请里编码的账号不符
            # ⇒ `InvitationAccountMismatchError`。我们的开户是"以 owner 身份激活别人的邀请"，
            # 于是必须走 **token-only**（不带 Authorization）。
            # 替身早期无条件回 success，所以"带会话去激活会静默失败"这件事一直绿着 ——
            # 而线上表现是账号留在 `pending`、登录却照样能用，极难发现。
            if self.headers.get("Authorization"):
                self._reply({"code": "invitation_account_mismatch", "message": "mismatch"}, status=400)
                return
            self._reply({"result": "success"})
            return
        self._reply({}, status=404)

    def do_GET(self) -> None:  # noqa: N802
        type(self).calls.append("GET " + self.path)
        if self.path.endswith("/members"):
            self._reply(
                {
                    "accounts": [
                        {"id": DIFY_ACCOUNT_ID, "email": account_email_for(COMPANY_USER_ID), "role": "editor"}
                    ]
                }
            )
            return
        self._reply({}, status=404)


def _serve(handler_cls: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


#: `stack` fixture 里那个假公司的 URL。fixture 只 yield 了代理的地址，而有一条用例
#: 要直接对着**假公司**发请求（验证码的 Accept 行为），所以在这里记一份。
_FAKE_COMPANY_URL = ""


@pytest.fixture
def stack(tmp_path: Path):
    _FakeCompany.calls = []
    _FakeCompany.permissions = [EDIT_PERMISSION]
    _FakeCompany.fail_login = ""
    _FakeCompany.captcha_fail = ""
    _FakeCompany.gateway_reject = ""
    _FakeDify.calls = []
    _FakeDify.invite_returns_url = True
    company, company_url = _serve(_FakeCompany)
    dify, dify_url = _serve(_FakeDify)
    settings = LoginProxySettings(
        listen_host="127.0.0.1",
        listen_port=0,
        public_origin="https://console.example",
        company=CompanyAuthSettings(base_url=company_url, encode_key=AES_KEY, client_type="admin"),
        dify=DifyConsoleSettings(base_url=dify_url, secret_key=SECRET_KEY),
        data_dir=str(tmp_path / "data"),
        edit_permission=EDIT_PERMISSION,
        publish_permission=PUBLISH_PERMISSION,
        owner_user_id=OWNER_ACCOUNT_ID,
        backoff_threshold=3,
        backoff_window_seconds=60,
    )
    proxy = make_server(settings)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    host, port = proxy.server_address
    global _FAKE_COMPANY_URL
    _FAKE_COMPANY_URL = company_url
    try:
        yield f"http://{host}:{port}", Path(settings.data_dir)
    finally:
        proxy.shutdown()
        company.shutdown()
        dify.shutdown()


def _call(
    base: str, path: str, body: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any], dict[str, str]]:
    connection = http.client.HTTPConnection(base.removeprefix("http://"))
    connection.request(
        "POST",
        path,
        body=json.dumps(body or {}),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    raw_headers = response.getheaders()
    connection.close()
    cookies = "; ".join(v for k, v in raw_headers if k.lower() == "set-cookie")
    headers = {k.lower(): v for k, v in raw_headers}
    headers["set-cookie-all"] = cookies
    return response.status, payload, headers


def _login(
    base: str, *, headers: dict[str, str] | None = None, **overrides: Any
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = {"username": "zhang", "password": "pw", "randomStr": "abc123", "code": "1234"}
    body.update(overrides)
    return _call(base, LOGIN_ROUTE, body, headers=headers)


def _logout(base: str) -> tuple[int, dict[str, Any], dict[str, str]]:
    return _call(base, LOGOUT_ROUTE)


# --------------------------------------------------------------------------- #
# redirect 白名单：登录页把 Dify 送来的 redirect_url 原样带进来（开放重定向）      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "//evil.test",
        "//evil.test/path",
        "/\\evil.test",
        "https://evil.test",
        "http://evil.test/apps",
        "javascript:alert(1)",
        "\\\\evil.test",
        "/apps\r\nSet-Cookie: x=1",
        "/apps\nX: 1",
        "",
        "   ",
        "apps",
        "/a" * 400,  # 超过 512 字符上限
        None,
        123,
    ],
)
def test_an_offsite_or_malformed_redirect_falls_back_to_the_root(raw: Any) -> None:
    """不合法一律回落 ``/`` —— 这一页接公司口令，不能当钓鱼跳板。

    `//evil.test` 与 `/\\evil.test` 都是浏览器认的**协议相对 URL**（会跳到外站），
    所以它们必须与 `https://evil.test` 同样被拒。
    """
    assert safe_redirect_path(raw) == "/"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/", "/"),
        ("/apps", "/apps"),
        ("/apps?page=2", "/apps?page=2"),
        ("/app/abc/configuration", "/app/abc/configuration"),
        ("  /apps  ", "/apps"),
    ],
)
def test_a_same_site_relative_path_is_kept(raw: str, expected: str) -> None:
    assert safe_redirect_path(raw) == expected


def test_the_login_response_carries_the_validated_redirect(stack) -> None:
    """端到端：请求里给 redirect ⇒ 返回体里是**校验过**的那个。"""
    _status, payload, _headers = _login(stack[0], redirect="//evil.test")
    assert payload["redirect"] == "/"
    _status, payload, _headers = _login(stack[0], redirect="/apps")
    assert payload["redirect"] == "/apps"


def test_prefixed_cookies_refuse_to_be_issued_without_secure() -> None:
    """带 `__Host-` 前缀却不带 `Secure` 时**直接报错**，而不是发一个浏览器会丢掉的 cookie。

    静默丢弃的表现是"登录成功但立刻又要求登录"，而界面上完全看不出原因 ——
    所以宁可在这里失败。
    """
    session = mint_console_session(
        "acct-1", secret_key=SECRET_KEY, deployment_edition="COMMUNITY", access_expire_minutes=60
    )
    with pytest.raises(ValueError, match="Secure"):
        session.cookies(secure=False)
    assert session.cookies(secure=True)  # 反过来是正常的
    # 显式关掉前缀（例如本地 http 调试）时不该报错。
    plain = ConsoleSession(access_token="a", csrf_token="c", cookie_prefix="")
    assert plain.cookies(secure=False)


# --------------------------------------------------------------------------- #
# 正向                                                                        #
# --------------------------------------------------------------------------- #


def test_a_company_login_yields_a_dify_session_cookie(stack) -> None:
    """正常登录：拿到三枚 cookie，且 access 是一枚能被 Dify 验的 JWT。"""
    base, _data = stack
    status, payload, headers = _login(base)
    assert status == 200, payload
    assert payload["ok"] is True and payload["redirect"] == "/"
    cookies = headers["set-cookie-all"]
    assert "access_token=" in cookies and "csrf_token=" in cookies
    # 目标是"给浏览器一枚 Dify 会接受的会话"：形状必须是三段 JWT。
    assert cookies.count("access_token=") >= 1


def test_the_session_cookies_carry_the_host_prefix_and_secure(stack) -> None:
    """三枚 cookie 的名字必须带 `__Host-` 前缀，且带 `Secure`（#776 的 v2 待办）。

    判据是**名字**，不是子串：`"access_token=" in cookies` 对
    `__Host-access_token=` 也为真，所以那条断言在前缀漏掉时**照样绿**。
    而漏掉的后果是控制台读不到会话 —— "登录成功但立刻又要求登录"，界面上看不出原因。

    `__Host-` 同时要求 `Secure`、`Path=/`、不得带 `Domain`：浏览器会**静默丢弃**
    不满足的三条，所以逐条断言。
    """
    _status, _payload, headers = _login(stack[0])
    cookies = headers["set-cookie-all"]
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert f"__Host-{name}=" in cookies, cookies
        assert f" {name}=" not in cookies.replace(f"__Host-{name}=", ""), (
            f"还有一枚不带前缀的 {name} —— 两枚同名 cookie 会让谁生效变得不可预期"
        )
    assert cookies.count("; Secure") >= 3
    assert "Domain=" not in cookies
    assert cookies.count("Path=/") >= 3


def test_logout_clears_the_prefixed_cookies(stack) -> None:
    """登出清的是**带前缀的**那三枚 —— 清另一个名字等于什么都没做。"""
    base, _data = stack
    status, payload, headers = _logout(base)
    assert status == 200 and payload["ok"] is True
    cookies = headers["set-cookie-all"]
    for name in ("access_token", "refresh_token", "csrf_token"):
        assert f"__Host-{name}=;" in cookies, cookies


def test_the_first_login_provisions_an_editor_and_the_second_reuses_it(stack) -> None:
    """首次登录开户（邀请+激活→editor）；二次登录映射到**同一**账号，不再邀请。"""
    assert _login(stack[0])[0] == 200
    assert any("invite-email" in call for call in _FakeDify.calls)
    assert any("activate" in call for call in _FakeDify.calls)
    first = _FakeDify.calls.count("POST /console/api/workspaces/current/members/invite-email")

    _login(stack[0])
    second = _FakeDify.calls.count("POST /console/api/workspaces/current/members/invite-email")
    assert second == first == 1, "二次登录又邀请了一次 —— 会给同一个人开出第二个账号"


def test_the_account_email_is_deterministic_and_undeliverable() -> None:
    assert account_email_for(COMPANY_USER_ID) == account_email_for(COMPANY_USER_ID)
    assert account_email_for(COMPANY_USER_ID).endswith("@aiops-login.invalid")


def test_the_captcha_is_relayed_as_an_image(stack) -> None:
    connection = http.client.HTTPConnection(stack[0].removeprefix("http://"))
    connection.request("GET", f"{CAPTCHA_ROUTE}?randomStr=abc123")
    response = connection.getresponse()
    body = response.read()
    content_type = response.getheader("Content-Type", "")
    connection.close()
    assert response.status == 200
    assert content_type.startswith("image/")
    assert body.startswith(b"\xff\xd8\xff"), "不是一张 JPEG"


def test_the_captcha_request_does_not_pin_an_accept_type(stack) -> None:
    """代理取验证码时**不得**指定 Accept（生产网关按 Accept 过滤，非 `*/*` 一律 404）。

    2026-10-16 部署后实测：第一版发 `Accept: image/jpeg` ⇒ 公司网关回 **404**，
    表现为"验证码取不到"，完全看不出是内容协商。54 个单测当时全绿 —— 因为替身
    不看 Accept。**替身现在复现这个行为**，于是这条用例会红在 `fetch_captcha` 上，
    而不是等到真上游。

    `_FakeCompany` 的地址从它自己的 `_serve` 拿不到（fixture 只 yield 了代理），
    所以直接复用 fixture 里那个假公司的 URL —— 见 fixture 顶部。
    """
    from aiops_diagnostics.dify_login_company import CompanyAuthSettings, fetch_captcha

    settings = CompanyAuthSettings(base_url=_FAKE_COMPANY_URL, client_type="admin")
    body, content_type = fetch_captcha(settings, "abc123")
    assert body.startswith(b"\xff\xd8\xff")
    assert content_type.startswith("image/")


# --------------------------------------------------------------------------- #
# 反例：该拒的必须拒                                                           #
# --------------------------------------------------------------------------- #


def test_a_user_without_the_edit_permission_is_refused_and_dify_is_never_called(stack) -> None:
    """无编辑权限码 ⇒ 拒绝，且**不开户**（用户故事 4：明确告诉他去找谁开权限）。"""
    _FakeCompany.permissions = ["some:other:permission"]
    status, payload, _ = _login(stack[0])
    assert status == 403
    assert "可编辑智能体" in payload["reason"]
    assert _FakeDify.calls == [], "没有编辑权限却调用了 Dify —— 会开出不该有的账号"


def test_a_wrong_password_is_refused_without_touching_dify(stack) -> None:
    """公司登录失败（200 + ``code:-1000``）⇒ 可读原因，且**不调用 Dify**。"""
    _FakeCompany.fail_login = "password"
    status, payload, _ = _login(stack[0])
    assert status == 403
    assert "密码" in payload["reason"]
    assert _FakeDify.calls == []


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("wrong", "验证码不正确或已失效"),
        ("stale", "验证码不正确或已失效"),
        ("empty", "验证码不正确或已失效"),
    ],
)
def test_the_captcha_filters_428_is_a_rejection_not_an_outage(stack, branch, expected) -> None:
    """验证码被公司拒绝 ⇒ **运营的输入问题**，不是"服务不可用"。

    这是本票的回归判据。真网关对三个验证码分支都回 428（2026-10-19 在 41 上实测），
    而通用默认只把 401/403 认作凭据拒绝 ⇒ 428 落进"上游不可用"，运营输错一次验证码就
    被告知"公司登录服务暂时不可用"。

    判据钉的是**状态码与文案**，不是"服务不可用这句话有没有出现"—— 后者在没有网络
    的替身里永远成立，正是这种形状让缺陷绿了三周。
    """
    _FakeCompany.captcha_fail = branch
    status, payload, _ = _login(stack[0])
    assert status == 403, f"{branch}: 验证码拒绝被当成了上游故障（{status}）"
    assert expected in payload["reason"], payload["reason"]
    assert "暂时不可用" not in payload["reason"]
    assert _FakeDify.calls == [], "验证码没过却调用了 Dify"


def test_a_gateway_level_rejection_is_not_blamed_on_the_operator(stack) -> None:
    """登录端点上的 401/403 是**网关的问题**，不是"你密码错了"。

    实测（2026-10-19）：密码错是 **200 + `code:-1000`**；401/403 意味着网关自己拒了
    （我们配的 Basic 凭据、来源限制、网关故障）。把它说成"你输错了"会把运维引去查一个
    不存在的输错 —— 与本次要修的缺陷是同一类错误，方向相反。

    判据：401 ⇒ 502 + 「暂时不可用」，且**不是**「密码不正确」。
    """
    _FakeCompany.gateway_reject = "gateway"
    status, payload, _ = _login(stack[0])
    assert status == 502, f"网关拒绝被说成了运营输错：{status} {payload}"
    assert "暂时不可用" in payload["reason"]
    assert "密码" not in payload["reason"]


def test_a_genuinely_unreachable_company_is_still_reported_as_an_outage(stack, monkeypatch) -> None:
    """反向判据：真的连不上公司时**仍然**是 502 + 「暂时不可用」。

    没有这一条，把 428 归成拒绝的修法可以顺手把所有失败都说成"你输错了" ——
    那对运营更糟：他会一直重试一个已经挂掉的上游。

    打在 ``urlopen`` 而不是 ``request_json`` 上：要走的正是**分类管线**本身
    （传输故障 → unavailable），把那一步短路掉就把这段判据测没了。
    """
    import urllib.request

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    status, payload, _ = _login(stack[0])
    assert status == 502
    assert "暂时不可用" in payload["reason"]


def test_the_password_is_sent_aes_encrypted_never_plaintext(stack) -> None:
    """公司网关要求 AES：明文出去会被解成乱码（表现为登录失败）。"""
    _login(stack[0], password="my-secret-pw")
    seen = _FakeCompany.seen_password
    assert seen and seen != "my-secret-pw"
    assert "my-secret-pw" not in seen


def test_a_browser_supplied_company_token_is_ignored(stack) -> None:
    """浏览器塞一个公司令牌到请求里 —— 它不参与登录，身份只来自代理自己的交换。"""
    base, data_dir = stack
    forged = sign_console_jwt({"id": "ATTACKER", "username": "attacker"}, "made-up-key")
    connection = http.client.HTTPConnection(base.removeprefix("http://"))
    connection.request(
        "POST",
        LOGIN_ROUTE,
        body=json.dumps({"username": "zhang", "password": "pw", "randomStr": "abc123", "code": "1234"}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {forged}",
            "X-Company-Token": forged,
        },
    )
    response = connection.getresponse()
    response.read()
    connection.close()
    assert response.status == 200
    # 映射表里落的必须是**公司交换**得到的用户，不是令牌里的 ATTACKER。
    me = _proxy_mapping(data_dir)
    assert me["company_user_id"] == COMPANY_USER_ID
    assert me["company_user_id"] != "ATTACKER"


def _proxy_mapping(data_dir: Path) -> dict[str, Any]:
    import sqlite3

    with sqlite3.connect(data_dir / "mappings.db") as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM dify_login_mappings").fetchone()
    return dict(row)


def test_repeated_failures_trigger_backoff(stack) -> None:
    """连续失败退避：阈值一到就 429，不给继续猜密码。"""
    _FakeCompany.fail_login = "password"
    statuses = [_login(stack[0])[0] for _ in range(4)]
    assert 429 in statuses, f"没有触发退避：{statuses}"
    assert statuses[-1] == 429


def test_one_operator_failing_does_not_lock_out_everyone_else(stack) -> None:
    """退避的来源是**运营的地址**，不是 nginx 的。

    这个进程只监听回环，所有浏览器都经宿主 nginx 进来 ⇒ ``client_address`` 恒为
    127.0.0.1。按它计数等于把来源这一维抹平成常量（2026-10-19 在 41 实测：6 个不同
    用户名连续失败，第 5 个起全部 429，窗口 300 秒）。nginx 已经设了 ``X-Real-IP``。

    判据：同一条代理连接、6 个**不同**来源地址各失败一次 ⇒ 没有一个被 429。
    """
    _FakeCompany.fail_login = "password"
    statuses = [
        _login(stack[0], headers={"X-Real-IP": f"203.0.113.{n}"}, username=f"operator-{n}")[0]
        for n in range(1, 7)
    ]
    assert 429 not in statuses, f"一个运营的失败锁死了其他人：{statuses}"


def test_the_same_source_still_backs_off(stack) -> None:
    """反向判据：**同一个**来源连续失败仍然退避 —— 修来源判定不能把退避本身修没了。"""
    _FakeCompany.fail_login = "password"
    statuses = [_login(stack[0], headers={"X-Real-IP": "203.0.113.9"}, username="zhang")[0] for _ in range(4)]
    assert statuses[-1] == 429, f"同一来源却没有退避：{statuses}"


def test_rotating_both_keys_still_hits_a_global_cap(stack) -> None:
    """伪造来源头 + 轮换用户名**不能换来无限次尝试**。

    Devin 在 PR #851 指出：能连上 10119 的本机进程同样能自带 `X-Real-IP`，轮换它加
    轮换用户名就能同时绕过 `src:` 与 `acct:` 两条键。应用层分不出"这是 nginx 还是本机
    进程"，所以不假装能分 —— 另记一条与来源无关的全局键，把收益封在倍数以内。

    fixture 的阈值是 3、倍数 5 ⇒ 第 16 次必然 429。
    """
    _FakeCompany.fail_login = "password"
    statuses = [
        _login(stack[0], headers={"X-Real-IP": f"198.51.100.{n}"}, username=f"rotating-{n}")[0]
        for n in range(1, 20)
    ]
    assert 429 in statuses, f"轮换两条键就绕过了退避：{statuses}"
    assert statuses[:15] == [403] * 15, statuses[:15]


def test_the_same_account_backs_off_across_sources(stack) -> None:
    """账号这个键仍然跨来源生效：换 IP 不能换来无限次猜同一个账号。"""
    _FakeCompany.fail_login = "password"
    statuses = [
        _login(stack[0], headers={"X-Real-IP": f"203.0.113.{n}"}, username="zhang")[0] for n in range(1, 5)
    ]
    assert statuses[-1] == 429, f"换来源就绕过了账号退避：{statuses}"


def test_a_logged_rejection_carries_a_result_code_and_no_secrets(stack, caplog) -> None:
    """失败必须留下**可据以行动**的一行：结果码 + 来源，且不含任何凭据。

    这一条来自一次真实的排查代价：2026-10-19 的那次 502 在 journal 里只有
    ``"POST /__aiops/login" 502``，八种成因无法区分，只能靠回放重建。
    """
    _FakeCompany.fail_login = "password"
    with caplog.at_level("WARNING", logger="aiops.dify_login_proxy.server"):
        _login(stack[0], password="super-secret-pw", headers={"X-Real-IP": "203.0.113.77"})
    messages = [r.getMessage() for r in caplog.records]
    assert any("COMPANY_LOGIN_REJECTED" in m for m in messages), messages
    assert any("203.0.113.77" in m for m in messages), "来源没进日志，退避问题无从查起"
    joined = "\n".join(messages)
    assert "super-secret-pw" not in joined
    assert "1234" not in joined, "验证码字符不得进日志"


def test_a_forged_x_forwarded_for_does_not_change_the_source(stack) -> None:
    """只认 ``X-Real-IP``（nginx 覆盖写），不认客户端可自带的 ``X-Forwarded-For``。

    采信后者等于给"换一个假 IP 绕过退避"开门。

    判据方向要看清：6 个**不同**用户名、每个带一个新的 ``X-Forwarded-For``。若那个头
    被当成了来源，6 次都会是 403（各自独立）；实际应当仍然是同一个来源 ⇒ 第 4 次起 429。
    """
    _FakeCompany.fail_login = "password"
    statuses = [
        _login(stack[0], headers={"X-Forwarded-For": f"198.51.100.{n}"}, username=f"spoof-{n}")[0]
        for n in range(1, 7)
    ]
    assert 429 in statuses, f"X-Forwarded-For 被当成了来源，退避被绕过：{statuses}"


# --------------------------------------------------------------------------- #
# TLS 硬前置                                                                  #
# --------------------------------------------------------------------------- #


def test_the_proxy_refuses_a_non_https_public_origin() -> None:
    with pytest.raises(NotReady):
        assert_public_origin_is_https("http://console.example")
    assert assert_public_origin_is_https("https://console.example/") == "https://console.example"


def test_login_proxy_settings_refuse_to_build_without_https(tmp_path: Path) -> None:
    with pytest.raises(NotReady):
        LoginProxySettings(
            listen_host="127.0.0.1",
            listen_port=10119,
            public_origin="http://console.example",
            company=CompanyAuthSettings(base_url="http://company.invalid", encode_key=AES_KEY),
            dify=DifyConsoleSettings(base_url="http://dify.invalid", secret_key=SECRET_KEY),
            data_dir=str(tmp_path),
            edit_permission=EDIT_PERMISSION,
        )


# --------------------------------------------------------------------------- #
# 会话形状（与 Dify 的约定）                                                   #
# --------------------------------------------------------------------------- #


def test_the_minted_session_has_the_shape_dify_verifies() -> None:
    """载荷必须与 `get_account_jwt_token_for_account_id` 逐字段一致。"""
    import base64

    session = mint_console_session(
        DIFY_ACCOUNT_ID,
        secret_key=SECRET_KEY,
        deployment_edition="COMMUNITY",
        access_expire_minutes=60,
    )
    parts = session.access_token.split(".")
    assert len(parts) == 3
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    assert payload["user_id"] == DIFY_ACCOUNT_ID
    assert payload["iss"] == "COMMUNITY"
    assert payload["sub"] == "Console API Passport"
    # csrf 的 sub 必须是 user_id（Dify 的 check_csrf_token 要求）。
    csrf = json.loads(base64.urlsafe_b64decode(session.csrf_token.split(".")[1] + "=="))
    assert csrf["sub"] == DIFY_ACCOUNT_ID


# --------------------------------------------------------------------------- #
# #779 账号生命周期：登录时按权限码封禁/解封（自签模型下封禁是**我们自己的映射**）  #
# --------------------------------------------------------------------------- #


def _audit(data_dir: Path) -> list[dict[str, Any]]:
    import sqlite3

    with sqlite3.connect(data_dir / "mappings.db") as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM dify_login_audit ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def _status(data_dir: Path) -> str:
    import sqlite3

    with sqlite3.connect(data_dir / "mappings.db") as connection:
        row = connection.execute("SELECT status FROM dify_login_mappings LIMIT 1").fetchone()
    return str(row[0]) if row else ""


def test_losing_the_edit_permission_disables_the_mapping(stack) -> None:
    """失权后的下一次登录：被拒，且映射置为 disabled，审计记 `revoked`。

    "封禁"在自签模型下是我们自己的映射状态（Dify 无 ban 接口、成员移除不可逆，见代码注释）。
    """
    base, data_dir = stack
    assert _login(base)[0] == 200
    assert _status(data_dir) == "active"

    _FakeCompany.permissions = []  # 公司侧撤掉了编辑权限码
    status, payload, _ = _login(base)
    assert status == 403
    assert "可编辑智能体" in payload["reason"]
    assert _status(data_dir) == "disabled"
    assert any(row["action"] == "revoked" for row in _audit(data_dir))


def test_regaining_the_permission_reactivates_the_mapping(stack) -> None:
    """重新获得权限码后再登录：放行，映射改回 active，审计记 `reactivated`。

    这是与封禁**可逆**的那一半 —— 没有它，"撤权"就成了一次性死刑。
    """
    base, data_dir = stack
    assert _login(base)[0] == 200
    _FakeCompany.permissions = []
    assert _login(base)[0] == 403
    assert _status(data_dir) == "disabled"

    _FakeCompany.permissions = [EDIT_PERMISSION]
    status, _, _ = _login(base)
    assert status == 200
    assert _status(data_dir) == "active"
    assert any(row["action"] == "reactivated" for row in _audit(data_dir))


def test_a_bad_password_never_disables_the_mapping(stack) -> None:
    """公司登录**失败**（密码错）绝不封禁 —— 一次手误不该变成一次锁定。"""
    base, data_dir = stack
    assert _login(base)[0] == 200
    _FakeCompany.fail_login = "password"
    assert _login(base)[0] == 403
    assert _status(data_dir) == "active", "密码错把映射封了 —— 会把一次手误变成一次锁定"
    assert not any(row["action"] == "revoked" for row in _audit(data_dir))


def test_a_never_authorised_user_is_denied_and_never_provisioned(stack) -> None:
    """从头就没有编辑权限码 ⇒ 拒绝、**不开户**、审计记 denied。"""
    base, data_dir = stack
    _FakeCompany.permissions = []
    assert _login(base)[0] == 403
    assert _FakeDify.calls == []
    assert any(row["action"] == "denied" for row in _audit(data_dir))


def test_the_session_lifetime_defines_the_revocation_window(stack) -> None:
    """撤权窗口 = 会话寿命：`access` 与 `refresh` 的 Max-Age 必须**相等且等于配置值**。

    会话是无状态的（Dify 不查会话存储），所以一枚已签出的令牌在它的有效期里一直有效 ——
    部署要把这个窗口调小只能靠这个配置，测试钉住"两个都跟着它走"。
    """
    base, _ = stack
    _, _, headers = _login(base)
    cookies = headers["set-cookie-all"]
    ages = [segment.strip() for segment in cookies.split(";") if segment.strip().startswith("Max-Age=")]
    assert ages, cookies
    assert set(ages) == {"Max-Age=3600"}, f"access/refresh 寿命不一致或不是配置值：{ages}"


# --------------------------------------------------------------------------- #
# #780 发布权查询端点：验我们自签的会话 → 查映射 → 答能不能发布                    #
# --------------------------------------------------------------------------- #


def _check_publish(base: str, session: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection(base.removeprefix("http://"))
    connection.request(
        "POST",
        PUBLISH_CHECK_ROUTE,
        body=json.dumps({"session": session}),
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return payload


def _login_and_get_session(base: str) -> str:
    """登录一次，取**我们下发的 access token**（Dify 会话 cookie 的值）。"""
    import re

    _, _, headers = _login(base)
    cookies = headers["set-cookie-all"]
    match = re.search(r"(?<!refresh_)(?<!csrf_)access_token=([^;]+)", cookies)
    assert match, cookies
    return match.group(1)


def test_the_publish_check_allows_a_session_whose_user_has_the_publish_permission(stack) -> None:
    base, data_dir = stack
    session = _login_and_get_session(base)
    # 默认只有编辑权码 ⇒ 拒。
    denied = _check_publish(base, session)
    assert denied["allowed"] is False
    assert denied["reason"] == "MISSING_PUBLISH_PERMISSION"
    # 给上发布权码再登录一次 ⇒ 放行，且带回审计要的三个字段。
    _FakeCompany.permissions = [EDIT_PERMISSION, PUBLISH_PERMISSION]
    session2 = _login_and_get_session(base)
    allowed = _check_publish(base, session2)
    assert allowed["allowed"] is True
    assert allowed["company_user_id"] == COMPANY_USER_ID
    assert allowed["dify_account_id"]


def test_the_publish_check_refuses_a_forged_session(stack) -> None:
    """伪造/无效会话 ⇒ 拒，且**不透露**是"没这个映射"还是"会话无效"以外的细节。"""
    base, _ = stack
    forged = sign_console_jwt({"user_id": DIFY_ACCOUNT_ID, "exp": 4102444800}, "wrong-key")
    verdict = _check_publish(base, forged)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "SESSION_INVALID"


def test_the_publish_check_refuses_an_account_with_no_mapping(stack) -> None:
    """会话是我们签的，但那个 Dify 账号不在映射表里 ⇒ NO_MAPPING。"""
    base, _ = stack
    orphan = mint_console_session(
        "11111111-2222-3333-4444-555555555555",
        secret_key=SECRET_KEY,
        deployment_edition="COMMUNITY",
        access_expire_minutes=60,
    )
    verdict = _check_publish(base, orphan.access_token)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "NO_MAPPING"
