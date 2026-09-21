from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_FORBIDDEN,
    CALLER_AUTH_INVALID,
    CALLER_AUTH_UNAVAILABLE,
    CallerAuthError,
    IntrospectionCallerResolver,
    IntrospectionSettings,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _settings() -> IntrospectionSettings:
    return IntrospectionSettings(
        url="https://auth.example.test/oauth2/introspect",
        client_id="aiops",
        client_secret="secret",
        audience="aiops-api",
    )


def _active(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "active": True,
        "sub": "B-1",
        "c_user_id": "C-1",
        "tenant_id": "TENANT-1",
        "aud": ["aiops-api"],
        "scope": "aiops:orders:read",
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "data_scope": {"type": "self"},
    }
    payload.update(overrides)
    return payload


def test_introspection_builds_immutable_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def respond(request, **_kwargs):
        requests.append(request)
        return _Response(_active())

    monkeypatch.setattr(
        "aiops_diagnostics.bounded_http.urllib.request.urlopen",
        respond,
    )

    context = IntrospectionCallerResolver(_settings()).resolve(
        "opaque-token",
        required_scope="aiops:orders:read",
    )

    assert context.caller.b_user_id == "B-1"
    assert context.subject.c_user_id == "C-1"
    assert context.effective_tenant_id == "TENANT-1"
    assert context.data_scope.type == "self"
    assert context.scope_fingerprint
    assert requests[0].get_header("Authorization") == "Basic YWlvcHM6c2VjcmV0"


def test_introspection_form_encodes_basic_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    def respond(request, **_kwargs):
        requests.append(request)
        return _Response(_active())

    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", respond)
    settings = IntrospectionSettings(
        url="https://auth.example.test/oauth2/introspect",
        client_id="client id",
        client_secret="secret:value",
        audience="aiops-api",
    )

    IntrospectionCallerResolver(settings).resolve(
        "opaque-token",
        required_scope="aiops:orders:read",
    )

    assert requests[0].get_header("Authorization") == "Basic Y2xpZW50JTIwaWQ6c2VjcmV0JTNBdmFsdWU="
    assert "secret:value" not in repr(settings)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (_active(active=False), CALLER_AUTH_INVALID),
        (_active(aud=["other-api"]), CALLER_AUTH_INVALID),
        (_active(scope="other:scope"), CALLER_AUTH_FORBIDDEN),
        (_active(exp=1), CALLER_AUTH_INVALID),
        (_active(sub=""), CALLER_AUTH_INVALID),
        (_active(tenant_id=""), CALLER_AUTH_INVALID),
        (_active(data_scope={"type": "unknown"}), CALLER_AUTH_INVALID),
    ],
)
def test_introspection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected_code: str,
) -> None:
    monkeypatch.setattr(
        "aiops_diagnostics.bounded_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(CallerAuthError) as excinfo:
        IntrospectionCallerResolver(_settings()).resolve(
            "opaque-token",
            required_scope="aiops:orders:read",
        )

    assert excinfo.value.code == expected_code
    assert "opaque-token" not in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_introspection_transport_failure_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", unavailable)

    with pytest.raises(CallerAuthError) as excinfo:
        IntrospectionCallerResolver(_settings()).resolve(
            "opaque-token",
            required_scope="aiops:orders:read",
        )

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable


def test_introspection_non_utf8_body_is_a_coded_domain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-UTF-8 introspection body must not escape as a raw UnicodeDecodeError.

    The peer returning bytes that are not UTF-8 is a transport-level fault of the
    same family as an unreachable service; it has to surface as the retryable
    CALLER_AUTH_UNAVAILABLE, because an unmapped exception reaches the API layer
    as a 500 instead of a 503.
    """

    class _BadEncoding:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b"\xff\xfe\x00 invalid utf-8"

    monkeypatch.setattr(
        "aiops_diagnostics.bounded_http.urllib.request.urlopen",
        lambda *_a, **_k: _BadEncoding(),
    )

    with pytest.raises(CallerAuthError) as excinfo:
        IntrospectionCallerResolver(_settings()).resolve(
            "opaque-token",
            required_scope="aiops:orders:read",
        )

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable


def test_introspection_mid_connection_failure_is_a_coded_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection dropped mid-read must map like any other transport fault.

    ConnectionResetError and IncompleteRead were absent from the previous
    capture set, so a dropped connection produced an unmapped exception rather
    than a coded, retryable domain error.
    """

    def _dropped(*_args, **_kwargs):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", _dropped)

    with pytest.raises(CallerAuthError) as excinfo:
        IntrospectionCallerResolver(_settings()).resolve(
            "opaque-token",
            required_scope="aiops:orders:read",
        )

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable


def test_introspection_truncated_body_is_a_coded_domain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated response body (IncompleteRead) is a transport fault too."""
    import http.client

    class _Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b'{"active": tr')

    monkeypatch.setattr(
        "aiops_diagnostics.bounded_http.urllib.request.urlopen",
        lambda *_a, **_k: _Truncated(),
    )

    with pytest.raises(CallerAuthError) as excinfo:
        IntrospectionCallerResolver(_settings()).resolve(
            "opaque-token",
            required_scope="aiops:orders:read",
        )

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable
