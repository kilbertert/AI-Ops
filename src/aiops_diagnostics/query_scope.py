"""权限上下文到数据库查询范围的解析（单次诊断运行专用）。

``ScopeContext``（T1）给出调用者、目标主体、有效租户与业务数据范围；本模块把它
解析成 MySQL/TDengine/Redis 查询可直接下推的不可变 ``QueryScope``：

- ``tenant_id``：有效租户，永远作为 SQL 过滤条件，不信任前端裸传的租户参数；
- ``site_ids``：``None`` 表示 all 范围（租户内不限站点），``()`` 表示最终可见
  站点为空（查询短路返回空证据，不发起数据库请求）；
- ``user_id``：self 范围时目标主体的 C 端用户过滤（``ch_order_info.user_id``、
  ``ch_occupy_order_info.userId``）。

站点集合的来源与 PRD #23 的平台规则一致：

- UPMS 数据范围的 ``siteIds`` 直接进入站点集合；
- ``shopIds`` 通过充电库 ``ch_site.shop_id`` 归属解析为站点；
- 代查目标主体时，Dis 点位归属（``/dis/admin/device/point/{uid}/device/points``
  → ``ch_site.dis_point_id``）解析目标用户的站点，并与调用者自身的站点范围取
  交集；交集为空时返回空范围，不扩大查询范围；
- 仅有 ``organIds`` 而无法解析出任何店铺/站点时，最终站点集合为空（空证据）。

Dis 不可用、代查缺少 Dis 配置或范围 ID 超过上限时以带错误码的 ``ScopeError``
fail closed，且不会触发任何 MySQL 查询（解析先于数据源查询完成）。

运营商范围解析（``resolve_operator_site_scope``）是同一层里的另一条入链：后端权威
授权所用的同一个 UPMS 端点给出 B 端主体的店铺集合，经充电库既有站点归属映射得到
站点集合。管家端会话（``third_session_auth``）用它替换自己的 ``self`` 兜底范围，
订单授权判定（``caller_auth.ScopedOrderAuthorizer``）与证据收集因此共用同一个范围。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from aiops_diagnostics.bounded_http import (
    JSON_CONTENT_TYPE,
    ErrorMapping,
    RequestSpec,
    join_url,
    parse_raw_envelope,
    request_json,
)
from aiops_diagnostics.config import DisSettings, UpmsSettings
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_EMPTY_SCOPE,
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_SELF,
    ScopeContext,
    ScopeError,
    UpmsDirectory,
)

_LOGGER = logging.getLogger(__name__)

SCOPE_ERROR_DIS_CONFIG_MISSING = "scope.dis_config_missing"
SCOPE_ERROR_DIS_UNAVAILABLE = "scope.dis_unavailable"
SCOPE_ERROR_DIS_AUTH_FAILED = "scope.dis_auth_failed"
SCOPE_ERROR_SCOPE_TOO_LARGE = "scope.too_large"

#: 运营商站点范围解析不出可见站点时的可区分原因（只写日志，不含任何身份标识）。
#: 两者表象都是「空集合」，运维含义相反：一个是账号漏登记（应补绑定），一个是
#: 已登记店铺在充电库里没有站点（数据缺口）。
OPERATOR_SCOPE_NO_SHOP_BINDING = "no_shop_binding"
OPERATOR_SCOPE_SHOP_WITHOUT_SITE = "shop_without_site"

#: 单个范围的站点/店铺/点位 ID 数量上限；超过说明数据范围异常，fail closed。
MAX_SCOPE_IDS = 1000

DIS_POINT_BY_USER_PATH = "/dis/admin/device/point/{user_id}/device/points"

_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclass(frozen=True, slots=True)
class QueryScope:
    """一次诊断运行内冻结的数据库查询范围。

    ``site_ids`` 为 ``None`` 表示租户内不限站点（all 范围且无代查收窄）；
    ``()`` 表示可见站点为空，所有范围查询短路返回空结果，不发起 SQL。
    """

    tenant_id: str
    site_ids: tuple[str, ...] | None
    user_id: str | None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("QueryScope 缺少有效租户")
        if self.site_ids is not None and len(self.site_ids) > MAX_SCOPE_IDS:
            raise ScopeError(f"范围站点数量超过上限 {MAX_SCOPE_IDS}", code=SCOPE_ERROR_SCOPE_TOO_LARGE)

    @property
    def empty_site_scope(self) -> bool:
        return self.site_ids == ()

    def audit_summary(self) -> dict[str, Any]:
        """审计摘要：范围语义，不含任何凭证或权限副本。"""
        return {
            "tenant_id": self.tenant_id,
            "site_ids": list(self.site_ids) if self.site_ids is not None else None,
            "user_id": self.user_id,
            "site_scope_empty": self.empty_site_scope,
        }


class DisDirectory(Protocol):
    """Dis 点位归属解析接缝：C 端用户 → 授权点位 ID 集合。"""

    def point_ids_for_user(self, c_user_id: str, tenant_id: str) -> tuple[str, ...]: ...


class DisHttpDirectory:
    """``DisDirectory`` 的 Dis HTTP 实现。

    复用充电桩 Java ``DisFeignClient`` 的既有契约：``Authorization`` 使用服务侧
    配置的 Dis 令牌（不是用户凭证），并携带 ``tenantId``/``site``/``saasType``
    头。令牌只存在于服务端配置，绝不进入 ``QueryScope``、审计摘要或错误消息。
    """

    def __init__(self, settings: DisSettings) -> None:
        self.settings = settings

    def point_ids_for_user(self, c_user_id: str, tenant_id: str) -> tuple[str, ...]:
        base_url = self.settings.base_url
        token = self.settings.token
        if not base_url or not token:
            raise ScopeError("Dis 服务未配置", code=SCOPE_ERROR_DIS_CONFIG_MISSING)
        path = DIS_POINT_BY_USER_PATH.format(user_id=_safe_path_segment(c_user_id))
        # The Dis service token is a service-side credential, treated like every
        # other declared auth scheme; it never enters QueryScope, audit
        # summaries or error messages. The envelope is parsed here (raw), not by
        # the skeleton, because this client inspects ``code`` and ``data``
        # itself.
        payload = request_json(
            RequestSpec(
                url=join_url(base_url, path),
                method="GET",
                headers={
                    "Authorization": token,
                    "tenantId": _clean_header(tenant_id),
                    "site": _clean_header(tenant_id),
                    "saasType": "STANDARD",
                    "Accept": JSON_CONTENT_TYPE,
                },
                timeout=self.settings.timeout_seconds,
            ),
            mapping=ErrorMapping(
                auth_rejected=lambda _f: ScopeError("Dis 拒绝服务令牌", code=SCOPE_ERROR_DIS_AUTH_FAILED),
                http_error=lambda f: ScopeError(
                    f"Dis 请求失败: HTTP {f.status}", code=SCOPE_ERROR_DIS_UNAVAILABLE
                ),
                unavailable=lambda f: ScopeError(
                    f"Dis 请求失败: {f.detail}", code=SCOPE_ERROR_DIS_UNAVAILABLE
                ),
                invalid_body=lambda f: ScopeError(
                    f"Dis 请求失败: {f.detail}", code=SCOPE_ERROR_DIS_UNAVAILABLE
                ),
                invalid_envelope=lambda _f: ScopeError(
                    "Dis 请求失败: invalid response", code=SCOPE_ERROR_DIS_UNAVAILABLE
                ),
            ),
            envelope=parse_raw_envelope,
        )
        if not isinstance(payload, dict) or payload.get("code") not in (0, 200):
            detail = payload.get("msg") if isinstance(payload, dict) else "invalid response"
            raise ScopeError(f"Dis 拒绝查询: {detail}", code=SCOPE_ERROR_DIS_UNAVAILABLE)
        data = payload.get("data")
        if data is None:
            return ()
        if not isinstance(data, list):
            raise ScopeError("Dis 点位响应无效", code=SCOPE_ERROR_DIS_UNAVAILABLE)
        point_ids: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                raise ScopeError("Dis 点位响应无效", code=SCOPE_ERROR_DIS_UNAVAILABLE)
            text = str(item.get("pointId") or "").strip()
            if text:
                point_ids.append(text)
        if len(point_ids) > MAX_SCOPE_IDS:
            raise ScopeError(f"Dis 返回点位数量超过上限 {MAX_SCOPE_IDS}", code=SCOPE_ERROR_SCOPE_TOO_LARGE)
        return tuple(point_ids)


class ShopDirectory(Protocol):
    """运营商店铺归属解析接缝：B 端 ``SysUser.id`` → 店铺 ID 集合。

    实现复用后端权威授权所用的同一个 UPMS 端点（``/shopuser/getShops``，即
    ``ShopIdInterceptor``/``@ShopDataScope`` 取隔离集合的那个来源），不在系统里
    保留第二套事实来源。未绑定任何店铺时返回**空集合**。
    """

    def shop_ids_by_b_user_id(self, b_user_id: str) -> tuple[str, ...]: ...


class UpmsShopDirectory:
    """``ShopDirectory`` 的 UPMS 实现（复用后端权威授权所用的同一端点）。

    ``credential`` 是服务侧配置的内部调用凭据，只透传给 UPMS，绝不进入
    ``QueryScope``、审计摘要或错误消息。
    """

    def __init__(self, settings: UpmsSettings, credential: str) -> None:
        self._directory = UpmsDirectory(settings)
        self._credential = credential

    def shop_ids_by_b_user_id(self, b_user_id: str) -> tuple[str, ...]:
        return self._directory.shop_ids_by_b_user_id(self._credential, b_user_id)


class SiteScopeMapper(Protocol):
    """充电库内的站点归属解析接缝（``ch_site``）。

    由 ``MySQLSource`` 实现；解析发生在证据查询之前，实现必须是只读、参数绑定
    且有界的查询。
    """

    def site_ids_by_shops(self, shop_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]: ...

    def site_ids_by_points(self, point_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class _StaticMapper:
    """离线/测试用站点归属映射。"""

    sites_by_shop: dict[str, tuple[str, ...]] = field(default_factory=dict)
    sites_by_point: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def site_ids_by_shops(self, shop_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        result: list[str] = []
        for shop_id in shop_ids:
            result.extend(self.sites_by_shop.get(shop_id, ()))
        return tuple(dict.fromkeys(result))

    def site_ids_by_points(self, point_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        result: list[str] = []
        for point_id in point_ids:
            result.extend(self.sites_by_point.get(point_id, ()))
        return tuple(dict.fromkeys(result))


def static_site_mapper(
    *,
    sites_by_shop: dict[str, tuple[str, ...]] | None = None,
    sites_by_point: dict[str, tuple[str, ...]] | None = None,
) -> SiteScopeMapper:
    """构造内存站点归属映射，供离线 fixture 与测试使用。"""
    return _StaticMapper(
        sites_by_shop=dict(sites_by_shop or {}),
        sites_by_point=dict(sites_by_point or {}),
    )


def resolve_query_scope(
    context: ScopeContext,
    *,
    dis: DisDirectory | None = None,
    mapper: SiteScopeMapper | None = None,
) -> QueryScope:
    """把 ``ScopeContext`` 解析为冻结的 ``QueryScope``。

    解析顺序：先调用者数据范围（UPMS），再代查目标的 Dis 点位归属，最后合并为
    交集。任一依赖失败或配置缺失时 fail closed，不会触发 MySQL 查询。
    """
    tenant_id = context.effective_tenant_id
    data_scope = context.data_scope

    if len(data_scope.site_ids) > MAX_SCOPE_IDS or len(data_scope.shop_ids) > MAX_SCOPE_IDS:
        raise ScopeError(f"数据范围 ID 数量超过上限 {MAX_SCOPE_IDS}", code=SCOPE_ERROR_SCOPE_TOO_LARGE)

    if data_scope.type == SCOPE_TYPE_SELF:
        # self 范围 = 仅本人记录，按主体 C 端用户过滤；站点过滤无意义且不发起 Dis。
        user_id = context.subject.c_user_id
        if not user_id:
            raise ScopeError(
                "self 业务数据范围但主体未绑定 C 端用户，无法限定查询",
                code=SCOPE_ERROR_EMPTY_SCOPE,
            )
        return QueryScope(tenant_id=tenant_id, site_ids=None, user_id=user_id)

    sites: set[str] | None = None
    if data_scope.type != SCOPE_TYPE_ALL:
        sites = set(data_scope.site_ids)
        if data_scope.shop_ids and mapper is not None:
            sites.update(_sites_from_shops(mapper, data_scope.shop_ids, tenant_id))

    if context.delegated and context.subject.c_user_id:
        if dis is None or mapper is None:
            raise ScopeError(
                "代查目标需要 Dis 与站点归属解析，但目录未配置",
                code=SCOPE_ERROR_DIS_CONFIG_MISSING,
            )
        point_ids = dis.point_ids_for_user(context.subject.c_user_id, tenant_id)
        _require_bounded(len(point_ids), "Dis 点位")
        target_sites = set(mapper.site_ids_by_points(point_ids, tenant_id))
        _require_bounded(len(target_sites), "目标站点")
        sites = target_sites if sites is None else sites & target_sites

    return QueryScope(
        tenant_id=tenant_id,
        site_ids=None if sites is None else tuple(sorted(sites)),
        user_id=None,
    )


def resolve_operator_site_scope(
    b_user_id: str,
    tenant_id: str,
    *,
    shops: ShopDirectory,
    mapper: SiteScopeMapper,
) -> QueryScope:
    """运营商站点范围：B 端主体 → 店铺集合（UPMS）→ 站点集合（充电库归属）。

    管家端订单授权（PRD #423）的可见范围判据：运营商在数据上表现为**一组站点**，
    因此范围以站点集合表达，可经既有 ``site_ids`` 机制下推。管家端会话用它替换
    ``self`` 兜底范围（#426），订单授权判定与证据收集因此共用这一个范围。

    三个已识别陷阱中的两个在这里处置（第三个是改范围类型会触发代查分支，属于
    查询路径）：

    1. **id 空间**：入参是 B 端 ``SysUser.id``（会话的 C→B 映射产出），不是会话的
       C 端 ``userId``。两者实测无重叠，传错只会得到空集而非他人范围，但那仍是
       类型错误，不能依赖「当前恰好不重叠」。因此调用方必须先确认会话身份唯一
       确定了 B 端主体——#424 的占位 ``b_user_id``（``c:`` 前缀）绝不能传进来。
    2. **店铺 id ≠ 站点 id**：真实数据 954 行站点中 ``ch_site.id ≡ ch_site.shop_id``
       的占 953 行、1 行不同。因此店铺集合**必须**经既有 ``site_ids_by_shops``
       （``ch_site.shop_id → ch_site.id``）映射，直接互用会在例外行上漏算/多算，
       而另外 953 行上恰好是对的——错误不会自己暴露。

    两种空集合都不放行：未绑定店铺（账号漏登记）与店铺在充电库里没有站点（数据
    缺口）都返回空范围并各记一条可区分原因；店铺/站点数量超界或上游不可达/响应
    形状非法时以带错误码的 ``ScopeError`` 失败关闭。

    跨库字符集冲突在本链路不出现（店铺集合来自 UPMS HTTP，站点集合来自充电库单库
    查询，无跨库 JOIN）。若将来改为把店铺集合直接送进 SQL 与 ``ch_site`` 比对，
    必须先显式统一排序规则，且不得把报错当成"查无此行"。
    """
    shop_ids = shops.shop_ids_by_b_user_id(b_user_id)
    _require_bounded(len(shop_ids), "运营商店铺")
    if not shop_ids:
        # 代理商账号本身未绑定任何店铺：空集合并记录供运营补登记，不回落为租户级放行。
        _log_operator_scope(OPERATOR_SCOPE_NO_SHOP_BINDING, shop_count=0)
        return QueryScope(tenant_id=tenant_id, site_ids=(), user_id=None)
    site_ids = _sites_from_shops(mapper, shop_ids, tenant_id)
    _require_bounded(len(site_ids), "运营商站点")
    if not site_ids:
        # 已登记店铺在充电库里没有对应站点：与前一种空集合成因不同，同样失败关闭。
        _log_operator_scope(OPERATOR_SCOPE_SHOP_WITHOUT_SITE, shop_count=len(shop_ids))
    return QueryScope(tenant_id=tenant_id, site_ids=site_ids, user_id=None)


def _log_operator_scope(reason: str, *, shop_count: int) -> None:
    """记录空集合的成因：不含用户 id、租户与凭据，凭它区分「漏登记」与「数据缺口」。"""
    _LOGGER.info(
        "operator_site_scope_empty reason=%s shop_count=%d",
        reason,
        shop_count,
    )


def _sites_from_shops(mapper: SiteScopeMapper, shop_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
    """店铺集合 → 有界、去重、排序的站点集合（店铺 id ≠ 站点 id，必须经归属映射）。

    ``resolve_query_scope`` 的 organ 分支与 ``resolve_operator_site_scope`` 共用：
    一条规则（翻译 + 定序 + 上限）只写一次，改上限或排序时不需要找两处。
    """
    site_ids = tuple(sorted(mapper.site_ids_by_shops(tuple(shop_ids), tenant_id)))
    _require_bounded(len(site_ids), "店铺归属")
    return site_ids


def _require_bounded(count: int, label: str) -> None:
    if count > MAX_SCOPE_IDS:
        raise ScopeError(f"{label}数量超过上限 {MAX_SCOPE_IDS}", code=SCOPE_ERROR_SCOPE_TOO_LARGE)


def _safe_path_segment(value: str) -> str:
    """Whitelist user identifiers before they are interpolated into URL paths."""
    text = (value or "").strip()
    if not _SAFE_PATH_SEGMENT.fullmatch(text) or not text.strip("."):
        raise ValueError("用户 ID 包含不允许的字符")
    return text


def _clean_header(value: str) -> str:
    """Strip characters that could fold or inject additional HTTP headers."""
    text = (value or "").strip()
    if not text or any(char in text for char in ("\r", "\n", ":")):
        raise ValueError("租户标识包含不允许的字符")
    return text
