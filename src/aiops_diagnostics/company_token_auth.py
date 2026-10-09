"""公司 OAuth2 访问令牌 → 管家端身份与运营商站点范围（ADR-0009 第一段）。

管家端 App（``ulinkmanage.h5.mall.qushiyun.com``）登录走公司 ``/upms/token/login``，拿到的是
**公司签发的 OAuth2 访问令牌**。AI-Ops 既有的两条凭据路径都认不出它：共享会话 Redis 里没有
``app:3rd_session:<该令牌>``（前端照现状接必然 ``401``），而 RFC 7662 自省那条要求
``active``/``aud``/``scope`` 的**具体含义与公司这份不同**（见下：公司也会给 ``scope``，但
身份不在 ``sub``/``data_scope`` 里）。本模块补上「把令牌翻成身份」的那一跳。

**校验落在公司权威入口**（``/oauth/check_token``，公司资源服务器 ``RemoteTokenServices`` 用的
同一处，ADR-0009 D2）：不自己验签、不复制密钥、不读令牌存储、不把「能以令牌读到对象」当校验。

⚠️ **这条端点对「服务间调用」是否成立，尚未验证，而现有证据指向不成立**（#448 的探查，
2026-09-29 41 实测 + Spring 源码复核）。本模块只实现了这条链路，**没有**、也无法证明公司那边
会接受我们的凭据：

- **`sys_oauth_client` 里现有 7 个客户端的 `client_secret` 就是它的 `client_id` 字面量**
  （`admin`/`admin`、`app`/`app`、…）。那是为了让公司旧的 `NoOpPasswordEncoder` 能匹配而写进库的
  历史值，不是「有凭据可取」——**它等于公开值**。
- 实测 `POST /auth/oauth/check_token` 带 `Basic admin:admin` 与**不带** Basic 得到**逐字相同**的
  `{"code":1,"msg":"Full authentication is required to access this resource"}`。
  ⇒ 能推出的只有：**这些输入下没有任何一种产生成功认证**（即这条路径不能当服务间入口用）。
  **不能**据此定位失败发生在 `client_details` 认证**之前**还是**之中** —— 两者产生同样的观测，
  要区分需要公司侧服务端日志，我们没有。`cloud-auth` 的 `WebSecurityConfigurer` 注册的是
  `PasswordEncoderFactories.createDelegatingPasswordEncoder()`（要求 `{bcrypt}` 之类前缀），
  与库里的字面量 secret 匹配不上 —— 这是**同一条链之内**失败的一种**候选解释**（与观测一致，
  但依赖上面那个未定的分支），**不是结论**。
- 另一条入口 `/auth/oauth/token` 实测返回 `{"code":1,"msg":"验证码不能为空"}`（与管家端登录同一条
  被图形验证码拦住的链）。

因此本模块的**启用前提**是一条公司侧未解的依赖：要么为 AI-Ops 建一个真正的客户端（`id == secret`
不算），要么公司另有服务间校验入口。**在那之前不要把这条路径当作可用**：模块已实现，
但它的外部依赖在 41 上未就绪（见基线文档 §4 第 1 条）。

⚠️ **不能复用 ``IntrospectionCallerResolver``**（实现时的第一坑）：公司成功时返回的是
**框架组装的身份映射**（框架字段 + 增强器字段合并，**没有** ``code``/``data`` 信封），不是
RFC 7662 的自省体 —— 身份在增强器注入的 ``id``/``tenant_id`` 里，而不是 ``sub`` + ``aud`` +
``data_scope``。两处「都是 OAuth2 自省」是巧合。

依据是公司源码与 Spring 源码，且两条路径形状不同：

- **成功**：``DefaultAccessTokenConverter.convertAccessToken`` 先放框架字段
  （``username``/``authorities``（仅用户令牌分支）/``scope``/``exp``/``jti``，``resourceIds``
  非空时还有 ``aud``），最后一步才是 ``response.putAll(token.getAdditionalInformation())``
  **合并且覆盖同名键**；身份字段来自 ``cloud-auth/AuthorizationServerConfig.tokenEnhancer()``
  逐字段写入的 ``additionalInformation``（``id``/``user_id``/``username``/``organ_id``/
  ``type``/``tenant_id``/``system_id``/``shop_id``/``license``/``tenant_ids``/``shop_ids``）。
  随后 ``CheckTokenAccessTokenConverter`` 无条件 ``put("active", true)``，再补 ``client_id``。
  公司那两个 ``ResponseBodyAdvice``（``I18nResponseAdvice``、``TenantNameResponseAdvice``）的
  ``supports()`` 都要求**控制器方法声明的返回类型**可被 ``R`` 赋值，而 ``check_token`` 声明的是
  ``Map`` ⇒ **不包装**。所以成功体里没有 ``code``、没有 ``data``，但**确实有** ``scope``/``exp``/
  ``client_id``/``active``（客户端凭据令牌还有 ``aud``/``authorities``）。
- **失败**：非法/过期令牌由 ``CheckTokenEndpoint`` 抛异常 → 公司
  ``BaseWebResponseExceptionTranslator`` → ``ResponseEntity.ok().body(R.failed(e.getOAuth2ErrorCode(),
  e.getMessage()))``，即 **HTTP 200 + ``{"code":<码>,"msg":"token无效","data":null}``**
  （``CommonConstants.SUCCESS=0`` / ``FAIL=1``；``R.failed(Integer, String)`` 走
  ``restResult(null, code, msg)`` ⇒ ``data`` 是 **null**）。它是 ``@ExceptionHandler`` 的返回值，
  不经过上面那条 advice。
  ⚠️ ``data:"invalid_token"`` 是**另一条**路径的形状（资源服务器入口
  ``ResourceAuthExceptionEntryPoint``），**不是** ``check_token`` 的 —— 这条端点的失败体
  ``data`` 为 null。

因此判据是「**存在且非 0/200 的 ``code`` ⇒ 拒绝**」，而不是「``code`` 必须等于 0」——
后者会把每一条合法令牌都拒掉。**注意这个判据不能换成 HTTP 状态**：令牌无效也返回 HTTP 200
（见上），按状态判会把它读成成功。

⚠️ ``active`` **不是**判据：公司确实返回它（Spring 的 ``CheckTokenAccessTokenConverter`` 无条件
``put("active", true)``），但客户端凭据令牌同样 ``active: true``。判据是**增强器注入的身份字段**
（``id`` + ``tenant_id``）—— 这也是为什么不能用更宽的形状判据：``username``/``client_id``/``exp``/
``scope``/``authorities`` 对**客户端凭据令牌同样存在**，只有增强器写的字段能区分。

``exp`` 有意不本地判：公司是权威校验方（过期令牌在那一跳就被拒成 ``code:1``），本地再判一次
只会引入时钟偏移这个新的失败模式，而 ``shop_ids`` 快照的 staleness 窗口已按令牌有效期接受
（见下）。

**范围语义**：身份取令牌（B 端 ``id`` / C 端 ``user_id`` / ``tenant_id``）；数据范围按内容域
分流（#436 的同一条边界，见 ``_data_scope``）—— 只有 ``operator`` 入口换成「令牌 ``shop_ids`` →
运营商站点集合」（经 #442 的共享解析 ``operator_site_scope_from_shops``，规则只有一份），其余
入口保持最窄的 ``self``。失败一律 fail closed，**不回落成任何更宽的范围**。

**明写的代价**（ADR-0009 Consequences）：令牌里的 ``shop_ids`` 是**签发时刻**的快照，而既有
``/shopuser/getShops`` 是实时的 —— staleness 窗口 = 令牌有效期。这是已接受的取舍（店铺绑定变更
是低频事件），**不是缺陷**；若将来实测到「变更后、令牌未过期期间的越权查询」成为真实问题，
那是回到实时查询的选择（加一跳），不是改模型。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
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
from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_INVALID,
    CALLER_AUTH_UNAVAILABLE,
    CallerAuthError,
)
from aiops_diagnostics.config import Settings
from aiops_diagnostics.query_scope import (
    ShopDirectory,
    SiteScopeMapper,
    operator_site_scope_from_shops,
)
from aiops_diagnostics.scope_context import (
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeContext,
    ScopeError,
    SubjectRecord,
    is_operator_entry,
    is_tenant_level_account,
)
from aiops_diagnostics.sources import SourceError, mysql_site_mapper

_LOGGER = logging.getLogger(__name__)

#: 密钥长度的**告警**阈值（不是拒绝阈值）。
#:
#: 这里刻意只告警、不拒绝 —— 上一版把它做成了硬门（<16 字符即启动失败），而**公司线上的真钥匙
#: 就是 10 个字符**，于是这条检查在真实部署上直接把「启用」这条路封死了。评审当初建议加长度门
#: 是对的，但**当时没人知道真钥匙多长**；现在知道了，判据必须按事实写。
#:
#: 保留告警的理由：短钥匙可枚举，而这条模式把「令牌有效」的断言整个搬到了本进程 ——
#: 换成真钥匙之后这条告警会消失，它同时也是「钥匙还没换」的一个信号。
SHORT_SIGNATURE_KEY_WARNING_LENGTH = 16

#: ``mysql_site_mapper`` 的形状：由 ``Settings`` 构造一个上下文管理器，产出站点归属映射。
#: 测试注入替身时替换它，生产路径不换。
ScopeMapperFactory = Callable[[Settings], AbstractContextManager[SiteScopeMapper]]


@dataclass(frozen=True, slots=True)
class CompanyTokenSettings:
    """公司 ``/oauth/check_token`` 的接入配置。

    ``client_secret`` 参与 ``Basic`` 认证头，绝不进入任何日志、审计摘要或错误消息 ——
    因此用 ``repr=False`` 把 ``dataclass`` 自动生成的 ``__repr__`` 里的明文摘掉。
    """

    url: str = ""
    client_id: str = ""
    client_secret: str = field(repr=False, default="")
    timeout_seconds: int = 10
    #: 本地校验模式的签名密钥（A2 / #448）。**与远端模式互斥**：两者都配即启动失败，
    #: 因为「谁来断言这条令牌」只能有一个答案。
    signature_key: str = field(repr=False, default="")
    #: **是否按公司自己的判据认令牌**（默认 True = 公司一致）：公司既不验签、也不判 `exp`，
    #: 按载荷里的 `id` 查用户（41 实测，见 ``validation.md``）。设 False 则回到「验签 + 判 exp」
    #: 的更严模式 —— 那是安全上更可取、但与公司不一致的行为。用户裁定按公司口径，故默认 True。
    trust_company_payload: bool = True

    def validate(self) -> None:
        if self.signature_key:
            if len(self.signature_key) < SHORT_SIGNATURE_KEY_WARNING_LENGTH:
                # 只记一行、不拦：拦了就没法用真实的钥匙启用（见常量上的说明）。
                _LOGGER.warning(
                    "company JWT signature key is shorter than %d characters; "
                    "this is expected only while the company default key is still in use",
                    SHORT_SIGNATURE_KEY_WARNING_LENGTH,
                )
            if not self.url and not self.client_id and not self.client_secret:
                return
            raise ValueError(
                "company token access must use either local signature verification or "
                "the remote check_token endpoint, not both"
            )
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("company check_token URL must be a complete HTTP or HTTPS URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote company check_token URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("company check_token URL must not contain credentials, query, or fragment")
        if not self.client_id or not self.client_secret:
            raise ValueError("company check_token client credentials are required")
        if not 1 <= self.timeout_seconds <= 30:
            raise ValueError("company check_token timeout must be between 1 and 30 seconds")


class CompanyTokenCallerResolver:
    """公司 OAuth2 令牌 → ``ScopeContext``（管家端身份 + 运营商站点范围）。

    与 ``RedisThirdSessionResolver`` 的分工：那条认共享会话，这条认公司令牌，两者产出的
    ``ScopeContext`` 形状相同（同一套 ``data_scope`` 机制、同一个范围指纹），因此订单授权判定与
    证据收集对两条链是同一条代码路径，不需要为管家端另开一条查询路径。

    ``scoped_settings`` 是站点归属映射（``ch_site.shop_id → ch_site.id``）所需的数据源配置，
    与受限直连同一条跳板隧道 —— 缓存它（而不是每次现取）是为了让构造点的配置注入显式可见。
    """

    def __init__(
        self,
        settings: CompanyTokenSettings,
        scoped_settings: Settings,
        *,
        scope_mapper_factory: ScopeMapperFactory | None = None,
        shop_directory: ShopDirectory | None = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.scoped_settings = scoped_settings
        self._scope_mapper: ScopeMapperFactory = scope_mapper_factory or mysql_site_mapper
        #: 令牌不带 ``shop_ids`` 时的店铺归属来源（公司权威端点，与后端隔离集合同源）。
        #: 为 ``None`` 时该情形一律拒绝 —— 拿不到归属就是拿不到范围，不放宽。
        self._shop_directory = shop_directory

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        # ``third_session`` 对本解析器无意义：走到这里说明凭据是公司令牌，不是共享会话值。
        # ``source_key`` 同样无意义：**校验在分派处**（``SourceKeyCallerResolver``），能进到本方法
        # 说明门已经放行过。不在这里再验一次是有意的 —— 两处校验一旦漂移，漂移方向就是「其中一处
        # 放行」，而这里没有配置可比（密钥在分派器的配置里，不在本解析器里）。
        del third_session, source_key
        if not token or token.startswith("aops_"):
            raise CallerAuthError("invalid access token", code=CALLER_AUTH_INVALID)
        claims = self._claims(token)
        subject = _subject_from_claims(claims)
        # ⚠️ **非管家端入口不解析店铺集合**（2026-09-29 修正）：``_data_scope`` 对 consumer/缺失/
        # 非法入口一律返回最窄的 ``self``，因此这里算出来的店铺集合**随后会被丢弃**。而生产令牌
        # 实测**不带 ``shop_ids``**，缺键会走回退去查公司权威的 ``/shopuser/getShops`` —— 于是
        # 消费者请求会为了一个用不上的答案去打一次公司内部端点，且在该端点不可用时把
        # **不相关的故障**引进消费者路径（503），并与 ADR-0009 决定 2「不再调 /shopuser/getShops」
        # 直接矛盾。
        #
        # 因此把入口判定提到解析**之前**：只有管家端入口才需要店铺集合。判据与 ``_data_scope``
        # 共用 ``is_operator_entry``（两份实现漂移的方向就是放宽）。
        #
        # 形状校验仍在解析内部保留（``_shop_ids_from_claims``）：管家端入口上一条形状不符的
        # ``shop_ids`` 照旧被拒 —— 那一条与入口无关，只是现在只在需要它的入口上发生。
        # 顶层账号（平台/租户主账号）的范围是整个租户，与店铺集合无关，因此连解析都不做
        # —— 提前返回与 ``_data_scope`` 里那一条同判据，并省掉可能触发 ``/shopuser/getShops``
        # 的那一跳。
        if is_operator_entry(platform_entry) and not is_tenant_level_account(subject.user_type):
            shop_ids = _shop_ids_from_claims(
                claims, shop_directory=self._shop_directory, b_user_id=subject.b_user_id
            )
        else:
            shop_ids = ()
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            # 查的是自己的运营商范围，不是代查他人：保持 True 会进入代查分支（未配 Dis 报错、
            # 配了则与 Dis 点位取交集而收窄）。与会话路径同一条定调（PRD #423）。
            delegated=False,
            effective_tenant_id=subject.tenant_id or "",
            data_scope=self._data_scope(subject, shop_ids, platform_entry),
            roles=frozenset(),
            # 与会话解析器一致：令牌已由公司校验，能力面不在这里裁；``required_scope`` 照常
            # 通过，因此本路径不产出 403。
            permissions=frozenset({required_scope}),
        )

    def _data_scope(
        self, subject: SubjectRecord, shop_ids: tuple[str, ...], platform_entry: str | None
    ) -> DataScope:
        """令牌路径的数据范围：**只有管家端入口**换成运营商站点集合。

        与 ``third_session_auth`` 的那一行是**同一条边界**（#436 的回归修复）：替换
        ``self`` 的语义是为管家端设计的（运营商员工要看本运营商名下站点的订单），但它在身份层
        生效，若不按入口分流，消费者侧会被一并收窄与放宽。未知入口在这里按最窄的 ``self``
        处理、不放宽 —— 非法入口随后由平台决策拒绝，范围已经先收紧。

        判据用 ``is_operator_entry``（两份实现漂移的方向就是放宽，所以它只有一份）。

        解析失败一律失败关闭为**空集合**（拒绝全部订单查询），不回落成租户级放行、也不套用
        ``self`` —— 后者会把「查不到运营商范围」伪装成「只能看本人的单」。失败覆盖链路上每一种
        逃逸方式：上游不可达/形状非法/数量超界（``ScopeError``）、充电库不可用（``SourceError``）、
        范围 ID 不可用（``ValueError``）；不捕获就等于让一次授权故障变成 500。
        """
        if not is_operator_entry(platform_entry):
            return DataScope(type=SCOPE_TYPE_SELF)
        if is_tenant_level_account(subject.user_type):
            # 与 ``third_session_auth._data_scope`` 同一条边界、同一个判据：顶层账号
            # （平台/租户主账号，``type ∈ {-1,1}``）的可见范围是整个租户。
            # **不解析店铺集合**：它的空集合在这里既不表示「看不到订单」，多打一次
            # ``/shopuser/getShops`` 也改变不了结论（见 ``resolve`` 里对 shop_ids 的
            # 按需取值）—— 令牌常常不带 ``shop_ids``，提前返回可省掉那一跳。
            return DataScope(type=SCOPE_TYPE_ALL)
        try:
            with self._scope_mapper(self.scoped_settings) as mapper:
                scope = operator_site_scope_from_shops(shop_ids, subject.tenant_id or "", mapper=mapper)
        except (ScopeError, SourceError, ValueError) as exc:
            _log_scope_unavailable(exc)
            return DataScope(type=SCOPE_TYPE_ORGAN, site_ids=())
        return DataScope(type=SCOPE_TYPE_ORGAN, site_ids=scope.site_ids)

    def _claims(self, token: str) -> Mapping[str, Any]:
        """调公司 ``check_token`` 并取回身份声明（成功与失败是**两种形状**，见模块文档）。

        这里不能按 ``code``/``data`` 信封判：合法令牌根本没有 ``code`` 键。因此用
        ``parse_raw_envelope``（接受对象与数组，形状判断留给本客户端），再按下面这条规则分流：

        - 载荷**是对象**且带 ``code`` 键且该码不在 ``{0, 200}`` ⇒ 公司明确拒绝了这条令牌；
        - 其它 ⇒ 按身份声明解读（缺 ``id``/``tenant_id`` 才拒）。

        这条规则与 ``parse_code_data_envelope`` 的差别只在「没有 ``code`` 时怎么办」：骨架那条
        要求 ``code`` 存在且成功，用来读这条端点是错的（会把每条合法令牌都拒成信封不符）。
        """

        def _rejected(failure: HttpFailure) -> Exception:
            # ⚠️ 这一跳的 401/403 说的是「AI-Ops 的客户端凭据没被接受」，不是「用户令牌无效」：
            # 公司的 ``checkTokenAccess("isAuthenticated()")`` 是客户端凭据门，而用户令牌的判决
            # 是 HTTP 200 + ``code`` 非 0（见 ``_unavailable``）。报成 INVALID 会把运维问题
            # 说成用户问题（客户端去重新登录，而真正要改的是 AI-Ops 的配置），因此归到
            # ``UNAVAILABLE``。``retryable`` 是对外文案的一部分：HTTP 层这一条取真值，让
            # 网关把它译成 503 而不是 401（``_authenticate_caller`` 的分支按它选状态码）。
            del failure
            return CallerAuthError(
                "company check_token rejected the caller",
                code=CALLER_AUTH_UNAVAILABLE,
                retryable=True,
            )

        def _unavailable(failure: HttpFailure) -> Exception:
            if failure.cause is None:
                # 骨架用 ``cause is None`` 表达「这次拒绝来自信封层」（见 ``HttpFailure`` 的
                # 文档串）：上游明确答了「这条令牌不成立」，不是它不可用、重试也没用。
                return CallerAuthError("company token rejected", code=CALLER_AUTH_INVALID)
            return CallerAuthError(
                "company check_token service is unavailable",
                code=CALLER_AUTH_UNAVAILABLE,
                retryable=True,
            )

        def _invalid_envelope(_failure: HttpFailure) -> Exception:
            # 信封结构不符（载荷既不是对象也不是数组）：按上游不可用处理更贴近事实 ——
            # 一条形状异常的响应既不能说令牌成立，也不能说令牌被拒。
            return CallerAuthError(
                "company check_token service is unavailable",
                code=CALLER_AUTH_UNAVAILABLE,
                retryable=True,
            )

        if self.settings.signature_key:
            # 本地校验模式（A2 / #448）：按 HS256 验签后**直接从令牌读声明**，不调用任何上游。
            # ⚠️ 这条模式把「令牌有效」的断言从公司那一跳搬到了本进程，因此它的前提是**签名密钥
            # 本身是秘密**。密钥是配置项（``AIOPS_GATEWAY_COMPANY_JWT_KEY``），不进仓库、不进日志。
            return _claims_from_signed_jwt(
                token,
                self.settings.signature_key,
                trust_company_payload=self.settings.trust_company_payload,
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
                # 其余 HTTP 状态（5xx/404/网关错误）：上游答了但答不成，属不可用。
                http_error=_unavailable,
                unavailable=_unavailable,
                # 非 JSON 响应体（一层反代返回的 HTML 错误页）是**传输**故障，与
                # ``IntrospectionCallerResolver`` 同一处理：可重试的 UNAVAILABLE，不能逃成
                # 未映射异常，也不能说成「令牌被拒」。
                invalid_body=_unavailable,
                invalid_envelope=_invalid_envelope,
            ),
            envelope=parse_raw_envelope,
        )
        if not isinstance(payload, Mapping):
            # 没有身份声明可读（数组/标量）：上游答了，但答的不是这条通道的答案。
            raise CallerAuthError("company token response is invalid", code=CALLER_AUTH_INVALID)
        if _envelope_refused(payload):
            raise CallerAuthError("company token rejected", code=CALLER_AUTH_INVALID)
        return payload


def _claims_from_signed_jwt(token: str, key: str, *, trust_company_payload: bool = True) -> Mapping[str, Any]:
    """本地按 HS256 校验公司 JWT 并取回声明（A2 / #448 的第二条通道）。

    与远端 ``check_token`` 的差别**只在「谁断言令牌有效」**：这里由本进程用共享密钥验签，
    其余字段解读完全共用（``_subject_from_claims`` / ``_shop_ids_from_claims``）。

    **必须验签，不能只解 base64**：不验签的话任何人都能自造一份 ``id`` 声明，等于把身份交给
    调用方自报。

    **两种模式，由 ``trust_company_payload`` 决定**：

    - **True（生产默认）= 公司一致**：不验签、不判 ``exp`` —— 与公司接口行为一致。
      失败仍 fail closed：结构不是三段、``alg`` 不是 HS256。
    - **False = 严格模式**：额外验签 + 判 ``exp``。

    两模式共同保留的判据：``alg`` 必须**显式等于 HS256**，不接受 ``none``，
    也不按令牌自报的算法选实现（那正是 JWT 算法混淆的入口）。
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise CallerAuthError("company token is not a JWT", code=CALLER_AUTH_INVALID)
    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise CallerAuthError("company token header is invalid", code=CALLER_AUTH_INVALID) from exc
    if not isinstance(header, Mapping) or header.get("alg") != "HS256":
        raise CallerAuthError("company token algorithm is not accepted", code=CALLER_AUTH_INVALID)
    if not trust_company_payload:
        try:
            expected = hmac.new(
                key.encode("utf-8"),
                f"{header_b64}.{payload_b64}".encode("ascii"),
                hashlib.sha256,
            ).digest()
            signature = _b64url_decode_bytes(signature_b64)
        except (ValueError, UnicodeEncodeError) as exc:
            raise CallerAuthError("company token signature is invalid", code=CALLER_AUTH_INVALID) from exc
        if not hmac.compare_digest(expected, signature):
            # 记一行**可区分**的原因：这两个分支此前都不记日志，而网关对外只看得到同一句
            # 「access token validation failed」—— 于是「签名不匹配」与「已过期」在观测面上
            # **不可分**，排查时只能靠猜（#458 的评审就指出过这一点）。
            # 只记原因，不记令牌、不记密钥、不记身份。
            _LOGGER.info("company token rejected reason=signature_mismatch")
            raise CallerAuthError("company token signature does not verify", code=CALLER_AUTH_INVALID)
    else:
        # 公司一致模式：**不验签**。公司侧实测既不验签也不判 `exp`，按载荷里的 `id` 查用户；
        # 有效性由会话对象决定。用户裁定按此口径适配前端（前端用户长时间停留、不重登，
        # 而公司接口接受已过期的令牌，两边判据不同会让「公司能用、AI-Ops 401」）。
        #
        # ⚠️ 这条就是公司当前的判据，也是它的一个安全弱项：令牌载荷可伪造（实测：签名整段
        # 换成 `A…` 公司仍 200，只改 `id` 才 500「用户不存在」）。**我们的防线相应后移到**
        # 「令牌必须来自 nginx 那一跳」（``X-AIOps-Source-Key``，调用方自报不了）**与**
        # 「身份字段必须齐备且租户一致」。这比验签弱 —— 但它是**用户明确选定的边界**，
        # 且与公司其余接口行为一致；要回收它把 ``trust_company_payload`` 设回 False 即可。
        _LOGGER.info("company token accepted reason=company_parity_no_verification")
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise CallerAuthError("company token payload is invalid", code=CALLER_AUTH_INVALID) from exc
    if not isinstance(payload, Mapping):
        raise CallerAuthError("company token payload is invalid", code=CALLER_AUTH_INVALID)
    # 公司一致模式**不判 `exp`**（公司也不判，见上）；严格模式才判。
    #
    # ⚠️ **这里判得比公司自己严，是有意的 —— 但它是本模式与公司行为的一处已知差异**：
    # 41 实测，同一条 `exp` 已过的令牌打公司自己的接口 **仍然 200**
    # （`GET /upms/user/info` → 返回该用户信息；`/user/check` → `ok:true`），
    # 说明**公司侧在当前配置下并不因为 `exp` 过期而拒绝**（它认的是 Redis 里的会话对象，
    # 由登出/撤销删除；`exp` 只写进对象、不做时效判定）。
    #
    # 因此会出现「前端拿同一把令牌打公司接口通、打 AI-Ops 401」的现象，**那不是 AI-Ops 出错**。
    # 两条路要选一条：
    #   (a) 保持现状（拒绝过期令牌）：更严，但与公司其余接口的行为不一致；
    #   (b) 放宽到与公司一致：只验签、不判 `exp` —— 需产品/安全确认（届时删掉本段判定即可）。
    # **本文件选 (a)**：放宽一个授权边界不该由实现方默认决定。
    if not trust_company_payload:
        expires_at = payload.get("exp")
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            raise CallerAuthError("company token has no expiry", code=CALLER_AUTH_INVALID)
        if expires_at <= datetime.now(UTC).timestamp():
            _LOGGER.info("company token rejected reason=expired")
            raise CallerAuthError("company token is expired", code=CALLER_AUTH_INVALID)
    return payload


