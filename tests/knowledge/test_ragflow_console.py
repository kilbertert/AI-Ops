"""RAGFlow 控制台代理（#926）：代表租户、不派发密码、拒绝可区分、账本记拒绝。

判据落在两件事上：**发给上游的登录请求里是哪个账号**（这条链路唯一有安全含义的事实），
以及**取不到会话时不把浏览器送过去**（送过去只会看到登录页，看起来像按钮没反应）。
"""

from __future__ import annotations

import json
import pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.knowledge.kb_onboarding import derive_email, encrypt_credential
from aiops_diagnostics.knowledge.ragflow_console import (
    LOGIN_REFUSED,
    NOT_ONBOARDED,
    UNREADABLE,
    RAGFlowLoginClient,
    RAGFlowLoginError,
    open_console,
)

TENANT = "1942105476598861824"
_PEM_CACHE: dict[str, str] = {}


def _pem(tmp_path) -> str:
    """生成一把**真实**的 RSA 公钥，让加密这条路径真的跑一遍。

    用假 PEM 会让"加密"变成"随便编一个字符串"，而真机上它是会 500 的那一步
    （RAGFlow 回 `Fail to crypt password`）。测试要覆盖的正是这个编码。
    """
    key = _PEM_CACHE.get("path")
    if key:
        return key
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = pathlib.Path(tmp_path) / "public.pem"
    path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    _PEM_CACHE["path"] = str(path)
    return str(path)


ENV = "41"
KEY = "0123456789abcdef"
OTHER_TENANT = "1783022023241633792"


