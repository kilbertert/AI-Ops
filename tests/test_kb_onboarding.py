"""`onboard-tenant`：1:1:1 的三个对象，一个幂等入口（#924 / PRD #923）。

四条判据，每条对着一种"看起来建好了、其实半成品"：

1. **重跑幂等**：已校验过的租户一条 kb-service 写都不发。
2. **半成品补齐**：有行但没校验过（上次模型校验失败）⇒ 重跑会补齐，而不是"已存在"就走。
3. **失败说出是哪一步**：模型校验失败时错误里带 `step=model-verify`，且**不写**校验时间戳。
4. **密钥不可用即拒绝**：一次真实的 embedding 调用，被 401 拒时开户失败 —— 这是 41 上
   那个"库建好了但永远解析失败"的形状。

凭据那一组单独一条：写进去的是密文，解出来的等于写进去的，且**明文不出现在存储值里**。

变异对照：把"已校验就跳过"换成"有行就跳过"，第 2 条变红；把 `verify_embedding` 换成
只读配置，第 4 条变红。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aiops_diagnostics.admin_cli import admin_app
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.kb_onboarding import (
    KbAdminClient,
    OnboardError,
    decrypt_credential,
    derive_email,
    encrypt_credential,
    onboard_tenant,
    rotate_credential,
    verify_embedding,
)

TENANT = "1942105476598861824"
ENVIRONMENT = "41"
CREDENTIAL_KEY = "0123456789abcdef"  # 16 bytes


class _KbService:
    """Just enough of kb-service: the two POSTs onboarding makes, and a record of them."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.register_status = 200
        self.repair_status = 200
        self.rotate_status = 200
        self.rotate_body: Any = None
        self.verify_matches = True
        self.base = ""

    def handler(self) -> type[BaseHTTPRequestHandler]:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                upstream.calls.append(
                    {"path": self.path, "tenant": self.headers.get("tenant-id"), "body": body}
                )
                if self.path == "/kb/tenants":
                    status = upstream.register_status
                    payload = {"data": {"biz_tenant_id": body.get("biz_tenant_id")}}
                elif self.path == "/kb/tenants/repair":
                    status = upstream.repair_status
                    payload = {"data": {"instances": ["maas"]}}
                elif self.path == "/kb/tenants/verify-password":
                    status = 200 if upstream.verify_matches else 401
                    payload = {"data": {"matches": upstream.verify_matches}}
                elif self.path == "/kb/tenants/rotate":
                    if upstream.rotate_body is not None:
                        status, payload = upstream.rotate_status, upstream.rotate_body
                    else:
                        status, payload = upstream.rotate_status, {"data": {"biz_tenant_id": TENANT}}
                else:
                    status, payload = 404, {"detail": "not found"}
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_: Any) -> None:
                pass

        return Handler

    def paths(self) -> list[str]:
        return [call["path"] for call in self.calls]


@pytest.fixture()
def kb_service() -> Any:
    upstream = _KbService()
    server = ThreadingHTTPServer(("127.0.0.1", 0), upstream.handler())
    upstream.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield upstream
    finally:
        server.shutdown()
        server.server_close()


def _store(tmp_path: Path) -> GatewayStore:
    return GatewayStore(tmp_path / "gateway.db")


def _workspace(state: str = "ready", workspace_id: str = "ws-1") -> Any:
    class _Outcome:
        pass

    outcome = _Outcome()
    outcome.state = state
    outcome.workspace_id = workspace_id
    outcome.note = ""
    return outcome


def test_credential_round_trips_and_never_stores_plaintext(tmp_path: Path) -> None:
    plaintext = "correct horse battery staple"
    stored = encrypt_credential(plaintext, CREDENTIAL_KEY)

    assert stored.startswith("enc:v1:")
    assert plaintext not in stored
    assert decrypt_credential(stored, CREDENTIAL_KEY) == plaintext

    # Two encryptions of the same secret differ (random IV): equal ciphertexts
    # would leak "these two tenants share a password".
    assert encrypt_credential(plaintext, CREDENTIAL_KEY) != stored


def test_derived_email_matches_the_shape_kb_service_lazily_registers() -> None:
    """A tenant registered before this command existed must not become a second account.

    kb-service derives `sha1(tenant_id)[:16]@kb.internal`; this is the observed
    value for the 1942 tenant on 36. If the two derivations drift, onboarding
    would build a *second* RAGFlow tenant and the existing libraries would
    vanish from that tenant's view without a single error.
    """
    assert derive_email(TENANT) == "83c9c0243bf50715@kb.internal"


def test_first_onboarding_registers_repairs_and_marks_verified(
    tmp_path: Path, kb_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aiops_diagnostics.kb_onboarding.verify_embedding", lambda **_k: 3072)
    store = _store(tmp_path)

    outcome = onboard_tenant(
        tenant_id=TENANT,
        environment=ENVIRONMENT,
        store=store,
        kb=KbAdminClient(kb_service.base),
        credential_key=CREDENTIAL_KEY,
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="sk-test",
        ensure_workspace=lambda: _workspace(),
    )

    assert outcome.model_verified is True
    assert outcome.workspace_state == "ready"
    assert kb_service.paths() == ["/kb/tenants", "/kb/tenants/repair"]
    assert all(call["tenant"] == TENANT for call in kb_service.calls[:1])
    register = kb_service.calls[0]["body"]
    assert register["biz_tenant_id"] == TENANT
    assert register["email"] == derive_email(TENANT)
    assert register["password"]  # a real credential was generated

    row = store.get_ragflow_tenant(TENANT, ENVIRONMENT)
    assert row is not None and row["model_checked_at"]
    assert row["credential_ciphertext"].startswith("enc:v1:")
    assert register["password"] not in row["credential_ciphertext"]
    assert decrypt_credential(row["credential_ciphertext"], CREDENTIAL_KEY) == register["password"]


def test_rerunning_is_idempotent_once_verified(
    tmp_path: Path, kb_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aiops_diagnostics.kb_onboarding.verify_embedding", lambda **_k: 3072)
    store = _store(tmp_path)
    kwargs = dict(
        tenant_id=TENANT,
        environment=ENVIRONMENT,
        store=store,
        kb=KbAdminClient(kb_service.base),
        credential_key=CREDENTIAL_KEY,
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="sk-test",
        ensure_workspace=lambda: _workspace(),
    )
    onboard_tenant(**kwargs)
    first = list(kb_service.calls)
    before = store.get_ragflow_tenant(TENANT, ENVIRONMENT)

    onboard_tenant(**kwargs)

    assert kb_service.calls == first, "a verified tenant must not be written again"
    after = store.get_ragflow_tenant(TENANT, ENVIRONMENT)
    assert after["credential_ciphertext"] == before["credential_ciphertext"]


def test_half_built_tenant_is_repaired_not_skipped(
    tmp_path: Path, kb_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row exists but the model was never proven: rerun must do the work.

    This is the shape a failed first attempt leaves behind. Treating "row
    exists" as "done" would leave that tenant permanently unable to parse.
    """
    store = _store(tmp_path)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("old", CREDENTIAL_KEY),
        model_checked=False,
    )
    monkeypatch.setattr("aiops_diagnostics.kb_onboarding.verify_embedding", lambda **_k: 3072)

    onboard_tenant(
        tenant_id=TENANT,
        environment=ENVIRONMENT,
        store=store,
        kb=KbAdminClient(kb_service.base),
        credential_key=CREDENTIAL_KEY,
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="sk-test",
        ensure_workspace=lambda: _workspace(),
    )

    assert "/kb/tenants/repair" in kb_service.paths()
    assert store.get_ragflow_tenant(TENANT, ENVIRONMENT)["model_checked_at"]


def test_model_check_failure_names_the_step_and_leaves_no_check_timestamp(
    tmp_path: Path, kb_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _rejected(**_kwargs: Any) -> int:
        raise OnboardError(
            "EMBEDDING_KEY_REJECTED", "https://embed.example/v1 rejected (…test): 401", step="model-verify"
        )

    monkeypatch.setattr("aiops_diagnostics.kb_onboarding.verify_embedding", _rejected)
    store = _store(tmp_path)

    with pytest.raises(OnboardError) as caught:
        onboard_tenant(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            store=store,
            kb=KbAdminClient(kb_service.base),
            credential_key=CREDENTIAL_KEY,
            embedding_base_url="https://embed.example/v1",
            embedding_api_key="sk-test",
            ensure_workspace=lambda: _workspace(),
        )

    assert caught.value.step == "model-verify"
    assert caught.value.code == "EMBEDDING_KEY_REJECTED"
    row = store.get_ragflow_tenant(TENANT, ENVIRONMENT)
    assert row is not None, "the account row is kept so the rerun can repair it"
    assert row["model_checked_at"] == "", "a failed check must not look like a completed onboarding"


def test_verify_embedding_rejects_a_blocked_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real call is the only check that catches a blocked key.

    On 41 a tenant had every model configured and an embedding key the provider
    had blocked; each parse failed with `401 API-key is blocked` and the console
    said only "解析失败". A config read would have called it healthy.
    """
    import urllib.error

    def _rejected(_spec: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError(
            "https://embed.example/v1/embeddings",
            401,
            "Unauthorized",
            {},
            __import__("io").BytesIO(b'{"error":{"code":"InvalidApiKey"}}'),
        )

    monkeypatch.setattr("aiops_diagnostics.bounded_http.open_response", _rejected)

    with pytest.raises(OnboardError) as caught:
        verify_embedding(base_url="https://embed.example/v1", api_key="sk-secret-1234")

    assert caught.value.code == "EMBEDDING_KEY_REJECTED"
    assert "1234" in caught.value.message  # the tail names which key
    assert "sk-secret" not in caught.value.message, "the key itself must never be echoed"


def test_rotate_refuses_a_credential_it_cannot_read(tmp_path: Path) -> None:
    """Rotating under a changed key would write a value nobody can decrypt."""
    store = _store(tmp_path)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("old", CREDENTIAL_KEY),
        model_checked=True,
    )

    with pytest.raises(OnboardError) as caught:
        rotate_credential(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            store=store,
            kb=KbAdminClient("http://127.0.0.1:1"),
            credential_key="ffffffffffffffff",
        )

    assert caught.value.code == "CREDENTIAL_UNREADABLE"


def test_rotate_replaces_the_stored_secret(tmp_path: Path, kb_service: Any) -> None:
    store = _store(tmp_path)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("old", CREDENTIAL_KEY),
        model_checked=True,
    )

    rotate_credential(
        tenant_id=TENANT,
        environment=ENVIRONMENT,
        store=store,
        kb=KbAdminClient(kb_service.base),
        credential_key=CREDENTIAL_KEY,
    )

    assert "/kb/tenants/rotate" in kb_service.paths(), "the upstream change must happen first"
    row = store.get_ragflow_tenant(TENANT, ENVIRONMENT)
    assert decrypt_credential(row["credential_ciphertext"], CREDENTIAL_KEY) != "old"
    assert row["model_checked_at"], "rotation is not a re-onboarding: the verified flag survives"


def test_rotation_writes_our_row_only_after_the_upstream_accepted(tmp_path: Path, kb_service: Any) -> None:
    """A failed upstream rotation must leave our stored password untouched.

    The failure shape this prevents: we write a new password, RAGFlow rejects the
    change, and the next login-proxy hop fails with "wrong password" while
    nothing on either side says why. So the order is upstream-first, and a
    refusal is a refusal.
    """
    store = _store(tmp_path)
    original = encrypt_credential("old", CREDENTIAL_KEY)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=original,
        model_checked=True,
    )
    kb_service.rotate_status = 500
    kb_service.verify_matches = False  # 探测也证明不了新密码生效

    with pytest.raises(OnboardError) as caught:
        rotate_credential(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            store=store,
            kb=KbAdminClient(kb_service.base),
            credential_key=CREDENTIAL_KEY,
        )

    assert caught.value.step == "credential-rotate"
    assert store.get_ragflow_tenant(TENANT, ENVIRONMENT)["credential_ciphertext"] == original


# ---- CLI 面：缺配置退 2、模型校验失败退 1 且带 step ----

TENANT = "1942105476598861824"


def _runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str) -> CliRunner:
    base = {
        "AIOPS_GATEWAY_ENVIRONMENT": "41",
        "AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY": "0123456789abcdef",
        "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_BASE_URL": "https://embed.example/v1",
        "AIOPS_GATEWAY_RAGFLOW_EMBEDDING_API_KEY": "sk-test",
        "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": "http://127.0.0.1:1",
        "AIOPS_GATEWAY_DATABASE_FILE": str(tmp_path / "gateway.db"),
    }
    base.update(overrides)
    for name, value in base.items():
        monkeypatch.setenv(name, value)
    return CliRunner()


def test_missing_credential_key_is_a_usage_error(monkeypatch, tmp_path):
    runner = _runner(monkeypatch, tmp_path, AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY="")
    result = runner.invoke(admin_app, ["onboard-tenant", "--tenant", TENANT, "--skip-workspace"])
    assert result.exit_code == 2
    assert "AIOPS_GATEWAY_RAGFLOW_CREDENTIAL_KEY" in result.output


def test_model_check_failure_exits_one_with_the_step(monkeypatch, tmp_path):
    from aiops_diagnostics.kb_onboarding import OnboardError

    runner = _runner(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "aiops_diagnostics.kb_onboarding.KbAdminClient.register_tenant",
        lambda self, **kwargs: None,
    )
    monkeypatch.setattr(
        "aiops_diagnostics.kb_onboarding.KbAdminClient.repair_models",
        lambda self, tenant_id: {"data": {}},
    )

    def _rejected(**_kwargs: Any) -> int:
        raise OnboardError("EMBEDDING_KEY_REJECTED", "rejected", step="model-verify")

    monkeypatch.setattr("aiops_diagnostics.kb_onboarding.verify_embedding", _rejected)

    result = runner.invoke(admin_app, ["onboard-tenant", "--tenant", TENANT, "--skip-workspace"])

    assert result.exit_code == 1
    payload = json.loads([line for line in result.output.splitlines() if line.startswith("{")][-1])
    assert payload["error"] == "EMBEDDING_KEY_REJECTED"
    assert payload["step"] == "model-verify"


