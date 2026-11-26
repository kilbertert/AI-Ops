"""Gateway 调用者身份解析器装配、UPMS 目录接线与统一认证异常映射。"""

from __future__ import annotations

from fastapi import status

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.conversation.faq import (
    PLATFORM_AMBIGUOUS,
    PLATFORM_FORBIDDEN,
    PLATFORM_UNAVAILABLE,
    FAQError,
)
from aiops_diagnostics.gateway.errors import StandardAPIError
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.identity.caller_auth import (
    CALLER_AUTH_CONFIG_MISSING,
    CALLER_AUTH_FORBIDDEN,
    CallerAuthError,
    CallerContextResolver,
    DisabledCallerResolver,
    EntryCallerResolver,
    IntrospectionCallerResolver,
    IntrospectionSettings,
    SourceKeyCallerResolver,
    UpmsCallerResolver,
)
from aiops_diagnostics.identity.company_permissions import CompanyPermissionSource
from aiops_diagnostics.identity.company_token_auth import (
    CompanyTokenCallerResolver,
    CompanyTokenSettings,
    PermissionSource,
)
from aiops_diagnostics.identity.query_scope import ShopDirectory, UpmsShopDirectory
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.identity.third_session_auth import (
    BSubjectDirectory,
    OperatorSiteScope,
    RedisThirdSessionResolver,
    ThirdSessionSettings,
    UpmsBSubjectDirectory,
    UpmsOperatorSiteScope,
)

