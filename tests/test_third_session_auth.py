from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from operator_support import (
    C_USER_ID,
    SESSION_TOKEN,
    TENANT,
    BSubjectDirectory,
    b_subject,
    java_session,
    session_settings,
)

from aiops_diagnostics.caller_auth import CALLER_AUTH_INVALID, CallerAuthError
from aiops_diagnostics.config import UpmsSettings
from aiops_diagnostics.query_scope import QueryScope, resolve_query_scope
from aiops_diagnostics.scope_context import (
    C_MAPPING_AMBIGUOUS,
    C_MAPPING_C_USER_MISMATCH,
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
    UpmsBSubjectDirectory,
)

UPMS_BASE_URL = "https://upms.example.test"
INSIDE_CREDENTIAL = "upms-internal-token"


# 无效会话载荷的替身：这个文件要驱动「没有载荷 / 不是 JSON / 缺字段」三种形状，
# 因此保留自己的 Redis 替身；有效载荷与 #426/#427/#428 共用 operator_support。
class _Redis:
    def __init__(self, value: str | bytes | None):
        self.value = value.encode() if isinstance(value, str) else value

    def get(self, key: str):
        assert key == "app:3rd_session:session-123456789012345"
        return self.value


def _redis_session(monkeypatch: pytest.MonkeyPatch, payload: str | bytes | None) -> None:
    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: _Redis(payload))


def test_resolves_existing_third_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _redis_session(monkeypatch, java_session({"userId": "C-1", "tenantId": "T-1"}))
    context = RedisThirdSessionResolver(session_settings()).resolve(
        "svc", required_scope="aiops:orders:read", third_session=SESSION_TOKEN
    )
    assert context.subject.c_user_id == "C-1"
    assert context.effective_tenant_id == "T-1"
    assert context.data_scope.type == "self"


@pytest.mark.parametrize("value", [None, "not-json", json.dumps({"userId": "C-1"})])
def test_missing_or_invalid_session_fails_closed(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    _redis_session(monkeypatch, value)
    with pytest.raises(CallerAuthError) as excinfo:
        RedisThirdSessionResolver(session_settings()).resolve(
            "svc", required_scope="aiops:orders:read", third_session=SESSION_TOKEN
        )
    assert excinfo.value.code == CALLER_AUTH_INVALID


def test_resolves_login_pointer_to_session_object(monkeypatch: pytest.MonkeyPatch) -> None:
    pointer = b"\xac\xed\x00\x05t\x00Kapp:3rd_session:wx:wx-1:uuid"
    session = java_session({"userId": "C-1", "tenantId": "T-1"})

    class Redis:
        def get(self, key: str):
            return pointer if key.endswith("login-1") else session

    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: Redis())
    context = RedisThirdSessionResolver(session_settings()).resolve(
        "svc", required_scope="aiops:orders:read", third_session="login-1"
    )
    assert context.subject.c_user_id == "C-1"


# --- #424: 会话身份补全 C→B 映射 --------------------------------------------
#
# 接缝不变，仍是「一次会话解析出一个 ScopeContext」；新断言只看身份形状、可见性与
# 范围指纹，不测内部调用顺序。


def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    directory: BSubjectDirectory | UpmsBSubjectDirectory | None,
) -> Any:
    """一次会话解析；替身（会话值、C→B 映射）与 #426/#427/#428 共用 operator_support。

    这里自己构造 resolver 而不是走 ``resolve_session``：断言里要看 ``directory.calls``，
    即「用过哪个 C 端 id 查过一次」——那是接缝的行为，不是实现细节。
    """
    _redis_session(monkeypatch, java_session({"userId": C_USER_ID, "tenantId": TENANT}))
    resolver = RedisThirdSessionResolver(session_settings(), b_subject_directory=directory)
    return resolver.resolve("svc", required_scope="aiops:orders:read", third_session=SESSION_TOKEN)


def test_session_identity_carries_b_side_subject_from_the_c_to_b_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = BSubjectDirectory({"C-1": (b_subject(),)})

    context = _resolve(monkeypatch, directory)

    assert context.subject.c_user_id == "C-1"
    assert context.subject.b_user_id == "B-9"
    assert context.subject.b_subject_reason == ""
    assert directory.calls == ["C-1"]
    # 未注入运营商站点范围（消费者端部署/缺配置）时可见性判定不变：仍是 self 范围、
    # 同一租户、同一 C 端用户过滤。注入了范围之后的判定见 #426 的
    # tests/test_operator_order_authorization.py。
    assert context.data_scope == DataScope(type="self")
    assert context.effective_tenant_id == "T-1"
    assert resolve_query_scope(context) == QueryScope(tenant_id="T-1", site_ids=None, user_id="C-1")


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ((), C_MAPPING_NOT_FOUND),
        ((b_subject(), b_subject(b_user_id="B-10")), C_MAPPING_AMBIGUOUS),
        ((b_subject(tenant_id="T-OTHER"),), C_MAPPING_TENANT_MISMATCH),
        # 同租户但绑的是另一个 C 端用户：核对的是 userId，不是 tenant_id。
        ((b_subject(c_user_id="C-OTHER"),), C_MAPPING_C_USER_MISMATCH),
        # 回带里没有 userId：无从核对，按不可用处理（fail closed）。
        ((b_subject(c_user_id=None),), C_MAPPING_C_USER_MISMATCH),
    ],
)
def test_unresolvable_c_to_b_mapping_fails_closed_with_a_distinguishable_reason(
    monkeypatch: pytest.MonkeyPatch, records: tuple[SubjectRecord, ...], reason: str
) -> None:
    """非唯一的四种情形一律拒绝而非放行：身份不携带可用的 B 端主体。"""

    context = _resolve(monkeypatch, BSubjectDirectory({"C-1": records}))

    assert context.subject.b_subject_reason == reason
    assert context.subject.c_user_id == "C-1"
    # B 端主体不可用，b_user_id 仍只是 C 侧占位值，不得被当作 B 端主体使用。
    assert context.subject.b_user_id == "c:C-1"


