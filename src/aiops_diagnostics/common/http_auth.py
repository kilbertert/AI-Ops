from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

INTERNAL_TOKEN_HEADER = "X-Internal-Token"
TIMESTAMP_HEADER = "X-Request-Timestamp"
DEFAULT_INTERNAL_TOKEN_EXPIRE_SECONDS = 300

#: Dify 1.17.1 控制台双提交凭据名（https 且未设 COOKIE_DOMAIN 时带 ``__Host-`` 前缀）。
CONSOLE_ACCESS_TOKEN_COOKIE = "access_token"
CONSOLE_CSRF_COOKIE = "csrf_token"
CONSOLE_CSRF_HEADER = "X-CSRF-Token"
MAX_CONSOLE_TOKEN_CHARS = 8192
_CONSOLE_TOKEN_RE = re.compile(r"[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+")


@dataclass(frozen=True, slots=True)
class ConsoleSession:
    """运营的 Dify 控制台凭据：两枚 cookie（CSRF 头就是 ``csrf_token`` 的副本）。"""

    access_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    cookie_prefix: str = ""

    def cookie_header(self) -> str:
        return (
            f"{self.cookie_prefix}{CONSOLE_ACCESS_TOKEN_COOKIE}={self.access_token}; "
            f"{self.cookie_prefix}{CONSOLE_CSRF_COOKIE}={self.csrf_token}"
        )

    def dify_headers(self) -> dict[str, str]:
        """发往 Dify 的全部头 —— 除此之外浏览器带来的什么都不转。"""
        return {
            "Cookie": self.cookie_header(),
            CONSOLE_CSRF_HEADER: self.csrf_token,
            "Accept": "application/json",
        }


def is_console_token(value: str) -> bool:
    return 0 < len(value) <= MAX_CONSOLE_TOKEN_CHARS and _CONSOLE_TOKEN_RE.fullmatch(value) is not None


def console_session_from_headers(cookie_header: str | None, csrf_header: str | None) -> ConsoleSession | None:
    """从 ``Cookie:`` 与 ``X-CSRF-Token:`` 取出完整的 Dify 控制台双提交凭据；不完整返回 ``None``。"""
    if not cookie_header or not csrf_header:
        return None
    found: dict[str, list[str]] = {
        prefix + name: []
        for prefix in ("", "__Host-")
        for name in (CONSOLE_ACCESS_TOKEN_COOKIE, CONSOLE_CSRF_COOKIE)
    }
    for chunk in cookie_header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if name in found:
            found[name].append(value if sep else "")
    plain = bool(found[CONSOLE_ACCESS_TOKEN_COOKIE] or found[CONSOLE_CSRF_COOKIE])
    prefixed = bool(found["__Host-access_token"] or found["__Host-csrf_token"])
    if plain and prefixed:
        return None
    prefix = "__Host-" if prefixed else ""
    access, csrf = found[prefix + CONSOLE_ACCESS_TOKEN_COOKIE], found[prefix + CONSOLE_CSRF_COOKIE]
    if len(access) != 1 or len(csrf) != 1:
        return None
    if not (is_console_token(access[0]) and is_console_token(csrf[0]) and is_console_token(csrf_header)):
        return None
    if not hmac.compare_digest(csrf[0].encode("ascii"), csrf_header.encode("ascii")):
        return None
    return ConsoleSession(access_token=access[0], csrf_token=csrf[0], cookie_prefix=prefix)


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
