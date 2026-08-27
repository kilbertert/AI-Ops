from __future__ import annotations

import hashlib
import hmac

from aiops_diagnostics.http_auth import (
    DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS,
    build_internal_token,
    build_internal_token_headers,
    validate_internal_token,
)


def _expected(secret: str, timestamp: str, expire_seconds: int) -> str:
    message = f"{timestamp}:{expire_seconds}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def test_internal_token_and_headers() -> None:
    token = _expected("shared-secret", "1760000000", 300)
    assert build_internal_token("shared-secret", "1760000000", 300) == token
    assert build_internal_token("shared-secret", 1760000000, 300) == token
    assert build_internal_token_headers("shared-secret", 300, 1760000000) == {
        "X-Internal-Token": token,
        "X-Request-Timestamp": "1760000000",
    }


def test_internal_token_defaults_to_300_second_window() -> None:
    assert DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS == 300
    assert build_internal_token("shared-secret", 1760000000) == _expected("shared-secret", "1760000000", 300)


def test_validate_internal_token_accepts_current_token() -> None:
    timestamp = 1760000000
    token = build_internal_token("shared-secret", timestamp)
    assert validate_internal_token(token, timestamp, "shared-secret", now=timestamp)
    assert validate_internal_token(token, str(timestamp), ("shared-secret",), now=timestamp)


def test_validate_internal_token_rejects_expired_timestamps() -> None:
    timestamp = 1760000000
    token = build_internal_token("shared-secret", timestamp)
    assert not validate_internal_token(token, timestamp, "shared-secret", now=timestamp + 301)
    assert not validate_internal_token(token, timestamp, "shared-secret", now=timestamp - 301)


def test_validate_internal_token_accepts_clock_window_boundaries() -> None:
    timestamp = 1760000000
    token = build_internal_token("shared-secret", timestamp)
    assert validate_internal_token(token, timestamp, "shared-secret", now=timestamp + 300)
    assert validate_internal_token(token, timestamp, "shared-secret", now=timestamp - 300)


def test_validate_internal_token_rejects_invalid_clock_and_expiry() -> None:
    timestamp = 1760000000
    token = build_internal_token("shared-secret", timestamp)

    assert not validate_internal_token(token, "not-a-timestamp", "shared-secret", now=timestamp)
    assert not validate_internal_token(token, timestamp, "shared-secret", now="not-a-timestamp")
    assert not validate_internal_token(token, timestamp, "shared-secret", expire_seconds=0, now=timestamp)
    assert not validate_internal_token(token, timestamp, "shared-secret", expire_seconds=-1, now=timestamp)


def test_validate_internal_token_rejects_missing_secrets_without_raising() -> None:
    timestamp = 1760000000
    token = build_internal_token("shared-secret", timestamp)

    assert not validate_internal_token(token, timestamp, None, now=timestamp)
    assert not validate_internal_token(token, timestamp, (), now=timestamp)
    assert not validate_internal_token(token, timestamp, "", now=timestamp)


def test_validate_internal_token_supports_dual_key_rotation() -> None:
    timestamp = 1760000000
    old_token = build_internal_token("old-secret", timestamp)
    new_token = build_internal_token("new-secret", timestamp)

    assert validate_internal_token(old_token, timestamp, ("new-secret", "old-secret"), now=timestamp)
    assert validate_internal_token(new_token, timestamp, ("new-secret", "old-secret"), now=timestamp)
    assert not validate_internal_token(old_token, timestamp, ("new-secret",), now=timestamp)
    assert not validate_internal_token(new_token, timestamp, ("old-secret",), now=timestamp)


def test_validate_internal_token_rejects_forged_token_and_missing_headers() -> None:
    timestamp = 1760000000
    assert not validate_internal_token("forged", timestamp, "shared-secret", now=timestamp)
    assert not validate_internal_token(None, timestamp, "shared-secret", now=timestamp)
    assert not validate_internal_token("forged", None, "shared-secret", now=timestamp)