def _b64url_decode(value: str) -> str:
    try:
        return _b64url_decode_bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("not UTF-8") from exc


def _b64url_decode_bytes(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64url") from exc


def _envelope_refused(claims: Mapping[str, Any]) -> bool:
    """公司是否**明确拒绝**了这条令牌。

    判据是「存在 ``code`` 键且该码不是 0/200」—— 合法令牌**没有** ``code``（见模块文档），
    而公司的拒绝（``{"code":1,"msg":"token无效","data":null}``）有。``code`` 缺失 ⇒ 不是拒绝，
    继续按身份声明解读；解读阶段缺 ``id``/``tenant_id`` 会自己拒。

    ⚠️ 判据只能在**信封**上，不能在 HTTP 状态上：令牌无效也走 HTTP 200（公司把异常译成
    ``ResponseEntity.ok().body(R.failed(...))``）。任何「404/401 就拒绝、其余放行」的写法
    都会把一条无效令牌静默读成有效。

    ``code`` 允许是字符串（``"0"``）：公司的 ``R`` 用 ``Integer``，但同一家的 JSON 里数字字段
    并非总是数字，多一种写法不增加放行面（非 ``{0,200}`` 一律拒，含无法解析的值）。
    """
    if "code" not in claims:
        return False
    code = claims.get("code")
    if isinstance(code, bool):
        return True
    if isinstance(code, int):
        return code not in {0, 200}
    if isinstance(code, str) and code.strip().isdigit():
        return int(code.strip()) not in {0, 200}
    return True


def _subject_from_claims(claims: Mapping[str, Any]) -> SubjectRecord:
    """公司身份声明 → ``SubjectRecord``（缺 B 端主体或租户即拒绝，不猜）。

    ``id`` 是 B 端 ``sys_user.id``（管家端授权的起点），``user_id`` 是 C 端；两者实测无重叠，
    不能互替。租户只取 ``tenant_id``：``tenant_ids`` 是平台管理员可**切换**的租户列表
    （``BaseUser.tenantIds`` 的注释即如此），拿它当范围只会放宽 —— 与「不回落成更宽范围」相悖。
    """
    b_user_id = _text(claims.get("id"))
    tenant_id = _text(claims.get("tenant_id"))
    if not b_user_id or not tenant_id:
        raise CallerAuthError("company token is missing its identity", code=CALLER_AUTH_INVALID)
    return SubjectRecord(
        b_user_id=b_user_id,
        c_user_id=_text(claims.get("user_id")) or None,
        username=_text(claims.get("username")),
        tenant_id=tenant_id,
        organ_id=_text(claims.get("organ_id")) or None,
        shop_id=_text(claims.get("shop_id")) or None,
        # ``type`` 由 tokenEnhancer 逐字段写入（``BaseUser.getType()``），因此与 ``id``/
        # ``tenant_id`` 同一条通道、同样的可信度。它决定「空店铺集合」的读法（见
        # ``is_tenant_level_account``）；缺失时按最窄处理。
        user_type=_text(claims.get("type")) or "",
    )


def _shop_ids_from_claims(
    claims: Mapping[str, Any], *, shop_directory: ShopDirectory | None = None, b_user_id: str = ""
) -> tuple[str, ...]:
    """令牌里的店铺集合（``shop_ids``），形状不符即拒绝。

    口径有意不对称，因为两者的信息来源不同：**键缺失**说明这条通道根本没给这个答案
    （客户端凭据令牌就没有身份、异常响应也不会带），拒；**值为 null / 空列表**说明上游明确答了
    「没有绑定店铺」—— 那是真实账号的合法状态（41 上验收账号即如此），交给共享解析产出**空
    范围**并记一条 ``no_shop_binding``（下游据此拒绝，见 §5 的站点绑定缺口）。
    """
    if "shop_ids" not in claims:
        # ⚠️ 生产令牌实测**不带** ``shop_ids``（keys: exp/id/organ_id/role_ids/shop_id/system_id/
        # tenant_id/type/username），``shop_id`` 对代理商账号还是空串 —— 也就是说「店铺集合」这条
        # 声明在令牌里**常常没有**。此时退回公司权威的店铺归属查询（``/shopuser/getShops``，
        # 与后端 ``@ShopDataScope`` 的隔离集合同源）。这不是放宽：拿不到归属仍然得到空集合。
        if shop_directory is None or not b_user_id:
            raise CallerAuthError("company token is missing its shop ids", code=CALLER_AUTH_INVALID)
        try:
            return shop_directory.shop_ids_by_b_user_id(b_user_id)
        except (ScopeError, SourceError, ValueError) as exc:
            # ⚠️ 归属查询的失败**必须**译成 ``CallerAuthError``：网关只捕获这一种并译成
            # 401/503（``_authenticate_caller``），让它逃出去就是 500 —— 一个授权依赖不可用
            # 会被报成服务端崩溃。失败关闭的方向：拒绝，而不是放行或 500。
            raise CallerAuthError(
                "company token shop lookup is unavailable", code=CALLER_AUTH_UNAVAILABLE, retryable=True
            ) from exc
    value = claims.get("shop_ids")
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise CallerAuthError("company token shop ids are invalid", code=CALLER_AUTH_INVALID)
    shop_ids: list[str] = []
    for item in value:
        text = _text(item)
        if not text:
            raise CallerAuthError("company token shop ids are invalid", code=CALLER_AUTH_INVALID)
        if text not in shop_ids:
            shop_ids.append(text)
    return tuple(shop_ids)


def _log_scope_unavailable(error: Exception) -> None:
    """运营商站点范围解析失败：可区分原因（错误码或异常类型），不含身份标识。

    与 ``third_session_auth`` 的 ``operator_scope_unavailable`` **分开**记：两条链路的成因不同
    （那边要去看会话/UPMS，这边要去看公司令牌与 check_token），混用一个词汇就无法从日志判断
    是哪条链坏了。也不与 #425 的两种空集合成因（漏登记 / 数据缺口）混：那两种拒绝是正确行为，
    这里要去看上游。
    """
    _LOGGER.info(
        "company_token scope_unavailable code=%s",
        getattr(error, "code", None) or type(error).__name__,
    )


def _text(value: object) -> str:
    """标量取值：非标量（含 ``bool``）一律空串，让调用方按「缺失/形状不符」处理。

    ``bool`` 显式排除：``str(True)`` 会得到一个看起来合法的标识符，而公司契约里没有任何字段
    是布尔。数组/对象同理 —— 让它们静默变成 ``"{'a': 1}"`` 比拒绝更危险。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()