def test_business_error_served_with_http_200_is_not_a_success(
    tmp_path: Path, kb_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 carrying a business error must fail the step, not pass silently.

    kb-service answers most business errors as HTTP 200 with an error object.
    ``request_json``'s default parser accepts any JSON object, so without an
    explicit success check a rejected rotation would look successful — and the
    caller would overwrite the password we store while the upstream still has
    the old one. The next login would then fail with "wrong password".
    """
    store = _store(tmp_path)
    original = encrypt_credential("old", CREDENTIAL_KEY)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=original,
        model_checked=True,
    )
    kb_service.rotate_body = {"code": 102, "message": "password change failed"}
    kb_service.verify_matches = False

    with pytest.raises(OnboardError) as caught:
        rotate_credential(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            store=store,
            kb=KbAdminClient(kb_service.base),
            credential_key=CREDENTIAL_KEY,
        )

    assert caught.value.code == "KB_BUSINESS_ERROR"
    assert caught.value.step == "credential-rotate"
    assert store.get_ragflow_tenant(TENANT, ENVIRONMENT)["credential_ciphertext"] == original


def test_a_bad_credential_key_fails_before_any_upstream_write(tmp_path: Path, kb_service: Any) -> None:
    """A key-length typo must not leave an upstream account whose password is gone.

    The failure shape this prevents: we register the account (upstream write),
    then encryption rejects the key, then the generated password exists only in a
    frame that is about to be discarded. Nothing can ever log in to that tenant
    again, and no row records why.
    """
    with pytest.raises(OnboardError) as caught:
        onboard_tenant(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            store=_store(tmp_path),
            kb=KbAdminClient(kb_service.base),
            credential_key="too-short",
            embedding_base_url="https://embed.example/v1",
            embedding_api_key="sk-test",
            ensure_workspace=lambda: _workspace(),
        )

    assert caught.value.code == "CREDENTIAL_KEY_INVALID"
    assert kb_service.calls == [], "no upstream write may happen before the key is known good"


def test_an_existing_upstream_account_is_rotated_not_registered(
    tmp_path: Path, kb_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kb-service 的 `create_tenant` 见到已有行就 `return dict(row)`，**不改密码**。

    所以"注册成功"是假的：我们记下一把上游不认的密码，直到第一次登录才炸
    （41 真机第三轮 `Email and password do not match!`）。已有账号的正确动作是
    **带上旧密码改密码** —— 只有这样才能保证"我们记的那把 = 上游那把"。
    """
    store = _store(tmp_path)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("old-password", CREDENTIAL_KEY),
        model_checked=False,
    )
    monkeypatch.setattr("aiops_diagnostics.kb_onboarding.verify_embedding", lambda **_k: 3072)

    onboard_tenant(
        tenant_id=TENANT,
        environment=ENVIRONMENT,
        store=store,
        kb=KbAdminClient(kb_service.base),
        credential_key=CREDENTIAL_KEY,
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="sk-test",
        ensure_workspace=lambda: _workspace(),
    )

    assert "/kb/tenants" not in kb_service.paths(), "已有账号不能再走注册（它不会改密码）"
    assert "/kb/tenants/rotate" in kb_service.paths()
    rotate = next(c for c in kb_service.calls if c["path"] == "/kb/tenants/rotate")
    assert rotate["body"]["old_password"] == "old-password", "必须用我们记的那把旧密码去改"
    row = store.get_ragflow_tenant(TENANT, ENVIRONMENT)
    assert decrypt_credential(row["credential_ciphertext"], CREDENTIAL_KEY) == rotate["body"]["new_password"]


