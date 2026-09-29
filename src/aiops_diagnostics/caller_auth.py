from __future__ import annotations

import secrets
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

#: 入站来源密钥的请求头（#444 / ADR-0009 D8）。由**可信的那一跳**（未来是公司网关）覆盖式注入，
#: 前端不持有 —— 因此它是**调用方自报不了**的判据，也是两条信任模式之间唯一的分派依据。
#: 与 ``http_auth.INTERNAL_TOKEN_HEADER`` 同一条形状：AI-Ops 自己的入站秘密走独立的头，
#: 不复用 ``Authorization``（那一个装的是被校验的凭据，不是「这一跳可信」的断言）。
SOURCE_KEY_HEADER = "X-AIOps-Source-Key"


def source_key_accepted(configured: str, presented: str | None) -> bool:
    """来源密钥这道门：**未配置即不启用**，带且验过才认（#444 / ADR-0009 D9）。

    三条判据各有理由，都不是「顺手写的比较」：

    - ``configured`` 为空 ⇒ 一律 ``False``。这是 D8「未配置密钥时该路径整体不启用」的落点：
      fail closed 指的是**新链路不参与**，而不是「没有密钥就放行」。
    - ``presented`` 为空/缺失 ⇒ ``False``。不带这个头是**正常请求**（既有客户端与会话链
      就是这样），必须继续按既有链处理，所以这里返回 ``False`` 而不是抛错。
    - 比较走 ``hmac.compare_digest``：这道门对着可达的入口，逐字节比较会把「猜中几个字符」
      暴露成可用的时间侧信道，使枚举密钥成为可能。编码成 ``bytes`` 再比较是因为
      ``compare_digest`` 对含非 ASCII 的 ``str`` 会抛 ``TypeError`` —— 那会让一个畸形请求头
      变成 500，等于把「拒绝」写成了「崩」。

    这里**不做**任何规范化（不去空白、不折叠大小写、不剥前缀）：密钥是本侧生成的定长随机串，
    任何「宽容比较」都只会缩短有效密钥空间。运维配置进来时已被 ``_env`` 去掉两侧空白。
    """
    if not configured or not presented:
        return False
    return secrets.compare_digest(configured.encode("utf-8"), presented.encode("utf-8"))


class CallerAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class CallerContextResolver(Protocol):
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
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
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del token, required_scope, third_session, platform_entry, source_key
        raise CallerAuthError(
            "standard access-token validation is not configured",
            code=CALLER_AUTH_CONFIG_MISSING,
        )


class SourceKeyCallerResolver:
    """按**入站来源密钥**在两条信任模式之间分派（#444 / ADR-0009 D8–D9）。

    同一个入口上并存两条凭据路径：既有链（会话 / introspection / UPMS）与新链（公司
    OAuth2 令牌）。分派依据是**来源密钥**，而不是「令牌长什么样」或「试出来哪个解析器不报错」：

    - 按令牌形态分派要靠试错，顺序错了就是**静默降级**（先问会话、被拒，再问令牌路径），
      而两条路径的身份来源不同，降级的落点也就无法预期。
    - 来源密钥是**调用方自报不了**的判据：它由可信的那一跳（未来是公司网关）覆盖式注入，
      前端不持有，因此「带着正确密钥」等价于「这一跳被信任过」。

    三条边界：

    1. **未配置密钥时本类根本不构造**（见 ``gateway_api._caller_resolver``）—— 新链路整体不
       参与，请求仍按既有链处理或拒绝。fail closed 指的是**门不开**，不是「没门就放行」。
    2. **密钥验过才走新链，否则一律走既有链**，且既有链的输入（``third_session`` /
       ``platform_entry``）原样透传 —— 分派只决定「谁来解析」，不改解析结果。
    3. **带密钥的请求不再看 ``third_session``**：两条路径的凭据是两种东西（会话值 vs 公司令牌），
       同时带齐也由密钥定夺，避免「同一个请求因为多带一个头而换了身份来源」。
    """

    def __init__(
        self,
        source_key: str,
        company: CallerContextResolver,
        fallback: CallerContextResolver,
    ) -> None:
        if not source_key:
            raise ValueError("a source key is required to route between trust modes")
        self._source_key = source_key
        self._company = company
        self._fallback = fallback

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        if source_key_accepted(self._source_key, source_key):
            # 不传 ``third_session``：这条路径的凭据是公司令牌，会话值对它没有意义
            # （``CompanyTokenCallerResolver`` 也是 ``del third_session``）。
            return self._company.resolve(token, required_scope=required_scope, platform_entry=platform_entry)
        return self._fallback.resolve(
            token,
            required_scope=required_scope,
            third_session=third_session,
            platform_entry=platform_entry,
        )


class UpmsCallerResolver:
    """Validate company Bearer tokens through the existing UPMS boundary."""

    def __init__(self, settings: Settings) -> None:
        if not settings.upms.base_url:
            raise ValueError("UPMS caller validation requires AIOPS_UPMS_BASE_URL")
        self.resolver = ScopeResolver(UpmsDirectory(settings.upms))

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del third_session, source_key
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

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del third_session, source_key
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
