from __future__ import annotations

import hashlib
import hmac

INTERNAL_TOKEN_HEADER = "X-Internal-Token"
TIMESTAMP_HEADER = "X-Request-Timestamp"


def build_internal_token(secret: str, timestamp: str | int, expire_seconds: int) -> str:
    """Return the platform-compatible HMAC-SHA256 internal token.

    The message format mirrors the Java ``InternalTokenManager``:
    ``hex( HMAC-SHA256(secret, "{timestamp}:{expireSeconds}") )``.
    """
    timestamp_text = str(int(timestamp))
    message = f"{timestamp_text}:{int(expire_seconds)}"
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
