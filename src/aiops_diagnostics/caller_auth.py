from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from aiops_diagnostics.bounded_http import (
    FORM_CONTENT_TYPE,
    JSON_CONTENT_TYPE,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    basic_auth_header,
    form_body,
    parse_raw_envelope,
    request_json,
)
from aiops_diagnostics.config import Settings
from aiops_diagnostics.query_scope import resolve_query_scope
from aiops_diagnostics.scope_context import (
    DataScope,
    ScopeContext,
    ScopeError,
    ScopeRequest,
    ScopeResolver,
    SubjectRecord,
    UpmsDirectory,
)
from aiops_diagnostics.sources import scoped_live_sources

CALLER_AUTH_INVALID = "caller_auth.invalid"
CALLER_AUTH_FORBIDDEN = "caller_auth.forbidden"
CALLER_AUTH_UNAVAILABLE = "caller_auth.unavailable"
CALLER_AUTH_CONFIG_MISSING = "caller_auth.config_missing"


class CallerAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CallerContextResolver(Protocol):
    def resolve(
        self, token: str, *, required_scope: str, third_session: str | None = None
    ) -> ScopeContext: ...


class OrderAuthorizer(Protocol):
    def can_access(self, context: ScopeContext, order_no: str) -> bool: ...


class DisabledOrderAuthorizer:
    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        del context, order_no
        raise CallerAuthError(
            "standard order authorization is not configured",
            code=CALLER_AUTH_CONFIG_MISSING,
        )


