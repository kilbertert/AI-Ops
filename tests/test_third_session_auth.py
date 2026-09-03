from __future__ import annotations

import json

import pytest

from aiops_diagnostics.caller_auth import CALLER_AUTH_INVALID, CallerAuthError
from aiops_diagnostics.third_session_auth import RedisThirdSessionResolver, ThirdSessionSettings


class _Redis:
    def __init__(self, value: str | None):
        self.value = value

    def get(self, key: str):
        assert key == "third_session:session-123456789012345"
        return self.value


def _settings() -> ThirdSessionSettings:
    return ThirdSessionSettings(
        "127.0.0.1", 6379, 0, password="secret", service_token="svc", key_prefix="third_session:"
    )


def test_resolves_existing_third_session(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"userId": "C-1", "tenantId": "T-1"})
    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: _Redis(payload))
    context = RedisThirdSessionResolver(_settings()).resolve(
        "svc", required_scope="aiops:orders:read", third_session="session-123456789012345"
    )
    assert context.subject.c_user_id == "C-1"
    assert context.effective_tenant_id == "T-1"
    assert context.data_scope.type == "self"


@pytest.mark.parametrize("value", [None, "not-json", json.dumps({"userId": "C-1"})])
def test_missing_or_invalid_session_fails_closed(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: _Redis(value))
    with pytest.raises(CallerAuthError) as excinfo:
        RedisThirdSessionResolver(_settings()).resolve(
            "svc", required_scope="aiops:orders:read", third_session="session-123456789012345"
        )
    assert excinfo.value.code == CALLER_AUTH_INVALID
