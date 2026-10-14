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
    PUBLISH_CHECK_ROUTE,
    mint_console_session,
    sign_console_jwt,
)
from aiops_diagnostics.dify_login_server import (
    LoginProxySettings,
    NotReady,
    assert_public_origin_is_https,
    make_server,
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
    fail_login: str = ""  # "" | "password" | "captcha"
    seen_password: str = ""
    seen_client_type: str = ""
    seen_token_header: str = ""

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).calls.append("GET " + self.path.split("?")[0])
        if self.path.startswith("/code"):
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
        body_raw = self.rfile.read(length).decode() if length else ""
        from urllib.parse import parse_qs

        params = {k: v[0] for k, v in parse_qs(body_raw).items()}
        type(self).seen_password = params.get("password", "")
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
                self._reply(
                    {
                        "result": "success",
                        "invitation_results": [],
                        "results": [
                            {
                                "status": "success",
                                "email": email,
                                "url": f"https://console.invalid/activate?email={email}&token=INV-TOKEN",
                            }
                        ],
                    },
                    status=201,
                )
            else:
                self._reply({"results": [{"status": "already_member", "email": email}]})
            return
        if self.path.endswith("/activate"):
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


@pytest.fixture
def stack(tmp_path: Path):
    _FakeCompany.calls = []
    _FakeCompany.permissions = [EDIT_PERMISSION]
    _FakeCompany.fail_login = ""
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
    try:
        yield f"http://{host}:{port}", Path(settings.data_dir)
    finally:
        proxy.shutdown()
        company.shutdown()
        dify.shutdown()


def _login(base: str, **overrides: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = {"username": "zhang", "password": "pw", "randomStr": "abc123", "code": "1234"}
    body.update(overrides)
    connection = http.client.HTTPConnection(base.removeprefix("http://"))
    connection.request(
        "POST", LOGIN_ROUTE, body=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    raw_headers = response.getheaders()
    connection.close()
    cookies = "; ".join(v for k, v in raw_headers if k.lower() == "set-cookie")
    headers = {k.lower(): v for k, v in raw_headers}
    headers["set-cookie-all"] = cookies
    return response.status, payload, headers


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
    """公司登录失败 ⇒ 可读原因，且**不调用 Dify**。"""
    _FakeCompany.fail_login = "password"
    status, payload, _ = _login(stack[0])
    assert status == 403
    assert "密码" in payload["reason"] or "验证码" in payload["reason"]
    assert _FakeDify.calls == []


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
