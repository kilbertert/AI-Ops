from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import redis

from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_INVALID,
    CALLER_AUTH_UNAVAILABLE,
    CallerAuthError,
)
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


@dataclass(frozen=True, slots=True)
class ThirdSessionSettings:
    host: str
    port: int
    database: int
    username: str = ""
    password: str = field(repr=False, default="")
    service_token: str = field(repr=False, default="")
    key_prefix: str = "third_session:"
    timeout_seconds: int = 5


class RedisThirdSessionResolver:
    def __init__(self, settings: ThirdSessionSettings) -> None:
        if not settings.password or not settings.service_token:
            raise ValueError("thirdSession Redis and service credentials are required")
        self.settings = settings

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        if not secrets.compare_digest(token, self.settings.service_token):
            raise CallerAuthError("service authentication failed", code=CALLER_AUTH_INVALID)
        if not third_session or len(third_session) > 256 or any(c in third_session for c in "\r\n"):
            raise CallerAuthError("thirdSession is invalid", code=CALLER_AUTH_INVALID)
        try:
            client = redis.Redis(
                host=self.settings.host,
                port=self.settings.port,
                db=self.settings.database,
                username=self.settings.username or None,
                password=self.settings.password,
                decode_responses=True,
                socket_connect_timeout=self.settings.timeout_seconds,
                socket_timeout=self.settings.timeout_seconds,
            )
            raw = client.get(f"{self.settings.key_prefix}{third_session}")
        except redis.RedisError as exc:
            raise CallerAuthError(
                "thirdSession store unavailable", code=CALLER_AUTH_UNAVAILABLE, retryable=True
            ) from exc
        if not raw:
            raise CallerAuthError("thirdSession invalid or expired", code=CALLER_AUTH_INVALID)
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CallerAuthError("thirdSession payload invalid", code=CALLER_AUTH_INVALID) from exc
        if not isinstance(payload, dict):
            raise CallerAuthError("thirdSession payload invalid", code=CALLER_AUTH_INVALID)
        user_id = str(payload.get("userId") or payload.get("user_id") or "").strip()
        tenant_id = str(payload.get("tenantId") or payload.get("tenant_id") or "").strip()
        if not user_id or not tenant_id:
            raise CallerAuthError("thirdSession subject invalid", code=CALLER_AUTH_INVALID)
        subject = SubjectRecord(b_user_id=f"c:{user_id}", c_user_id=user_id, tenant_id=tenant_id)
        caller = SubjectRecord(b_user_id="service:java-bff", tenant_id=tenant_id)
        return ScopeContext.build(
            caller=caller,
            subject=subject,
            delegated=True,
            effective_tenant_id=tenant_id,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )
