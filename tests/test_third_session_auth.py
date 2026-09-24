from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from aiops_diagnostics.caller_auth import CALLER_AUTH_INVALID, CallerAuthError
from aiops_diagnostics.config import UpmsSettings
from aiops_diagnostics.query_scope import QueryScope, resolve_query_scope
from aiops_diagnostics.scope_context import (
    C_MAPPING_AMBIGUOUS,
    C_MAPPING_FAILED,
    C_MAPPING_NOT_CONFIGURED,
    C_MAPPING_NOT_FOUND,
    C_MAPPING_TENANT_MISMATCH,
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    DataScope,
    ScopeError,
    SubjectRecord,
)
from aiops_diagnostics.third_session_auth import (
    RedisThirdSessionResolver,
    ThirdSessionSettings,
    UpmsBSubjectDirectory,
)

UPMS_BASE_URL = "https://upms.example.test"
INSIDE_CREDENTIAL = "upms-internal-token"
SESSION_TOKEN = "session-123456789012345"


class _Redis:
    def __init__(self, value: str | None):
        self.value = value.encode() if isinstance(value, str) else value

    def get(self, key: str):
        assert key == "app:3rd_session:session-123456789012345"
        return self.value


def _settings() -> ThirdSessionSettings:
    return ThirdSessionSettings(
        "127.0.0.1", 6379, 0, password="secret", service_token="svc", key_prefix="app:3rd_session:"
    )


def _redis_session(monkeypatch: pytest.MonkeyPatch, payload: str | bytes | None) -> None:
    monkeypatch.setattr(
        "aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: _Redis(payload)
    )


def _java_session(payload: dict[str, Any]) -> bytes:
    """A Redis value as the Java BFF stores it: header + embedded JSON string."""
    return b"\xac\xed\x00\x05t\x00" + json.dumps(payload).encode()


def test_resolves_existing_third_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _redis_session(monkeypatch, _java_session({"userId": "C-1", "tenantId": "T-1"}))
    context = RedisThirdSessionResolver(_settings()).resolve(
        "svc", required_scope="aiops:orders:read", third_session=SESSION_TOKEN
    )
    assert context.subject.c_user_id == "C-1"
    assert context.effective_tenant_id == "T-1"
    assert context.data_scope.type == "self"


@pytest.mark.parametrize("value", [None, "not-json", json.dumps({"userId": "C-1"})])
def test_missing_or_invalid_session_fails_closed(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    _redis_session(monkeypatch, value)
    with pytest.raises(CallerAuthError) as excinfo:
        RedisThirdSessionResolver(_settings()).resolve(
            "svc", required_scope="aiops:orders:read", third_session=SESSION_TOKEN
        )
    assert excinfo.value.code == CALLER_AUTH_INVALID


def test_resolves_login_pointer_to_session_object(monkeypatch: pytest.MonkeyPatch) -> None:
    pointer = b"\xac\xed\x00\x05t\x00Kapp:3rd_session:wx:wx-1:uuid"
    session = _java_session({"userId": "C-1", "tenantId": "T-1"})

    class Redis:
        def get(self, key: str):
            return pointer if key.endswith("login-1") else session

    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: Redis())
    context = RedisThirdSessionResolver(_settings()).resolve(
        "svc", required_scope="aiops:orders:read", third_session="login-1"
    )
    assert context.subject.c_user_id == "C-1"


# --- #424: 会话身份补全 C→B 映射 --------------------------------------------
#
# 接缝不变，仍是「一次会话解析出一个 ScopeContext」；新断言只看身份形状、可见性与
# 范围指纹，不测内部调用顺序。


def _b_subject(**overrides: Any) -> SubjectRecord:
    fields: dict[str, Any] = {"b_user_id": "B-9", "c_user_id": "C-1", "tenant_id": "T-1"}
    fields.update(overrides)
    return SubjectRecord(**fields)


class _BSubjectDirectory:
    """In-memory C→B mapping directory recording every lookup."""

    def __init__(
        self,
        by_c_user_id: dict[str, tuple[SubjectRecord, ...]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.by_c_user_id = by_c_user_id or {}
        self.error = error
        self.calls: list[str] = []

    def users_by_c_user_id(self, c_user_id: str) -> tuple[SubjectRecord, ...]:
        self.calls.append(c_user_id)
        if self.error is not None:
            raise self.error
        return self.by_c_user_id.get(c_user_id, ())


def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    directory: _BSubjectDirectory | UpmsBSubjectDirectory | None,
) -> Any:
    _redis_session(monkeypatch, _java_session({"userId": "C-1", "tenantId": "T-1"}))
    resolver = RedisThirdSessionResolver(_settings(), b_subject_directory=directory)
    return resolver.resolve("svc", required_scope="aiops:orders:read", third_session=SESSION_TOKEN)


def test_session_identity_carries_b_side_subject_from_the_c_to_b_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _BSubjectDirectory({"C-1": (_b_subject(),)})

    context = _resolve(monkeypatch, directory)

    assert context.subject.c_user_id == "C-1"
    assert context.subject.b_user_id == "B-9"
    assert context.subject.b_subject_reason == ""
    assert directory.calls == ["C-1"]
    # 可见性判定不变：仍是 self 范围、同一租户、同一 C 端用户过滤。
    assert context.data_scope == DataScope(type="self")
    assert context.effective_tenant_id == "T-1"
    assert resolve_query_scope(context) == QueryScope(tenant_id="T-1", site_ids=None, user_id="C-1")


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ((), C_MAPPING_NOT_FOUND),
        ((_b_subject(), _b_subject(b_user_id="B-10")), C_MAPPING_AMBIGUOUS),
        ((_b_subject(tenant_id="T-OTHER"),), C_MAPPING_TENANT_MISMATCH),
    ],
)
def test_unresolvable_c_to_b_mapping_fails_closed_with_a_distinguishable_reason(
    monkeypatch: pytest.MonkeyPatch, records: tuple[SubjectRecord, ...], reason: str
) -> None:
    """非唯一的三种情形一律拒绝而非放行：身份不携带可用的 B 端主体。"""

    context = _resolve(monkeypatch, _BSubjectDirectory({"C-1": records}))

    assert context.subject.b_subject_reason == reason
    assert context.subject.c_user_id == "C-1"
    # B 端主体不可用，b_user_id 仍只是 C 侧占位值，不得被当作 B 端主体使用。
    assert context.subject.b_user_id == "c:C-1"


@pytest.mark.parametrize(
    "error",
    [
        ScopeError("UPMS 拒绝查询", code=SCOPE_ERROR_UPMS_UNAVAILABLE),
        ValueError("用户 ID 包含不允许的字符"),
    ],
)
def test_failing_c_to_b_mapping_is_refused_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    context = _resolve(monkeypatch, _BSubjectDirectory(error=error))

    assert context.subject.b_subject_reason == C_MAPPING_FAILED
    assert context.subject.b_user_id == "c:C-1"


def test_absent_c_to_b_mapping_directory_has_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _resolve(monkeypatch, None)

    assert context.subject.b_subject_reason == C_MAPPING_NOT_CONFIGURED
    assert context.subject.b_user_id == "c:C-1"