@dataclass(frozen=True, slots=True)
class IntrospectionSettings:
    url: str
    client_id: str
    client_secret: str = field(repr=False)
    audience: str
    timeout_seconds: int = 5

    def validate(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("introspection URL must be a complete HTTP or HTTPS URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote introspection URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("introspection URL must not contain credentials, query, or fragment")
        if not self.client_id or not self.client_secret or not self.audience:
            raise ValueError("introspection client credentials and audience are required")
        if not 1 <= self.timeout_seconds <= 30:
            raise ValueError("introspection timeout must be between 1 and 30 seconds")


class DisabledCallerResolver:
    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del token, required_scope, third_session
        raise CallerAuthError(
            "standard access-token validation is not configured",
            code=CALLER_AUTH_CONFIG_MISSING,
        )


class UpmsCallerResolver:
    """Validate company Bearer tokens through the existing UPMS boundary."""

    def __init__(self, settings: Settings) -> None:
        if not settings.upms.base_url:
            raise ValueError("UPMS caller validation requires AIOPS_UPMS_BASE_URL")
        self.resolver = ScopeResolver(UpmsDirectory(settings.upms))

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del third_session
        try:
            context = self.resolver.resolve(ScopeRequest(credential=token))
        except ScopeError as exc:
            raise CallerAuthError("platform access token rejected", code=CALLER_AUTH_INVALID) from exc
        # ponytail: cloud-auth currently exposes platform permissions, not resource scopes.
        if not context.permissions and required_scope:
            raise CallerAuthError("platform caller has no permissions", code=CALLER_AUTH_FORBIDDEN)
        return context


class IntrospectionCallerResolver:
    def __init__(self, settings: IntrospectionSettings) -> None:
        settings.validate()
        self.settings = settings

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del third_session
        if not token or token.startswith("aops_"):
            raise CallerAuthError("invalid access token", code=CALLER_AUTH_INVALID)
        payload = self._introspect(token)
        if payload.get("active") is not True:
            raise CallerAuthError("inactive access token", code=CALLER_AUTH_INVALID)

        subject_id = _text(payload.get("sub"))
        tenant_id = _text(payload.get("tenant_id") or payload.get("tenantId"))
        if not subject_id or not tenant_id:
            raise CallerAuthError("access token is missing subject or tenant", code=CALLER_AUTH_INVALID)

        audience = _values(payload.get("aud"))
        if self.settings.audience not in audience:
            raise CallerAuthError("access token audience is not accepted", code=CALLER_AUTH_INVALID)

        scopes = frozenset(_scope_values(payload.get("scope")))
        if required_scope not in scopes:
            raise CallerAuthError("access token scope is insufficient", code=CALLER_AUTH_FORBIDDEN)

        expires_at = payload.get("exp")
        if not isinstance(expires_at, (int, float)) or expires_at <= datetime.now(UTC).timestamp():
            raise CallerAuthError("access token is expired", code=CALLER_AUTH_INVALID)

        try:
            data_scope = _data_scope(payload.get("data_scope") or payload.get("dataScope"))
        except ValueError as exc:
            raise CallerAuthError("invalid access-token data scope", code=CALLER_AUTH_INVALID) from exc
        subject = SubjectRecord(
            b_user_id=subject_id,
            c_user_id=_text(payload.get("c_user_id") or payload.get("cUserId")) or None,
            username=_text(payload.get("username")),
            tenant_id=tenant_id,
            organ_id=_text(payload.get("organ_id") or payload.get("organId")) or None,
            shop_id=_text(payload.get("shop_id") or payload.get("shopId")) or None,
        )
        roles = frozenset(_values(payload.get("roles")))
        permissions = frozenset(_values(payload.get("permissions"))) | scopes
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant_id,
            data_scope=data_scope,
            roles=roles,
            permissions=permissions,
        )

    def _introspect(self, token: str) -> Mapping[str, Any]:
        # The transport skeleton owns request assembly, the failure capture set
        # and the status-code table. This client only declares how its own
        # domain errors are built -- including the one deliberate difference
        # from the default table: introspection treats 400 as a rejected token
        # (a malformed token), not just 401/403.
        def _rejected(_failure: HttpFailure) -> Exception:
            return CallerAuthError("introspection rejected the token", code=CALLER_AUTH_INVALID)

        def _unavailable(_failure: HttpFailure) -> Exception:
            return CallerAuthError(
                "introspection service is unavailable",
                code=CALLER_AUTH_UNAVAILABLE,
                retryable=True,
            )

        def _invalid_body(_failure: HttpFailure) -> Exception:
            # A body that is not valid UTF-8/JSON is a transport-level fault of
            # the same family as an unreachable service, and must stay retryable
            # rather than escaping as an unmapped exception.
            return CallerAuthError(
                "introspection service is unavailable",
                code=CALLER_AUTH_UNAVAILABLE,
                retryable=True,
            )

        payload = request_json(
            RequestSpec(
                url=self.settings.url,
                method="POST",
                headers={
                    "Authorization": basic_auth_header(self.settings.client_id, self.settings.client_secret),
                    "Accept": JSON_CONTENT_TYPE,
                    "Content-Type": FORM_CONTENT_TYPE,
                },
                body=form_body({"token": token}),
                timeout=self.settings.timeout_seconds,
            ),
            mapping=ErrorMapping(
                auth_rejected=_rejected,
                http_error=_unavailable,
                unavailable=_unavailable,
                invalid_body=_invalid_body,
                invalid_envelope=_rejected,
                auth_rejected_statuses=frozenset({400, 401, 403}),
            ),
            envelope=parse_raw_envelope,
        )
        if not isinstance(payload, Mapping):
            raise CallerAuthError("invalid introspection response", code=CALLER_AUTH_INVALID)
        return payload


class ScopedOrderAuthorizer:
    """订单授权判定：某身份能否查某订单。

    范围一律来自 ``resolve_query_scope(context)``，因此管家端会话的运营商维度
    （#426：会话身份带上该 B 端主体的运营商站点集合）与平台 Bearer 调用者的数据
    范围走同一个判定。判定只回答「能查 / 被拒绝」，订单不存在与无权查看得到同一个
    ``False``——订单号不能用来探测他人业务。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        query_scope = resolve_query_scope(context)
        with scoped_live_sources(self.settings, scope=query_scope) as sources:
            return bool(sources.get_orders(order_no))


def _data_scope(value: object) -> DataScope:
    if not isinstance(value, Mapping):
        raise CallerAuthError("access token is missing data scope", code=CALLER_AUTH_INVALID)
    return DataScope(
        type=_text(value.get("type")) or "organ",
        organ_ids=tuple(_values(value.get("organ_ids") or value.get("organIds"))),
        shop_ids=tuple(_values(value.get("shop_ids") or value.get("shopIds"))),
        site_ids=tuple(_values(value.get("site_ids") or value.get("siteIds"))),
    )


def _scope_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item for item in value.split() if item)
    return _values(value)


def _values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(text for item in value if (text := _text(item)))
    return ()


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""