def test_an_existing_account_whose_password_we_cannot_read_is_refused(
    tmp_path: Path, kb_service: Any
) -> None:
    """解不开旧密码就接管不了 —— 明确拒绝，不猜、不重注册。"""
    store = _store(tmp_path)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("old", "ffffffffffffffff"),
        model_checked=False,
    )

    with pytest.raises(OnboardError) as caught:
        onboard_tenant(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            store=store,
            kb=KbAdminClient(kb_service.base),
            credential_key=CREDENTIAL_KEY,
            embedding_base_url="https://embed.example/v1",
            embedding_api_key="sk-test",
            ensure_workspace=lambda: _workspace(),
        )

    assert caught.value.code == "RAGFLOW_CREDENTIAL_UNREADABLE"
    assert kb_service.calls == [], "接管不了就不要碰上游"


def test_rotation_records_the_new_password_when_the_upstream_changed_despite_an_error(
    tmp_path: Path, kb_service: Any
) -> None:
    """41 真机第五轮：改密成功、重建 token 失败 ⇒ 整条报 500 ⇒ 我们不回写。

    那一刻上游已经是新密码、我们还是旧的，而报错只说"轮换失败"。下次轮换会拿过期的
    旧密码去改，永远改不动。所以失败之后要**探一次**：新密码能登录吗？能就记下来。
    """
    store = _store(tmp_path)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=encrypt_credential("old", CREDENTIAL_KEY),
        model_checked=True,
    )
    kb_service.rotate_status = 500  # 改密之后的第二步失败
    kb_service.verify_matches = True  # 但新密码其实已经生效

    result = rotate_credential(
        tenant_id=TENANT,
        environment=ENVIRONMENT,
        store=store,
        kb=KbAdminClient(kb_service.base),
        credential_key=CREDENTIAL_KEY,
    )

    assert result["upstream_changed_despite_error"] is True
    row = store.get_ragflow_tenant(TENANT, ENVIRONMENT)
    assert decrypt_credential(row["credential_ciphertext"], CREDENTIAL_KEY) != "old"


def test_rotation_keeps_the_old_password_when_the_probe_cannot_prove_anything(
    tmp_path: Path, kb_service: Any
) -> None:
    """证明不了就维持原判 —— 不拿一个可能错的密码覆盖掉手里的那把。"""
    store = _store(tmp_path)
    original = encrypt_credential("old", CREDENTIAL_KEY)
    store.record_ragflow_tenant(
        TENANT,
        ENVIRONMENT,
        ragflow_email=derive_email(TENANT),
        credential_ciphertext=original,
        model_checked=True,
    )
    kb_service.rotate_status = 500
    kb_service.verify_matches = False

    with pytest.raises(OnboardError):
        rotate_credential(
            tenant_id=TENANT,
            environment=ENVIRONMENT,
            store=store,
            kb=KbAdminClient(kb_service.base),
            credential_key=CREDENTIAL_KEY,
        )

    assert store.get_ragflow_tenant(TENANT, ENVIRONMENT)["credential_ciphertext"] == original
