"""运营用公司账号进 RAGFlow：代理**代表租户**，不派发密码（#926 / PRD #923）。

## 为什么必须代表租户

RAGFlow 的鉴权把请求里的租户固定成**登录者自己的 id**（`api_utils.add_tenant_id_to_kwargs`
里 `kwargs["tenant_id"] = current_user.id`）。成员邀请确实存在，但成员看的是**自己名下**
的数据；能看到团队库的只有少数走 `check_team_permission` 的路径。所以"多个运营共享一个
RAGFlow 租户"在这个产品里不存在 —— 能共享的只有**以 owner 身份换到的会话**。

因此代理拿的是开户时保管的**租户 owner 凭据**（#924），用它换 RAGFlow 会话，再把浏览器
送过去。运营不接触密码，也不需要在 RAGFlow 里有账号。

## 这个方案的代价，以及它落在哪

所有运营在 RAGFlow 里都是同一个 owner，**RAGFlow 那一侧分不出是谁建的库**。这个代价不是
被忽略，而是被搬到我们这一侧：每次跳转（成功与拒绝）都写一行 `knowledge_access_ledger`，
记公司用户、租户、目标 RAGFlow 账号、结果与原因码。与发布审计同一条思路 —— 授权在桥这
一侧完成，审计也在这一侧记。

## 三条硬约束

1. **租户只来自调用者身份**，请求里不接受任何租户参数。跨租户不是"被检查掉"，而是
   **结构上取不到**：本模块没有任何接受 tenant_id 入参的入口。
2. **未开户 ≠ 无权限**：前者引导去开户，后者拒绝。混成一个 403 会让运营去猜。
3. **懒创建在这里被禁止**：kb-service 的检索/repair 会为陌生租户自动注册，那会绕开
   开户的幂等与凭据保管。所以代理只在 `ragflow_tenants` **已经有行**时才动手。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    json_body,
    request_json,
)
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.kb_onboarding import decrypt_credential

#: 四类拒绝，各自可区分。运营看到的不是"跳转失败"。
NOT_ONBOARDED = "KNOWLEDGE_TENANT_NOT_ONBOARDED"
FORBIDDEN = "KNOWLEDGE_FORBIDDEN"
UNREADABLE = "KNOWLEDGE_CREDENTIAL_UNREADABLE"
UPSTREAM_DOWN = "KNOWLEDGE_UPSTREAM_UNAVAILABLE"
LOGIN_REFUSED = "KNOWLEDGE_LOGIN_REFUSED"


class RAGFlowLoginError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class LoginSession:
    tenant_id: str
    ragflow_email: str
    cookies: dict[str, str]

    def as_headers(self) -> dict[str, str]:
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}


class RAGFlowLoginClient:
    """换会话的那一次 HTTP 调用。只有一个方法，因为它只该做一件事。"""

    def __init__(self, base_url: str, *, timeout: float = 15.0) -> None:
        if not base_url.strip():
            raise ValueError("RAGFlow base URL is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def login(self, *, email: str, password_ciphertext: str) -> dict[str, str]:
        """POST /auth/login, return the session cookies.

        ``code != 0`` is a **refusal**, not an empty session: RAGFlow answers 200
        with ``{"code": 109, "message": "Email: ... is not registered!"}`` for a
        wrong account, and treating that as success would send the browser to a
        logged-out console — the shape that looks like "the button does nothing".
        """

        def _refused(failure: HttpFailure) -> Exception:
            return RAGFlowLoginError(LOGIN_REFUSED, f"RAGFlow rejected the login: {failure}")

        def _down(failure: HttpFailure) -> Exception:
            return RAGFlowLoginError(UPSTREAM_DOWN, f"RAGFlow is unreachable: {failure}")

        payload = request_json(
            RequestSpec(
                url=join_url(self.base_url, "/api/v1/auth/login"),
                method="POST",
                headers={"Content-Type": "application/json"},
                body=json_body({"email": email, "password": password_ciphertext}),
                timeout=self.timeout,
                follow_redirects=False,
            ),
            mapping=ErrorMapping(
                auth_rejected=_refused,
                http_error=_refused,
                unavailable=_down,
                invalid_body=_down,
                invalid_envelope=_down,
            ),
        )
        if not isinstance(payload, dict) or payload.get("code") not in (0, None):
            raise RAGFlowLoginError(LOGIN_REFUSED, f"RAGFlow rejected the login: {payload!r}"[:300])
        cookies = _session_cookies(payload)
        if not cookies:
            # 200 + code 0 但没有会话 cookie：把浏览器送过去只会看到登录页。
            raise RAGFlowLoginError(LOGIN_REFUSED, "RAGFlow answered 200 without a session")
        return cookies


def _session_cookies(payload: dict[str, Any]) -> dict[str, str]:
    """Pull session cookies out of the login response.

    RAGFlow 有两种形状：老版本把 cookie 放在响应体里，新版本靠 `Set-Cookie`。这里只认
    两者中**明确出现**的那个，取不到就返回空 —— 让调用方报"没有会话"，而不是伪造一个。
    """
    raw = payload.get("data")
    if isinstance(raw, dict):
        cookies = raw.get("cookies")
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items()}
    return {}


def open_console(
    *,
    store: GatewayStore,
    environment: str,
    caller: Any,
    credential_key: str,
    client: RAGFlowLoginClient,
    public_base_url: str,
) -> tuple[str, LoginSession]:
    """Resolve the caller's own tenant → its RAGFlow session → where to send the browser.

    **No tenant parameter.** ``caller.effective_tenant_id`` is the only source, so
    "which tenant can I enter" is decided by the session the browser already holds.
    """
    tenant_id = str(getattr(caller, "effective_tenant_id", "") or "")
    if not tenant_id:
        raise RAGFlowLoginError(FORBIDDEN, "the caller has no tenant")
    row = store.get_ragflow_tenant(tenant_id, environment)
    if row is None:
        raise RAGFlowLoginError(
            NOT_ONBOARDED, f"tenant {tenant_id} has no RAGFlow account yet; run onboard-tenant first"
        )
    ciphertext = str(row.get("credential_ciphertext") or "")
    try:
        password = decrypt_credential(ciphertext, credential_key)
    except Exception as exc:  # noqa: BLE001 - 解不开就是配置变了，给它自己的码
        # Wrong key, or a row written before the key was rotated. Either way the
        # password is not recoverable here, and "unreadable" is a different action
        # for the operator than "not onboarded".
        raise RAGFlowLoginError(
            UNREADABLE, "the stored RAGFlow credential cannot be decrypted with the current key"
        ) from exc
    email = str(row["ragflow_email"])
    cookies = client.login(email=email, password_ciphertext=password)
    return public_base_url, LoginSession(tenant_id=tenant_id, ragflow_email=email, cookies=cookies)


def record(
    store: GatewayStore,
    *,
    tenant_id: str,
    environment: str,
    outcome: str,
    caller: Any = None,
    ragflow_email: str = "",
    reason_code: str = "",
) -> None:
    """One ledger row. Separate from opening so refusals are recorded too."""
    store.record_knowledge_access(
        tenant_id=tenant_id,
        environment=environment,
        outcome=outcome,
        company_user_id=str(getattr(getattr(caller, "subject", None), "c_user_id", "") or ""),
        ragflow_email=ragflow_email,
        reason_code=reason_code,
    )


def describe_failure(error: RAGFlowLoginError) -> dict[str, Any]:
    """A refusal the operator can act on — never a bare 403.

    ``not onboarded`` says to run onboarding; ``forbidden`` says the caller may not
    enter this tenant. Collapsing them would make the operator guess which one it is.
    """
    return {"event": "ragflow_console_access_refused", "code": error.code, "message": error.message}
