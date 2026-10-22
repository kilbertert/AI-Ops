"""``aiops admin onboard-tenant``：一次把 1:1:1 的三个对象建齐（#924 / PRD #923）。

## 三个对象，一个入口

```
公司租户  ──1:1──  Dify 租户工作区      （已由 ensure_workspace 实现）
          ──1:1──  RAGFlow 租户         （本模块：注册 + 凭据保管 + 模型校验）
```

今天它们的诞生时刻不同：workspace 由 ``ensure-workspace`` 建；RAGFlow 租户要等**第一次
检索被访问**才在 kb-service 里懒注册；模型实例的初始化失败会被吞掉，界面上看不出任何
异常。结果是「库建好了但永远解析失败」——41 上就撞到过一把被禁用的 embedding 密钥，
RAGFlow 只给一行 401，运营不知道该找谁。

本模块把三件事收进一个幂等命令，并且**失败要说出是哪一步、哪一把钥匙**。

## 为什么凭据存在我们这一侧

kb-service 建租户时生成随机密码但**不留**（`tenancy.py` 只存 token）。要支持"运营用公司
账号进 RAGFlow"（#926 的登录代理），就必须有一个可重复使用的密码。于是它落进
``ragflow_tenants`` 表，**密文**存储，密钥来自部署环境。

明文密码在任何时候都不写库、不进日志、不进命令输出。轮换是一条独立命令
（见 :func:`rotate_credential`），轮换后旧密码立即失效。

## 幂等位怎么表达

``ragflow_tenants.model_checked_at`` 为空 = 从没校验成功过。开户时模型校验失败**不写**
这一列，所以"有行但没校验过"就是半成品，重跑开户会去补齐而不是跳过。这是本模块唯一
的完成判据 —— 不是"行存在"，而是"行存在且校验过"。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    json_body,
    request_json,
    tenant_id_header,
)
from aiops_diagnostics.gateway_store import GatewayStore

#: kb-service 的租户头（与检索、媒体同一条）。
CREDENTIAL_PREFIX = "enc:v1:"
DEFAULT_TENANT_DOMAIN = "kb.internal"


class OnboardError(RuntimeError):
    """开户没有完成。``code`` 稳定、非本地化；``step`` 指到失败的那一步。"""

    def __init__(self, code: str, message: str, *, step: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.step = step


@dataclass(frozen=True, slots=True)
class OnboardOutcome:
    tenant_id: str
    environment: str
    ragflow_email: str
    workspace_state: str
    workspace_id: str
    model_verified: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "environment": self.environment,
            "ragflow_email": self.ragflow_email,
            "workspace_state": self.workspace_state,
            "workspace_id": self.workspace_id,
            "model_verified": self.model_verified,
            "note": self.note,
        }


# ── 凭据：派生、加密、解密 ────────────────────────────────────────────────


def derive_email(tenant_id: str, *, domain: str = DEFAULT_TENANT_DOMAIN) -> str:
    """The stable RAGFlow login for one company tenant.

    Same shape kb-service's lazy registration uses (sha1 of the tenant id, first
    16 hex chars). Deriving rather than storing the name means a tenant that was
    lazily registered before this command existed still lines up with the row
    this command would create — otherwise onboarding would build a *second*
    RAGFlow tenant for the same company tenant, and the old libraries would
    silently disappear from that tenant's view.
    """
    local = hashlib.sha1(tenant_id.encode()).hexdigest()[:16]
    return f"{local}@{domain}"


def generate_password() -> str:
    """A fresh credential. ``token_urlsafe`` — URL-safe, no shell metacharacters."""
    return secrets.token_urlsafe(24)


def encrypt_credential(plaintext: str, key: str) -> str:
    """AES/CBC with an explicit IV, ``enc:v1:`` prefix, base64 payload.

    IV is random per call and prepended to the ciphertext: a fixed IV would make
    two tenants with the same password produce the same ciphertext, which leaks
    "these two share a secret". The key comes from the deployment environment and
    is never stored beside the ciphertext.

    Length is checked here rather than at use time: a wrong-length key fails at
    the point of writing, where the error names the real problem.
    """
    raw = key.encode("utf-8")
    if len(raw) not in (16, 24, 32):
        raise ValueError("credential key must be 16/24/32 bytes")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError as exc:  # pragma: no cover - 依赖缺失是部署错误
        raise RuntimeError("cryptography is not installed; cannot store a credential") from exc
    iv = os.urandom(16)
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(raw), modes.CBC(iv)).encryptor()
    blob = iv + encryptor.update(padded) + encryptor.finalize()
    return CREDENTIAL_PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_credential(stored: str, key: str) -> str:
    """The inverse of :func:`encrypt_credential`. Refuses anything not ours."""
    if not stored.startswith(CREDENTIAL_PREFIX):
        raise ValueError("credential is not in the expected envelope")
    raw = key.encode("utf-8")
    if len(raw) not in (16, 24, 32):
        raise ValueError("credential key must be 16/24/32 bytes")
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("cryptography is not installed; cannot read a credential") from exc
    blob = base64.b64decode(stored[len(CREDENTIAL_PREFIX) :])
    iv, body = blob[:16], blob[16:]
    decryptor = Cipher(algorithms.AES(raw), modes.CBC(iv)).decryptor()
    padded = decryptor.update(body) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded).decode("utf-8") + unpadder.finalize().decode("utf-8")


# ── kb-service 的三步 ────────────────────────────────────────────────────


class KbAdminClient:
    """The three kb-service calls onboarding needs, and nothing else.

    Kept separate from ``KbServiceClient`` on purpose: that one is the runtime's
    read path (search, media, listing) and is bound to the neutral tenant. This
    one administers *other* tenants and therefore carries an explicit tenant
    header per call — mixing the two would put a write verb on the client the
    query path holds.
    """

    def __init__(self, base_url: str, *, service_token: str = "", timeout: float = 30):
        if not base_url.strip():
            raise ValueError("kb-service base URL is required")
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token.strip()
        self.timeout = timeout

    def _headers(self, tenant_id: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **tenant_id_header(tenant_id)}
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        return headers

    def _error_mapping(self, step: str) -> ErrorMapping:
        """Every kb-service failure collapses onto "this step failed".

        kb-service is not a multi-status API: it answers ``{"code", "message"}``
        with HTTP 200 for most business errors, and 4xx/5xx only for transport-
        level problems. Distinguishing them here would invent a taxonomy the
        upstream does not have; what the caller needs is *which step* died and
        what the upstream said, and that is what the raised error carries.
        """

        def _failed(failure: HttpFailure) -> Exception:
            return OnboardError("KB_REGISTER_FAILED", f"kb-service {step} failed: {failure}", step=step)

        return ErrorMapping(
            auth_rejected=_failed,
            http_error=_failed,
            unavailable=_failed,
            invalid_body=_failed,
            invalid_envelope=_failed,
        )

    def _post(self, path: str, tenant_id: str, payload: dict[str, Any], *, step: str) -> Any:
        """POST and **require a business-success envelope**.

        ``request_json``'s default parser accepts any JSON object, so an HTTP 200
        carrying a business error would come back as success. For a rotation that
        is the worst possible shape: the upstream password never changed, but the
        caller would go on to overwrite the one we store, and the next login
        would fail with "wrong password" while nothing says why. So this checks
        for the success marker explicitly, and an answer without one is a
        failure of *this step* — never a silent pass.
        """
        try:
            payload_out = request_json(
                RequestSpec(
                    url=join_url(self.base_url, path),
                    method="POST",
                    headers=self._headers(tenant_id),
                    body=json_body(payload),
                    timeout=self.timeout,
                ),
                mapping=self._error_mapping(step),
            )
        except OnboardError:
            raise
        except Exception as exc:  # noqa: BLE001 - 归成一步的失败，交给上层报
            raise OnboardError("KB_REGISTER_FAILED", f"kb-service {path} failed: {exc}", step=step) from exc
        if not isinstance(payload_out, Mapping) or "data" not in payload_out:
            # Every kb-service success carries `data` (register/repair/rotate all
            # return `{"data": ...}`). Anything else — including an error object
            # served with HTTP 200 — is this step failing, not a silent pass.
            raise OnboardError(
                "KB_BUSINESS_ERROR",
                f"kb-service {path} did not answer with a business-success object: {payload_out!r}",
                step=step,
            )
        return payload_out

    def register_tenant(self, *, tenant_id: str, email: str, password: str) -> None:
        self._post(
            "/kb/tenants",
            tenant_id,
            {"biz_tenant_id": tenant_id, "email": email, "password": password, "nickname": f"kb-{tenant_id}"},
            step="ragflow-tenant",
        )

    def repair_models(self, tenant_id: str) -> dict[str, Any]:
        """Idempotent model setup. Re-running is how a half-built tenant heals."""
        result = self._post("/kb/tenants/repair", tenant_id, {}, step="model-instance")
        return result if isinstance(result, dict) else {"data": result}

    def verify_login(self, *, tenant_id: str, password: str) -> None:
        """用给定密码试着登录一次，只用来回答"上游现在认哪把"。

        走 kb-service 的 rotate 端点会改密码，所以这里需要一个**只读**的验证入口。
        没有就明说：把它当成"证明不了"，而不是当成"成功"。
        """
        self._post(
            "/kb/tenants/verify-password",
            tenant_id,
            {"password": password},
            step="credential-verify",
        )

    def rotate_credential(self, *, tenant_id: str, old_password: str, new_password: str) -> None:
        """Change the upstream password. **Must** succeed before we store the new one."""
        self._post(
            "/kb/tenants/rotate",
            tenant_id,
            {"old_password": old_password, "new_password": new_password},
            step="credential-rotate",
        )


# ── 模型可用性：一次真实的 embedding 调用 ───────────────────────────────


def verify_embedding(*, base_url: str, api_key: str, model: str = "text-embedding-3-large") -> int:
    """One real embedding call. Returns the vector dimension.

    **Why a real call and not a config read**: on 41 a tenant was fully
    configured with an embedding key that the provider had blocked. Every
    document parse failed with ``401 API-key is blocked``, and RAGFlow showed
    that as "解析失败" with no hint that the key was the problem. A config check
    would have called that tenant healthy.

    Raises :class:`OnboardError` naming the endpoint and the key's last four
    characters — never the key itself.

    Goes through the shared transport skeleton rather than building its own
    ``urllib`` request: `tests/test_transport_convergence.py` exists because a
    client with a private request builder is a client nothing constrains
    (timeouts, redirect policy, body caps).
    """
    if not base_url or not api_key:
        raise OnboardError(
            "EMBEDDING_KEY_MISSING",
            "embedding endpoint and key are both required to verify a tenant",
            step="model-verify",
        )
    url = base_url.rstrip("/") + "/embeddings"
    tail = api_key[-4:] if len(api_key) >= 4 else "****"

    def _rejected(failure: HttpFailure) -> Exception:
        return OnboardError(
            "EMBEDDING_KEY_REJECTED",
            f"{url} rejected the embedding key (…{tail}): {failure}",
            step="model-verify",
        )

    def _unreachable(failure: HttpFailure) -> Exception:
        return OnboardError(
            "EMBEDDING_ENDPOINT_UNREACHABLE", f"{url} is unreachable: {failure}", step="model-verify"
        )

    try:
        payload = request_json(
            RequestSpec(
                url=url,
                method="POST",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                body=json_body({"model": model, "input": ["onboarding probe"]}),
                timeout=30,
            ),
            mapping=ErrorMapping(
                auth_rejected=_rejected,
                http_error=_rejected,
                unavailable=_unreachable,
                invalid_body=_rejected,
                invalid_envelope=_rejected,
            ),
        )
    except OnboardError:
        raise
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise OnboardError(
            "EMBEDDING_RESPONSE_MALFORMED",
            f"{url} answered 200 without an embedding vector",
            step="model-verify",
        )
    vector = data[0].get("embedding")
    if not isinstance(vector, list) or not vector:
        raise OnboardError(
            "EMBEDDING_RESPONSE_MALFORMED",
            f"{url} answered without a usable vector",
            step="model-verify",
        )
    return len(vector)


# ── 编排 ────────────────────────────────────────────────────────────────


def onboard_tenant(
    *,
    tenant_id: str,
    environment: str,
    store: GatewayStore,
    kb: KbAdminClient,
    credential_key: str,
    embedding_base_url: str,
    embedding_api_key: str,
    embedding_model: str = "text-embedding-3-large",
    tenant_domain: str = DEFAULT_TENANT_DOMAIN,
    ensure_workspace: Any = None,
    log: Any = None,
) -> OnboardOutcome:
    """Idempotently bring up the three objects for one tenant.

    ``ensure_workspace`` is injected rather than imported: it needs three console
    credentials this module has no business knowing about, and tests need to
    drive the failure path without a Dify console. Its result must expose
    ``state`` / ``workspace_id`` / ``note``.
    """
    say = log or (lambda _line: None)
    email = derive_email(tenant_id, domain=tenant_domain)
    # 密钥长度错是**配置**错误：在任何上游写入之前就拒掉，而不是等到加密时才炸
    # （那时账号已经建好、密码已经丢了）。评审 P2。
    if len(credential_key.encode("utf-8")) not in (16, 24, 32):
        raise OnboardError(
            "CREDENTIAL_KEY_INVALID",
            "AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY must be 16/24/32 bytes",
            step="credential-key",
        )

    existing = store.get_ragflow_tenant(tenant_id, environment)
    already_checked = bool(existing and str(existing.get("model_checked_at") or ""))

    # 1) Dify workspace（空壳；内容属于别的票）
    workspace_state, workspace_id, workspace_note = "skipped", "", ""
    if ensure_workspace is not None:
        outcome = ensure_workspace()
        workspace_state = str(getattr(outcome, "state", "unknown"))
        workspace_id = str(getattr(outcome, "workspace_id", ""))
        workspace_note = str(getattr(outcome, "note", ""))
        say(f"workspace: {workspace_state} {workspace_id}".rstrip())

    # 2) RAGFlow 租户：已校验过就**不重发**，半成品则补齐
    password: str | None = None
    if already_checked:
        say("ragflow tenant: already verified, no writes")
        return OnboardOutcome(
            tenant_id=tenant_id,
            environment=environment,
            ragflow_email=email,
            workspace_state=workspace_state,
            workspace_id=workspace_id,
            model_verified=True,
            note=workspace_note,
        )
    password = generate_password()
    # **先加密，再注册**（评审 P2）。反过来的话，密钥长度写错会在上游已经建好账号之后
    # 才抛错：那个账号的密码只在这一帧里存在过，丢了就找不回来 —— 留下一个谁都进不去的
    # 租户。加密先失败则上游一个字节都没写。
    ciphertext = encrypt_credential(password, credential_key)
    # **这条租户在上游已经有账号**时不能走"注册"：kb-service 的
    # `tenancy.create_tenant` 见到已有行就 `return dict(row)`，**不改密码**。于是
    # 「注册成功」是假的 —— 我们记下了一把上游根本不认的密码，要等到第一次登录才炸
    # （41 真机第三轮：`Email and password do not match!`）。
    #
    # 已有账号的正确动作是**改密码**：带上旧密码走 rotate，回到"我们记的那把 = 上游那把"。
    # 旧密码从我们自己的行里取；取不到就无法安全接管，明确拒绝并让人处理，而不是猜。
    if existing is not None:
        if str(existing.get("ragflow_email") or "") not in ("", email):
            raise OnboardError(
                "RAGFLOW_TENANT_EMAIL_CONFLICT",
                f"tenant {tenant_id} already has a RAGFlow account under a different login",
                step="ragflow-tenant",
            )
        try:
            old_password = decrypt_credential(str(existing["credential_ciphertext"]), credential_key)
        except Exception as exc:  # noqa: BLE001 - 解不开就接管不了，别猜
            raise OnboardError(
                "RAGFLOW_CREDENTIAL_UNREADABLE",
                "this tenant already has a RAGFlow account, but the stored password cannot be "
                "decrypted with the current key; restore the key (or rotate manually) first",
                step="ragflow-tenant",
            ) from exc
        kb.rotate_credential(tenant_id=tenant_id, old_password=old_password, new_password=password)
        say(f"ragflow tenant: password re-established for existing account {email}")
    else:
        kb.register_tenant(tenant_id=tenant_id, email=email, password=password)
        say(f"ragflow tenant: registered {email}")
    store.record_ragflow_tenant(
        tenant_id,
        environment,
        ragflow_email=email,
        credential_ciphertext=ciphertext,
        model_checked=False,
    )

    # 3) 模型实例 + 一次真实校验
    kb.repair_models(tenant_id)
    dimension = verify_embedding(
        base_url=embedding_base_url, api_key=embedding_api_key, model=embedding_model
    )
    say(f"model: embedding ok, dim={dimension}")

    stored = store.get_ragflow_tenant(tenant_id, environment) or {}
    store.record_ragflow_tenant(
        tenant_id,
        environment,
        ragflow_email=email,
        credential_ciphertext=str(stored.get("credential_ciphertext") or ""),
        model_checked=True,
    )

    return OnboardOutcome(
        tenant_id=tenant_id,
        environment=environment,
        ragflow_email=email,
        workspace_state=workspace_state,
        workspace_id=workspace_id,
        model_verified=True,
        note=workspace_note,
    )


def rotate_credential(
    *,
    tenant_id: str,
    environment: str,
    store: GatewayStore,
    kb: KbAdminClient,
    credential_key: str,
    log: Any = None,
) -> dict[str, Any]:
    """Replace one tenant's RAGFlow password. Old password stops working.

    Two writes, in this order, and the order is the point:

    1. **RAGFlow first** (via kb-service's rotate endpoint, which logs in with the
       old password, calls ``PATCH /users/me``, then rebuilds the API token —
       RAGFlow invalidates ``access_token`` on a password change, so skipping the
       rebuild would leave this tenant unable to retrieve at all, surfacing as a
       401 that looks like "wrong credential").
    2. **Our row second**, only after the upstream accepted the change.

    Writing our row first would mean a failed rotation leaves us holding a
    password nothing accepts — the next login proxy hop would then fail with no
    trace of why. Deliberately a separate command: onboarding must stay
    idempotent ("running it again changes nothing"), so rotation cannot be its
    side effect.
    """
    say = log or (lambda _line: None)
    row = store.get_ragflow_tenant(tenant_id, environment)
    if row is None:
        raise OnboardError(
            "RAGFLOW_TENANT_NOT_ONBOARDED",
            f"tenant {tenant_id} has no RAGFlow account in {environment}; run onboard first",
            step="rotate",
        )
    email = str(row["ragflow_email"])
    try:
        old_password = decrypt_credential(str(row["credential_ciphertext"]), credential_key)
    except Exception as exc:  # noqa: BLE001 - 解不开说明密钥变了，轮换会写进一把读不了的值
        raise OnboardError(
            "CREDENTIAL_UNREADABLE",
            "the stored credential cannot be decrypted with the current key; restore the key before rotating",
            step="rotate",
        ) from exc
    new_password = generate_password()
    try:
        kb.rotate_credential(tenant_id=tenant_id, old_password=old_password, new_password=new_password)
    except OnboardError:
        # **上游可能已经改了密码，只是后面那一步失败**（41 真机第五轮实测：
        # 改密成功、重建 API token 因 `code=103` 失败 → 整条报 500 → 我们不回写）。
        # 那一刻我们手里的旧密码就不再是上游那把了，而失败信息只说"轮换失败"。
        #
        # 所以失败之后**试探一次**：新密码能不能登录？能 ⇒ 改密其实成功了，把它记下来。
        # 这不是"忽略错误"，是把"上游到底处于哪个状态"问清楚再决定 —— 否则下一次轮换
        # 会拿着过期的旧密码去改，永远改不动。
        if _already_rotated(kb, tenant_id=tenant_id, password=new_password):
            say(f"rotation reported failure but the new credential works; recording it for {email}")
            store.record_ragflow_tenant(
                tenant_id,
                environment,
                ragflow_email=email,
                credential_ciphertext=encrypt_credential(new_password, credential_key),
                model_checked=bool(str(row.get("model_checked_at") or "")),
            )
            return {
                "tenant_id": tenant_id,
                "environment": environment,
                "ragflow_email": email,
                "upstream_changed_despite_error": True,
            }
        raise
    store.record_ragflow_tenant(
        tenant_id,
        environment,
        ragflow_email=email,
        credential_ciphertext=encrypt_credential(new_password, credential_key),
        model_checked=bool(str(row.get("model_checked_at") or "")),
    )
    say(f"rotated credential for {email}")
    return {"tenant_id": tenant_id, "environment": environment, "ragflow_email": email}


def _already_rotated(kb: KbAdminClient, *, tenant_id: str, password: str) -> bool:
    """Did the upstream actually accept the new password?

    Asks kb-service to log in with it. Only a clean success counts — any other
    answer (including "kb-service unreachable") means we cannot prove the new
    password works, so we keep the original failure rather than overwrite our row
    with something that might be wrong.
    """
    try:
        kb.verify_login(tenant_id=tenant_id, password=password)
    except Exception:  # noqa: BLE001 - 证明不了就维持原判
        return False
    return True


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"
