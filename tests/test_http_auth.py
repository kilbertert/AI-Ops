from __future__ import annotations

import hashlib
import hmac

from aiops_diagnostics.http_auth import build_internal_token, build_internal_token_headers


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