def test_data_level_c_mapping_problems_are_logged_without_identifiers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """歧义/悬空类原因必须留痕，但日志不带 C 端 id、租户或凭据。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.third_session_auth"):
        _resolve(monkeypatch, _BSubjectDirectory({"C-1": (_b_subject(), _b_subject(b_user_id="B-10"))}))

    assert C_MAPPING_AMBIGUOUS in caplog.text
    assert "C-1" not in caplog.text
    assert "T-1" not in caplog.text


def test_b_side_subject_enters_the_existing_scope_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapped = _resolve(monkeypatch, _BSubjectDirectory({"C-1": (_b_subject(),)}))
    other_subject = _resolve(monkeypatch, _BSubjectDirectory({"C-1": (_b_subject(b_user_id="B-10"),)}))
    unmapped = _resolve(monkeypatch, _BSubjectDirectory({"C-1": ()}))

    assert mapped.scope_fingerprint != unmapped.scope_fingerprint
    assert mapped.scope_fingerprint != other_subject.scope_fingerprint
    assert unmapped.scope_fingerprint != other_subject.scope_fingerprint


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _FakeUpmsTransport:
    """Serve the documented ``/user/inside/byUserId/{userId}`` contract."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: Any, timeout: int | None = None) -> _FakeResponse:
        split = urlsplit(request.full_url)
        self.requests.append((split.path, dict(request.header_items())))
        return _FakeResponse({"code": 0, "msg": "ok", "data": self.payload})


def _upms_directory() -> UpmsBSubjectDirectory:
    return UpmsBSubjectDirectory(
        UpmsSettings(base_url=UPMS_BASE_URL, timeout_seconds=5), INSIDE_CREDENTIAL
    )


def test_upms_b_subject_directory_reuses_the_existing_mapping_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B 端 id 取自既有 C→B 映射端点，不新增服务端接口。"""
    transport = _FakeUpmsTransport(
        [{"id": "B-9", "userId": "C-1", "username": "agent.9", "tenantId": "T-1"}]
    )
    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", transport)

    records = _upms_directory().users_by_c_user_id("C-1")

    assert [record.b_user_id for record in records] == ["B-9"]
    path, headers = transport.requests[-1]
    assert path == "/user/inside/byUserId/C-1"
    assert headers["Authorization"] == f"Bearer {INSIDE_CREDENTIAL}"


def test_session_identity_matches_what_upms_answers_for_the_same_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：会话解析出的身份与直接向 UPMS 查同一用户的结果一致。"""
    transport = _FakeUpmsTransport(
        [{"id": "B-9", "userId": "C-1", "username": "agent.9", "tenantId": "T-1"}]
    )
    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", transport)

    context = _resolve(monkeypatch, _upms_directory())

    assert context.subject == SubjectRecord(
        b_user_id="B-9", c_user_id="C-1", username="agent.9", tenant_id="T-1"
    )


# --- #424: 部署接缝 ---------------------------------------------------------


def _gateway_settings(tmp_path: Path, config_text: str) -> Any:
    from aiops_diagnostics.gateway_config import GatewayServerSettings

    config_file = tmp_path / "production.env"
    config_file.write_text(config_text, encoding="utf-8")
    os.chmod(config_file, 0o600)  # the config is a private file
    return GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config_file,
        third_session_service_token="svc",
    )


def test_third_session_resolver_is_wired_with_the_c_to_b_directory(tmp_path: Path) -> None:
    from aiops_diagnostics.gateway_api import _caller_resolver

    settings = _gateway_settings(
        tmp_path,
        "AIOPS_REDIS_PASSWORD=redis-secret\n"
        "AIOPS_UPMS_BASE_URL=https://upms.example.test\n"
        "AIOPS_UPMS_INSIDE_TOKEN=upms-internal-token\n",
    )

    resolver = _caller_resolver(settings)

    assert isinstance(resolver, RedisThirdSessionResolver)
    assert isinstance(resolver.b_subject_directory, UpmsBSubjectDirectory)


def test_third_session_resolver_without_the_inside_token_resolves_no_b_subject(
    tmp_path: Path,
) -> None:
    """未配置服务侧内部凭据时不发起 C→B 调用：管家端按 fail closed 拒绝，消费者端不变。"""
    from aiops_diagnostics.gateway_api import _caller_resolver

    settings = _gateway_settings(
        tmp_path, "AIOPS_REDIS_PASSWORD=redis-secret\nAIOPS_UPMS_BASE_URL=https://upms.example.test\n"
    )

    resolver = _caller_resolver(settings)

    assert isinstance(resolver, RedisThirdSessionResolver)
    assert resolver.b_subject_directory is None
