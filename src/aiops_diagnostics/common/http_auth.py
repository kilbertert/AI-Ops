from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable

INTERNAL_TOKEN_HEADER = "X-Internal-Token"
TIMESTAMP_HEADER = "X-Request-Timestamp"
DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS = 300


def _coerce_timestamp(value: str | int | None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_secrets(secrets: str | Iterable[str] | None) -> tuple[str, ...]:
    if secrets is None:
        return ()
    if isinstance(secrets, str):
        candidates = (secrets,)
    else:
        try:
            candidates = tuple(secrets)
        except TypeError:
            return ()
    if not candidates or any(not isinstance(secret, str) or not secret for secret in candidates):
        return ()
    return candidates


def build_internal_token(
    secret: str,
    timestamp: str | int,
    expire_seconds: int = DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS,
) -> str:
    """Return the platform-compatible HMAC-SHA256 internal token.

    The message format mirrors the Java ``InternalTokenManager``:
    ``hex( HMAC-SHA256(secret, "{timestamp}:{expireSeconds}") )``.
    """
    timestamp_text = str(int(timestamp))
    expire_text = str(int(expire_seconds))
    message = f"{timestamp_text}:{expire_text}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def build_internal_token_headers(
    secret: str,
    expire_seconds: int,
    timestamp: str | int,
) -> dict[str, str]:
    """Assemble the two authenticated request headers used by ``/diag/*`` clients."""
    timestamp_text = str(int(timestamp))
    return {
        INTERNAL_TOKEN_HEADER: build_internal_token(secret, timestamp_text, expire_seconds),
        TIMESTAMP_HEADER: timestamp_text,
    }


def validate_internal_token(
    token: str | None,
    timestamp: str | int | None,
    secrets: str | Iterable[str] | None,
    *,
    expire_seconds: int = DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS,
    now: str | int | None = None,
) -> bool:
    """Validate a platform internal token against one or more configured secrets.

    ``secrets`` may contain both the new and the previous key during rotation;
    a token signed by any candidate is accepted. Missing or empty secrets fail
    closed. The timestamp must be within ``expire_seconds`` of ``now``,
    mirroring ``InternalTokenManager``'s window.
    """
    request_timestamp = _coerce_timestamp(timestamp)
    now_timestamp = _coerce_timestamp(now if now is not None else int(time.time()))
    try:
        expire = int(expire_seconds)
    except (TypeError, ValueError):
        return False
    candidates = _coerce_secrets(secrets)
    if token is None or request_timestamp is None or now_timestamp is None or expire <= 0 or not candidates:
        return False
    if abs(now_timestamp - request_timestamp) > expire:
        return False
    token_text = str(token)
    for secret in candidates:
        expected = build_internal_token(secret, request_timestamp, expire)
        if hmac.compare_digest(token_text, expected):
            return True
    return False