def test_a_cross_tenant_record_does_not_make_the_same_tenant_subject_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「本租户唯一主体 + 一条跨租户记录」不是歧义：先按会话租户筛选再判定。

    判在筛选之前会把唯一的主体判成 ambiguous_subject，会话退回 self 范围——
    PRD 要修掉的「查不到本运营商别人的单」在这个形状上复活。
    """
    records = (
        b_subject(),
        b_subject(b_user_id="B-2", c_user_id="C-X", tenant_id="T-OTHER"),
    )

    context = _resolve(monkeypatch, BSubjectDirectory({"C-1": records}))

    assert context.subject.b_user_id == "B-9"
    assert context.subject.b_subject_reason == ""
    assert context.subject.c_user_id == "C-1"


def test_two_same_tenant_records_are_still_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同租户内两条仍是歧义：筛选只挡跨租户，不放松同租户内的唯一性要求。"""
    records = (
        b_subject(c_user_id="C-OTHER"),
        b_subject(b_user_id="B-2", c_user_id="C-1"),
    )

    context = _resolve(monkeypatch, BSubjectDirectory({"C-1": records}))

    assert context.subject.b_subject_reason == C_MAPPING_AMBIGUOUS
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
    context = _resolve(monkeypatch, BSubjectDirectory(error=error))

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
        _resolve(monkeypatch, BSubjectDirectory({"C-1": (b_subject(), b_subject(b_user_id="B-10"))}))

    assert C_MAPPING_AMBIGUOUS in caplog.text
    assert "C-1" not in caplog.text
    assert "T-1" not in caplog.text


def test_b_side_subject_enters_the_existing_scope_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapped = _resolve(monkeypatch, BSubjectDirectory({"C-1": (b_subject(),)}))
    other_subject = _resolve(monkeypatch, BSubjectDirectory({"C-1": (b_subject(b_user_id="B-10"),)}))
    unmapped = _resolve(monkeypatch, BSubjectDirectory({"C-1": ()}))

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
    return UpmsBSubjectDirectory(UpmsSettings(base_url=UPMS_BASE_URL, timeout_seconds=5), INSIDE_CREDENTIAL)


def test_upms_b_subject_directory_reuses_the_existing_mapping_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B 端 id 取自既有 C→B 映射端点，不新增服务端接口。"""
    transport = _FakeUpmsTransport([{"id": "B-9", "userId": "C-1", "username": "agent.9", "tenantId": "T-1"}])
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
    transport = _FakeUpmsTransport([{"id": "B-9", "userId": "C-1", "username": "agent.9", "tenantId": "T-1"}])
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


def test_third_session_resolver_without_the_inside_token_resolves_nob_subject(
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


# --- #426: 部署接缝（运营商站点范围）----------------------------------------


def test_third_session_resolver_is_wired_with_the_operator_site_scope(tmp_path: Path) -> None:
    from aiops_diagnostics.gateway_api import _caller_resolver
    from aiops_diagnostics.third_session_auth import UpmsOperatorSiteScope

    settings = _gateway_settings(
        tmp_path,
        "AIOPS_REDIS_PASSWORD=redis-secret\n"
        "AIOPS_UPMS_BASE_URL=https://upms.example.test\n"
        "AIOPS_UPMS_INSIDE_TOKEN=upms-internal-token\n",
    )

    resolver = _caller_resolver(settings)

    assert isinstance(resolver, RedisThirdSessionResolver)
    assert isinstance(resolver.operator_scope, UpmsOperatorSiteScope)


def test_third_session_resolver_without_the_inside_token_has_no_operator_scope(
    tmp_path: Path,
) -> None:
    """缺同一套配置时不注入范围：会话数据范围保持 self，管家端因此 fail closed。"""
    from aiops_diagnostics.gateway_api import _caller_resolver

    settings = _gateway_settings(
        tmp_path, "AIOPS_REDIS_PASSWORD=redis-secret\nAIOPS_UPMS_BASE_URL=https://upms.example.test\n"
    )

    resolver = _caller_resolver(settings)

    assert resolver.operator_scope is None
