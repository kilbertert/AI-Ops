"""公司 OAuth2 访问令牌 → 管家端身份与运营商站点范围（ADR-0009 第一段）。

管家端 App（``ulinkmanage.h5.mall.qushiyun.com``）登录走公司 ``/upms/token/login``，拿到的是
**公司签发的 OAuth2 访问令牌**。AI-Ops 既有的两条凭据路径都认不出它：共享会话 Redis 里没有
``app:3rd_session:<该令牌>``（前端照现状接必然 ``401``），而 RFC 7662 自省那条要求
``active``/``aud``/``scope`` —— 公司这份都没有。本模块补上「把令牌翻成身份」的那一跳。

**校验落在公司权威入口**（``/oauth/check_token``，公司资源服务器 ``RemoteTokenServices`` 用的
同一处，ADR-0009 D2）：不自己验签、不复制密钥、不读令牌存储、不把「能以令牌读到对象」当校验。

⚠️ **不能复用 ``IntrospectionCallerResolver``**（实现时的第一坑）：公司成功时返回的是**裸映射**
（令牌的 ``additionalInformation`` 被合并进顶层：``id``/``user_id``/``username``/``organ_id``/
``type``/``tenant_id``/``system_id``/``shop_id``/``tenant_ids``/``shop_ids``），**没有**
``aud``/``scope``，**也没有** ``code``/``data`` 信封。两处「都是 OAuth2 自省」是巧合。

依据是公司源码而非推测，且两条路径形状不同：

- **成功**：``cloud-auth/AuthorizationServerConfig.tokenEnhancer()`` 逐字段列出
  ``additionalInformation``；公司未设置 ``accessTokenConverter``，故走 Spring ``CheckTokenEndpoint``
  的默认转换器，把该映射**合并进顶层**。公司那两个 ``ResponseBodyAdvice``（``I18nResponseAdvice``、
  ``TenantNameResponseAdvice``）的 ``supports()`` 都要求**控制器方法声明的返回类型**可被 ``R`` 赋值，
  而 ``check_token`` 声明的是 ``Map`` ⇒ **不包装**。所以成功体里没有 ``code``。
- **失败**：非法/过期令牌由 ``CheckTokenEndpoint`` 抛异常 → 公司
  ``BaseWebResponseExceptionTranslator`` → ``R.failed``，即 **HTTP 200 +
  ``{"code":1,"msg":"token无效","data":"invalid_token"}``**（``CommonConstants.SUCCESS=0`` /
  ``FAIL=1``）。它是 ``@ExceptionHandler`` 的返回值，不经过上面那条 advice。

因此判据是「**存在且非 0/200 的 ``code`` ⇒ 拒绝**」，而不是「``code`` 必须等于 0」——
后者会把每一条合法令牌都拒掉。

⚠️ ``active`` **不是**判据：公司确实返回它（Spring 的 ``CheckTokenAccessTokenConverter`` 无条件
``put("active", true)``），但客户端凭据令牌同样 ``active: true`` 而身份字段为空 —— 按 ``active``
判会给一条没有身份的通路放行。判据是**公司的身份字段**（``id`` + ``tenant_id``）。

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

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
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
    SiteScopeMapper,
    operator_site_scope_from_shops,
)
from aiops_diagnostics.scope_context import (
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeContext,
    ScopeError,
    SubjectRecord,
    is_operator_entry,
)
from aiops_diagnostics.sources import SourceError, mysql_site_mapper

_LOGGER = logging.getLogger(__name__)

#: ``mysql_site_mapper`` 的形状：由 ``Settings`` 构造一个上下文管理器，产出站点归属映射。
#: 测试注入替身时替换它，生产路径不换。
ScopeMapperFactory = Callable[[Settings], AbstractContextManager[SiteScopeMapper]]


@dataclass(frozen=True, slots=True)
class CompanyTokenSettings:
    """公司 ``/oauth/check_token`` 的接入配置。

    ``client_secret`` 参与 ``Basic`` 认证头，绝不进入任何日志、审计摘要或错误消息 ——
    因此用 ``repr=False`` 把 ``dataclass`` 自动生成的 ``__repr__`` 里的明文摘掉。
    """

    url: str
    client_id: str
    client_secret: str = field(repr=False)
    timeout_seconds: int = 10

    def validate(self) -> None:
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
    ) -> None:
        settings.validate()
        self.settings = settings
        self.scoped_settings = scoped_settings
        self._scope_mapper: ScopeMapperFactory = scope_mapper_factory or mysql_site_mapper

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
    ) -> ScopeContext:
        # ``third_session`` 对本解析器无意义：走到这里说明凭据是公司令牌，不是共享会话值。
        del third_session
        if not token or token.startswith("aops_"):
            raise CallerAuthError("invalid access token", code=CALLER_AUTH_INVALID)
        claims = self._claims(token)
        subject = _subject_from_claims(claims)
        # 形状校验先于入口分流：``shop_ids`` 的形状是上游契约的属性，与本次请求从哪个入口进来
        # 无关。一条形状不符的响应不该因为入口恰好是 consumer 就被当成「能用」。
        shop_ids = _shop_ids_from_claims(claims)
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


def _envelope_refused(claims: Mapping[str, Any]) -> bool:
    """公司是否**明确拒绝**了这条令牌。

    判据是「存在 ``code`` 键且该码不是 0/200」—— 合法令牌**没有** ``code``（见模块文档），
    而公司的拒绝（含 ``{"code":1,"data":"invalid_token"}``）有。``code`` 缺失 ⇒ 不是拒绝，
    继续按身份声明解读；解读阶段缺 ``id``/``tenant_id`` 会自己拒。

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
    )


def _shop_ids_from_claims(claims: Mapping[str, Any]) -> tuple[str, ...]:
    """令牌里的店铺集合（``shop_ids``），形状不符即拒绝。

    口径有意不对称，因为两者的信息来源不同：**键缺失**说明这条通道根本没给这个答案
    （客户端凭据令牌就没有身份、异常响应也不会带），拒；**值为 null / 空列表**说明上游明确答了
    「没有绑定店铺」—— 那是真实账号的合法状态（41 上验收账号即如此），交给共享解析产出**空
    范围**并记一条 ``no_shop_binding``（下游据此拒绝，见 §5 的站点绑定缺口）。
    """
    if "shop_ids" not in claims:
        raise CallerAuthError("company token is missing its shop ids", code=CALLER_AUTH_INVALID)
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