#: The platform decision's failure modes and the answer each one gets. One
#: definition: the three copies this replaces had already drifted apart once,
#: and an error taxonomy with two answers is an error taxonomy with none.
_PLATFORM_ERROR_STATUS = {
    PLATFORM_AMBIGUOUS: status.HTTP_409_CONFLICT,
    PLATFORM_FORBIDDEN: status.HTTP_403_FORBIDDEN,
    PLATFORM_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _inside_credential(runtime: Settings) -> str:
    """会话身份的 UPMS 服务侧内部凭据（C→B 映射与运营商站点范围共用同一套前提）。

    两个接缝的配置前提逐字相同（UPMS 地址 + 该凭据），所以只写一遍：往其中一个
    加条件时不会漏掉另一个。
    """
    return (runtime.upms.inside_token or "").strip()


def _b_subject_directory(runtime: Settings) -> BSubjectDirectory | None:
    """会话身份的 C→B 映射目录（#424）。

    复用 UPMS 既有端点；未配置 UPMS 地址或服务侧内部凭据时返回 ``None``——会话身份
    只保留 C 侧部分，需要 B 端主体的路径按 fail closed 拒绝，消费者端不受影响。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsBSubjectDirectory(runtime.upms, credential)


def _shop_directory(runtime: Settings) -> ShopDirectory | None:
    """令牌不带 ``shop_ids`` 时的店铺归属来源（公司权威端点）。

    与 C→B 映射、运营商站点范围共用**同一套配置前提**（UPMS 地址 + 服务侧内部凭据）：
    未配置时返回 ``None``，此时令牌缺 ``shop_ids`` 一律拒绝（拿不到归属就是拿不到范围）。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsShopDirectory(runtime.upms, credential)


def _operator_site_scope(runtime: Settings) -> OperatorSiteScope | None:
    """会话身份的运营商站点范围（#426，四跳链的后两跳）。

    与 C→B 映射共用同一套配置前提（UPMS 地址 + 服务侧内部凭据）：未配置时返回
    ``None``，会话数据范围保持 ``self``——管家端因此只能看到本人的单（fail closed，
    不放宽），消费者端不变。站点归属映射复用受限直连的同一条跳板隧道，不为授权
    另开一条连接路径。
    """
    credential = _inside_credential(runtime)
    if not runtime.upms.base_url or not credential:
        return None
    return UpmsOperatorSiteScope(runtime, credential)


def _permission_source(runtime: Settings) -> PermissionSource | None:
    """公司权限码 → 内部角色（#926）。

    **未配置 UPMS 时不构造**（返回 ``None``），解析器随即保持空角色集 —— 与改动前逐字一致。
    这样「哪些部署会因此多拿到权限」是一个看配置就能回答的问题。
    """
    if not (runtime.upms.base_url or "").strip():
        return None
    return CompanyPermissionSource(runtime.upms)


def _company_token_resolver(settings: GatewayServerSettings) -> CompanyTokenCallerResolver | None:
    """新链路的解析器（#443 的实现），只在来源密钥已配置时构造。

    构造失败（配置读不出来）返回 ``None`` ⇒ 调用方退回既有链：带密钥的请求随后按既有链被
    拒绝（401），而不是被放行 —— 这是 fail closed 的方向。半配置（只设 URL 或只设密钥）
    已在 ``GatewayServerSettings.validate`` 里变成启动失败，不走这里。
    """
    # 两条通道二选一：配了签名密钥就走本地验签（A2 / #448），否则走远端 check_token。
    if not settings.company_check_token_url and not settings.company_jwt_key:
        return None
    try:
        runtime = Settings.from_config(settings.server_config_file)
        return CompanyTokenCallerResolver(
            CompanyTokenSettings(
                url=settings.company_check_token_url,
                client_id=settings.company_token_client_id,
                client_secret=settings.company_token_client_secret,
                signature_key=settings.company_jwt_key,
                trust_company_payload=settings.trust_company_payload,
            ),
            runtime,
            shop_directory=_shop_directory(runtime),
            permission_source=_permission_source(runtime),
        )
    except (ValueError, OSError):
        return None


def _fallback_caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    """既有链：会话 → UPMS 兜底 → introspection。选择结果与新增本票之前逐字一致。

    唯一的例外在会话那一级（ADR-0320）：登记表（``environment_registry_file``）部署
    且本环境 UPMS 可用时，operator 入口的个人 Bearer 由 ``EntryCallerResolver`` 改走
    本环境 UPMS 验证，其余入口逐字落到会话链。登记表未配置时返回值与分派引入之前
    逐字一致——旧 41 的合同不变；本环境 UPMS 不可配时保持纯会话链（fail closed，
    不把 operator 悄悄放行到一条验证不了它的链上）。
    """
    if settings.third_session_service_token:
        try:
            runtime = Settings.from_config(settings.server_config_file)
            resolver: CallerContextResolver = RedisThirdSessionResolver(
                ThirdSessionSettings(
                    host=runtime.redis.host,
                    port=runtime.redis.port,
                    database=runtime.redis.database,
                    username=runtime.redis.user,
                    password=runtime.redis.password,
                    service_token=settings.third_session_service_token,
                    key_prefix=settings.third_session_key_prefix,
                ),
                b_subject_directory=_b_subject_directory(runtime),
                operator_scope=_operator_site_scope(runtime),
            )
        except (ValueError, OSError):
            return DisabledCallerResolver()
        if settings.environment_registry_file and (runtime.upms.base_url or "").strip():
            # 与 ``OperatorCpms.refresh_scope`` 同一个实时站点目录：首次鉴权就把
            # UPMS 的混合断言规范化到站点集合，初/复核/回读比较同一口径的范围指纹；
            # 服务侧内部凭据未配时注入 None——分派原样透传，复核按既有 fail closed。
            directory = _operator_site_scope(runtime)
            resolver = EntryCallerResolver(
                UpmsCallerResolver(runtime),
                resolver,
                site_scope=None if directory is None else directory.site_scope_by_b_user_id,
            )
        return resolver
    # #444 起，公司令牌路径**不再出现在这条链里**：它由上面的来源密钥门分派，只有密钥验过的
    # 请求才走得到。把这四种选择结果写清楚，是为了让「带错密钥」的行为可预期 —— 它会落到这里，
    # 按会话/introspection/UPMS 处理公司令牌，结果是被拒绝（401），而不是被放行。
    if not settings.introspection_url:
        try:
            return UpmsCallerResolver(Settings.from_config(settings.server_config_file))
        except (ValueError, OSError):
            return DisabledCallerResolver()
    return IntrospectionCallerResolver(
        IntrospectionSettings(
            url=settings.introspection_url,
            client_id=settings.introspection_client_id,
            client_secret=settings.introspection_client_secret,
            audience=settings.standard_api_audience,
            timeout_seconds=settings.introspection_timeout_seconds,
        )
    )


def _caller_resolver(settings: GatewayServerSettings) -> CallerContextResolver:
    # #444 / ADR-0009 D9：来源密钥这道门**装在会话那一级之前**。这是本票最关键的一处，
    # 也是 #443 记录的两条边界里那条「必须放在会话之前」的落点：会话那一级是
    # ``if settings.third_session_service_token:`` 且**直接 return**，而 41 现网那个键有值 ——
    # 门若排在它之后，带公司令牌、不带 ``third-session`` 的请求会在第一级就被 401，
    # 新路径在现网**永远不可达**，即便把密钥配上也一样。
    #
    # 未配置来源密钥 ⇒ 这一整段不参与（新链路整体不启用，fail closed；不是放行），
    # 下面的选择链与本票之前逐字一致；清空即回滚。
    if settings.company_source_key:
        company = _company_token_resolver(settings)
        if company is not None:
            return SourceKeyCallerResolver(
                settings.company_source_key,
                company,
                _fallback_caller_resolver(settings),
            )
    return _fallback_caller_resolver(settings)


def _platform_error(exc: FAQError) -> StandardAPIError:
    """``FAQError`` (a platform-decision failure) as the response it deserves."""
    return StandardAPIError(
        _PLATFORM_ERROR_STATUS.get(exc.code, status.HTTP_503_SERVICE_UNAVAILABLE),
        exc.code,
        str(exc),
        retryable=exc.code == PLATFORM_UNAVAILABLE,
    )


def _authenticate_caller(
    resolver: CallerContextResolver,
    authorization: str | None,
    third_session: str | None,
    *,
    required_scope: str,
    platform_entry: str | None = None,
    source_key: str | None = None,
) -> ScopeContext:
    """The one place a ``CallerAuthError`` becomes an outbound error, and the
    one place the resolver is asked for an identity.

    Every ``authenticated_*`` dependency delegates here. There used to be three
    copies of this mapping and one of them had no ``CALLER_AUTH_CONFIG_MISSING``
    branch, so a missing gateway configuration told twelve endpoints' callers
    "your login has expired, sign in again" — the exact opposite of what
    ``docs/gateway.md`` promises (a stable 503, fail closed). A rule with two
    answers is a rule with none, and the copy that drifted was the one nobody
    looked at.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise StandardAPIError(status.HTTP_401_UNAUTHORIZED, "ACCESS_TOKEN_REQUIRED", "access token required")
    token = authorization.removeprefix("Bearer ").strip()
    if token.startswith("aops_"):
        raise StandardAPIError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_ACCESS_TOKEN",
            "access token validation failed",
        )
    try:
        # ``platform_entry`` 与 ``source_key`` 都在这里**无条件**传下去，不走「带没带会话」的
        # 分支：解析器各自忽略对自己无意义的参数，但分支之间一旦不对称，「同一个请求多带一个头
        # 就换了范围语义/身份来源」就会变成排查不了的形状（同 ``authenticated_diagnosis_caller``
        # 那条注释）。``source_key`` 尤其如此：它是分派依据，缺它门就永远落到既有链。
        return resolver.resolve(
            token,
            required_scope=required_scope,
            third_session=third_session,
            platform_entry=platform_entry,
            source_key=source_key,
        )
    except CallerAuthError as exc:
        if exc.code == CALLER_AUTH_FORBIDDEN:
            status_code, code, retryable = status.HTTP_403_FORBIDDEN, "INSUFFICIENT_SCOPE", False
        elif exc.code == CALLER_AUTH_CONFIG_MISSING:
            # Explicitly retryable, rather than relying on whoever raised it to
            # have passed `retryable=True`: `CallerAuthError`'s default is
            # False, and a configuration gap is by definition something a fix
            # restores. This is the rule deciding, not the raiser remembering.
            status_code, code = status.HTTP_503_SERVICE_UNAVAILABLE, "ACCESS_TOKEN_VALIDATION_UNAVAILABLE"
            retryable = True
        else:
            status_code = (
                status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_401_UNAUTHORIZED
            )
            code = "ACCESS_TOKEN_VALIDATION_UNAVAILABLE" if exc.retryable else "INVALID_ACCESS_TOKEN"
            retryable = exc.retryable
        raise StandardAPIError(
            status_code, code, "access token validation failed", retryable=retryable
        ) from exc