class _FakeRAGFlow:
    """Only `/api/v1/auth/login`, and it records exactly what it was asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # Shaped like the real one (41, 2026-10-22): the session lives in a
        # `Set-Cookie` header; `data` carries only the user profile.
        self.response: Any = {"code": 0, "data": {"email": "x@kb.internal"}}
        self.set_cookie = "session=s-1; HttpOnly; Path=/"
        self.status = 200
        self.base = ""

    def handler(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                upstream.calls.append({"path": self.path, "body": body})
                raw = json.dumps(upstream.response).encode()
                self.send_response(upstream.status)
                if upstream.set_cookie:
                    self.send_header("Set-Cookie", upstream.set_cookie)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_: Any) -> None:
                pass

        return Handler


@pytest.fixture()
def ragflow() -> Any:
    upstream = _FakeRAGFlow()
    server = ThreadingHTTPServer(("127.0.0.1", 0), upstream.handler())
    upstream.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield upstream
    finally:
        server.shutdown()
        server.server_close()


def _store(tmp_path: Path, *, onboarded: bool = True, tenant: str = TENANT) -> GatewayStore:
    store = GatewayStore(tmp_path / "gateway.db")
    if onboarded:
        store.record_ragflow_tenant(
            tenant,
            ENV,
            ragflow_email=derive_email(tenant),
            credential_ciphertext=encrypt_credential("pw-1", KEY),
            model_checked=True,
        )
    return store


class _Caller:
    def __init__(self, tenant_id: str = TENANT) -> None:
        self.effective_tenant_id = tenant_id
        self.subject = type("S", (), {"c_user_id": "C-42"})()


def test_the_proxy_logs_in_as_the_callers_own_tenant(tmp_path: Path, ragflow: Any) -> None:
    store = _store(tmp_path)

    target, session = open_console(
        store=store,
        environment=ENV,
        caller=_Caller(),
        credential_key=KEY,
        client=RAGFlowLoginClient(ragflow.base),
        public_base_url="https://rag.example",
        public_pem_path=_pem(tmp_path),
    )

    assert target == "https://rag.example"
    assert ragflow.calls[0]["path"] == "/api/v1/auth/login"
    assert ragflow.calls[0]["body"]["email"] == derive_email(TENANT)
    sent = ragflow.calls[0]["body"]["password"]
    assert sent != "pw-1", "密码必须以密文发出，明文会被 RAGFlow 当成解不开"
    assert len(sent) > 100, "RSA 密文的 base64 远长于明文"
    assert session.cookies == {"session": "s-1"}


def test_a_tenant_that_was_never_onboarded_is_its_own_refusal(tmp_path: Path, ragflow: Any) -> None:
    """未开通 ≠ 无权限：前者引导去开户，后者拒绝。

    混成一个 403 会让运营去猜"是不是我没权限"，而实际是"这个租户还没开户"。
    """
    store = _store(tmp_path, onboarded=False)

    with pytest.raises(RAGFlowLoginError) as caught:
        open_console(
            store=store,
            environment=ENV,
            caller=_Caller(),
            credential_key=KEY,
            client=RAGFlowLoginClient(ragflow.base),
            public_base_url="https://rag.example",
            public_pem_path=_pem(tmp_path),
        )

    assert caught.value.code == NOT_ONBOARDED
    assert ragflow.calls == [], "an unonboarded tenant must not reach the upstream at all"


def test_a_rejected_login_is_a_refusal_not_an_empty_session(tmp_path: Path, ragflow: Any) -> None:
    """RAGFlow answers 200 with `code != 0` for an unknown account.

    Treating that as success would send the browser to a logged-out console — the
    shape that reads as "the button does nothing".
    """
    store = _store(tmp_path)
    ragflow.response = {"code": 109, "message": "Email: x is not registered!"}

    with pytest.raises(RAGFlowLoginError) as caught:
        open_console(
            store=store,
            environment=ENV,
            caller=_Caller(),
            credential_key=KEY,
            client=RAGFlowLoginClient(ragflow.base),
            public_base_url="https://rag.example",
            public_pem_path=_pem(tmp_path),
        )

    assert caught.value.code == LOGIN_REFUSED


def test_a_200_without_cookies_is_also_a_refusal(tmp_path: Path, ragflow: Any) -> None:
    store = _store(tmp_path)
    ragflow.set_cookie = ""

    with pytest.raises(RAGFlowLoginError) as caught:
        open_console(
            store=store,
            environment=ENV,
            caller=_Caller(),
            credential_key=KEY,
            client=RAGFlowLoginClient(ragflow.base),
            public_base_url="https://rag.example",
            public_pem_path=_pem(tmp_path),
        )

    assert caught.value.code == LOGIN_REFUSED


def test_one_tenants_onboarding_does_not_open_another(tmp_path: Path, ragflow: Any) -> None:
    """The tenant comes from the caller, so a tenant with no row cannot be entered.

    Structural, not a check that can be forgotten: `open_console` takes no tenant
    parameter at all.
    """
    store = _store(tmp_path, tenant=OTHER_TENANT)

    with pytest.raises(RAGFlowLoginError) as caught:
        open_console(
            store=store,
            environment=ENV,
            caller=_Caller(TENANT),
            credential_key=KEY,
            client=RAGFlowLoginClient(ragflow.base),
            public_base_url="https://rag.example",
            public_pem_path=_pem(tmp_path),
        )

    assert caught.value.code == NOT_ONBOARDED


def test_a_credential_encrypted_with_another_key_is_unreadable(tmp_path: Path, ragflow: Any) -> None:
    store = _store(tmp_path)

    with pytest.raises(Exception) as caught:
        open_console(
            store=store,
            environment=ENV,
            caller=_Caller(),
            credential_key="ffffffffffffffff",
            client=RAGFlowLoginClient(ragflow.base),
            public_base_url="https://rag.example",
            public_pem_path=_pem(tmp_path),
        )

    assert isinstance(caught.value, RAGFlowLoginError)
    assert caught.value.code == UNREADABLE, "an unreadable credential must not look like a login failure"


def test_the_ledger_records_refusals_too(tmp_path: Path) -> None:
    """With one shared owner, "who tried" is the only operator-level fact we keep."""
    store = _store(tmp_path)

    store.record_knowledge_access(
        tenant_id=TENANT,
        environment=ENV,
        outcome="refused",
        company_user_id="C-42",
        reason_code=NOT_ONBOARDED,
    )

    rows = store.knowledge_access_entries(tenant_id=TENANT)
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["company_user_id"] == "C-42"
    assert rows[0]["reason_code"] == NOT_ONBOARDED


# ---- 路由面：302 + cookie、四类可区分的拒绝、账本 ------------------------


def _gateway(tmp_path: Path, ragflow: Any, **overrides: Any):
    from fastapi.testclient import TestClient

    from aiops_diagnostics.conversation.faq import FAQCatalog, PlatformIdentityResolver
    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        environment=ENV,
        ragflow_credential_key=KEY,
        ragflow_console_base_url=ragflow.base if ragflow else "",
        ragflow_console_public_url="https://rag.example",
        ragflow_console_public_pem=_pem(tmp_path) if ragflow else "",
        **overrides,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    # The server config is a private file by contract (validate_private_file):
    # a world/group-readable one is refused at startup, and the test root needs to
    # satisfy the same rule the deployment does.
    settings.server_config_file.chmod(0o600)
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        caller_resolver=_CallerResolver(),
        order_authorizer=None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app), app


class _CallerResolver:
    def resolve(self, token: str, *, required_scope: str, **_: Any):
        from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

        subject = SubjectRecord(b_user_id="", c_user_id="C-42", tenant_id=TENANT)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=TENANT,
            data_scope=DataScope(type="self"),
            roles=frozenset({"ROLE_AGENT_ADMIN"}),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return ()

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def test_a_browser_navigation_gets_303_and_the_session_cookie(tmp_path: Path, ragflow: Any) -> None:
    """顶层导航（``Accept: text/html``）：303 + Set-Cookie，由浏览器自己跟随。"""
    from aiops_diagnostics.knowledge.kb_onboarding import derive_email

    client, _ = _gateway(tmp_path, ragflow)
    client.app.state.gateway.store.record_ragflow_tenant(  # type: ignore[attr-defined]
        TENANT,
        ENV,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("pw", KEY),
        model_checked=True,
    )

    response = client.get(
        "/v1/knowledge/console",
        headers={"Authorization": "Bearer tok", "Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://rag.example"
    assert response.cookies.get("session") == "s-1"


def test_the_page_gets_json_and_a_same_origin_cookie(tmp_path: Path, ragflow: Any) -> None:
    """页面脚本（fetch）：**200 + target**，cookie 种在这次同源应答上。

    这是「打开 RAGFlow」按钮唯一可行的形状：跨源重定向上的 Set-Cookie 浏览器不会保存
    （2026-10-22 真浏览器实测），而同源应答上的会 —— 只要 Domain 覆盖目标源。
    """
    from aiops_diagnostics.knowledge.kb_onboarding import derive_email

    client, _ = _gateway(tmp_path, ragflow)
    client.app.state.gateway.store.record_ragflow_tenant(  # type: ignore[attr-defined]
        TENANT,
        ENV,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("pw", KEY),
        model_checked=True,
    )

    response = client.get(
        "/v1/knowledge/console",
        headers={"Authorization": "Bearer tok", "Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["target"] == "https://rag.example"
    assert response.cookies.get("session") == "s-1"


def test_the_cookie_carries_the_configured_domain(tmp_path: Path, ragflow: Any) -> None:
    """没有 Domain 时 cookie 是 host-only，浏览器跳到 RAGFlow 的源时一条都不发。

    这一条钉住的是**真浏览器验收里唯一翻车的那处**：不是"有没有 cookie"，而是
    "浏览器会不会把它带到另一个源"。
    """
    from aiops_diagnostics.knowledge.kb_onboarding import derive_email

    client, _ = _gateway(tmp_path, ragflow, ragflow_console_cookie_domain="mall.qushiyun.com")
    client.app.state.gateway.store.record_ragflow_tenant(  # type: ignore[attr-defined]
        TENANT,
        ENV,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("pw", KEY),
        model_checked=True,
    )

    response = client.get(
        "/v1/knowledge/console",
        headers={"Authorization": "Bearer tok", "Accept": "application/json"},
    )

    cookie_header = response.headers.get("set-cookie", "")
    assert "Domain=mall.qushiyun.com" in cookie_header


def test_the_route_records_a_granted_hop(tmp_path: Path, ragflow: Any) -> None:
    from aiops_diagnostics.knowledge.kb_onboarding import derive_email

    client, _ = _gateway(tmp_path, ragflow)
    store = client.app.state.gateway.store  # type: ignore[attr-defined]
    store.record_ragflow_tenant(
        TENANT,
        ENV,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("pw", KEY),
        model_checked=True,
    )

    client.get("/v1/knowledge/console", headers={"Authorization": "Bearer tok"}, follow_redirects=False)

    rows = store.knowledge_access_entries(tenant_id=TENANT)
    assert rows[0]["outcome"] == "granted"
    assert rows[0]["company_user_id"] == "C-42"


def test_the_route_answers_409_when_the_tenant_was_never_onboarded(tmp_path: Path, ragflow: Any) -> None:
    client, _ = _gateway(tmp_path, ragflow)

    response = client.get("/v1/knowledge/console", headers={"Authorization": "Bearer tok"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == NOT_ONBOARDED


def test_the_route_is_absent_when_the_console_is_not_configured(tmp_path: Path) -> None:
    """No base URL ⇒ 404, never a fabricated redirect."""
    client, _ = _gateway(tmp_path, None)

    response = client.get("/v1/knowledge/console", headers={"Authorization": "Bearer tok"})

    assert response.status_code == 404


def test_from_env_actually_reads_the_console_settings(monkeypatch, tmp_path) -> None:
    """声明、路由、环境变量都在，但 `from_env()` 没赋值——真机上表现为"未启用"。

    这个缺口被所有单测漏掉，因为**它们都直接构造 settings**，绕过了唯一会读环境变量的
    那条路径。回归因此必须走 `from_env()`，而不能自己拼一个 `GatewayServerSettings`。
    """
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    monkeypatch.setenv("AIOPS_GATEWAY_RAGFLOW_CONSOLE_BASE_URL", "http://rag:10081")
    monkeypatch.setenv("AIOPS_GATEWAY_RAGFLOW_CONSOLE_PUBLIC_URL", "http://rag:10081")
    monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", "41")
    monkeypatch.setenv("AIOPS_GATEWAY_DATABASE_FILE", str(tmp_path / "gateway.db"))

    settings = GatewayServerSettings.from_env()

    assert settings.ragflow_console_base_url == "http://rag:10081"
    assert settings.ragflow_console_public_url == "http://rag:10081"


def test_every_ragflow_setting_is_wired_from_the_environment(monkeypatch, tmp_path) -> None:
    """A guard for the whole family, not just the pair that broke.

    "Declared but never read" is invisible to any test that builds the settings
    object itself, so it gets one test that builds it the way a deployment does.
    """
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    expected = {
        "AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY": "ragflow_credential_key",
        "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_BASE_URL": "ragflow_embedding_base_url",
        "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_API_KEY": "ragflow_embedding_api_key",
        "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_MODEL": "ragflow_embedding_model",
        "AIOPS_GATEWAY_RAGFLOW_CONSOLE_BASE_URL": "ragflow_console_base_url",
        "AIOPS_GATEWAY_RAGFLOW_CONSOLE_PUBLIC_URL": "ragflow_console_public_url",
        "AIOPS_GATEWAY_RAGFLOW_TENANT_DOMAIN": "ragflow_tenant_domain",
    }
    for name in expected:
        monkeypatch.setenv(name, f"value-{name.lower()}")
    monkeypatch.setenv("AIOPS_GATEWAY_ENVIRONMENT", "41")
    monkeypatch.setenv("AIOPS_GATEWAY_DATABASE_FILE", str(tmp_path / "gateway.db"))

    settings = GatewayServerSettings.from_env()

    missing = [name for name, attr in expected.items() if getattr(settings, attr) != f"value-{name.lower()}"]
    assert not missing, f"这些 RAGFlow 设置声明了但 from_env() 没有读：{missing}"
