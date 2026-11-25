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
- 仅有 ``organIds`` 而无法解析出任何店铺/站点时，最终站点集合为空（空证据）；
- 历史 Era 1 的 Dis 点位代查链路已随 ADR-0342 退役；若调用方仍传入非 self 的
  ``delegated`` 上下文，一律以 ``ScopeError`` fail closed，绝不扩大查询范围。

运营商范围解析（``resolve_operator_site_scope``）是同一层里的另一条入链：后端权威
授权所用的同一个 UPMS 端点给出 B 端主体的店铺集合，经充电库既有站点归属映射得到
站点集合。管家端会话（``third_session_auth``）用它替换自己的 ``self`` 兜底范围，
订单授权判定（``caller_auth.ScopedOrderAuthorizer``）与证据收集因此共用同一个范围。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from aiops_diagnostics.common.config import UpmsSettings
from aiops_diagnostics.identity.scope_context import (
    SCOPE_ERROR_EMPTY_SCOPE,
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_SELF,
    ScopeContext,
    ScopeError,
    UpmsDirectory,
)

_LOGGER = logging.getLogger(__name__)

SCOPE_ERROR_SCOPE_TOO_LARGE = "scope.too_large"

#: 运营商站点范围解析不出可见站点时的可区分原因（只写日志，不含任何身份标识）。
#: 两者表象都是「空集合」，运维含义相反：一个是账号漏登记（应补绑定），一个是
#: 已登记店铺在充电库里没有站点（数据缺口）。
OPERATOR_SCOPE_NO_SHOP_BINDING = "no_shop_binding"
OPERATOR_SCOPE_SHOP_WITHOUT_SITE = "shop_without_site"

#: 单个范围的站点/店铺 ID 数量上限；超过说明数据范围异常，fail closed。
MAX_SCOPE_IDS = 1000


@dataclass(frozen=True, slots=True)
class QueryScope:
    """一次诊断运行内冻结的数据库查询范围。

    ``site_ids`` 为 ``None`` 表示租户内不限站点（all 范围）；
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


@dataclass(frozen=True, slots=True)
class _StaticMapper:
    """离线/测试用站点归属映射。"""

    sites_by_shop: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def site_ids_by_shops(self, shop_ids: tuple[str, ...], tenant_id: str) -> tuple[str, ...]:
        result: list[str] = []
        for shop_id in shop_ids:
            result.extend(self.sites_by_shop.get(shop_id, ()))
        return tuple(dict.fromkeys(result))


def static_site_mapper(
    *,
    sites_by_shop: dict[str, tuple[str, ...]] | None = None,
) -> SiteScopeMapper:
    """构造内存站点归属映射，供离线 fixture 与测试使用。"""
    return _StaticMapper(sites_by_shop=dict(sites_by_shop or {}))


def resolve_query_scope(
    context: ScopeContext,
    *,
    mapper: SiteScopeMapper | None = None,
) -> QueryScope:
    """把 ``ScopeContext`` 解析为冻结的 ``QueryScope``。

    依赖失败或配置缺失时 fail closed，不会触发 MySQL 查询。
    """
    tenant_id = context.effective_tenant_id
    data_scope = context.data_scope

    if len(data_scope.site_ids) > MAX_SCOPE_IDS or len(data_scope.shop_ids) > MAX_SCOPE_IDS:
        raise ScopeError(f"数据范围 ID 数量超过上限 {MAX_SCOPE_IDS}", code=SCOPE_ERROR_SCOPE_TOO_LARGE)

    if data_scope.type == SCOPE_TYPE_SELF:
        # self 范围 = 仅本人记录，按主体 C 端用户过滤。
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
        raise ScopeError(
            "代查目标站点范围解析已退役，拒绝扩大查询范围",
            code=SCOPE_ERROR_EMPTY_SCOPE,
        )

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

    本函数只负责**第一步取店铺集合**；「店铺集合 → 站点范围」的规则（两个陷阱与
    两种空集合）在 ``operator_site_scope_from_shops`` 里，只有一份。

    唯一的陷阱在这里：入参是 B 端 ``SysUser.id``（会话的 C→B 映射产出），不是会话
    的 C 端 ``userId``。两者实测无重叠，传错只会得到空集而非他人范围，但那仍是类型
    错误，不能依赖「当前恰好不重叠」。因此调用方必须先确认会话身份唯一确定了 B 端
    主体——#424 的占位 ``b_user_id``（``c:`` 前缀）绝不能传进来。
    """
    shop_ids = shops.shop_ids_by_b_user_id(b_user_id)
    return operator_site_scope_from_shops(shop_ids, tenant_id, mapper=mapper)


def operator_site_scope_from_shops(
    shop_ids: tuple[str, ...],
    tenant_id: str,
    *,
    mapper: SiteScopeMapper,
) -> QueryScope:
    """店铺集合 → 运营商站点范围（#425 的第三步，与店铺集合**来源无关**）。

    从 ``resolve_operator_site_scope`` 里提取的返回段：**店铺集合从哪来**是
    调用方的事（运营商账号查 ``/shopuser/getShops``；管家端 OAuth2 会话直接取
    令牌里的 ``shop_ids``），**拿到之后怎么算**只有一份实现 —— 少一个来源就多抄
    一遍规则，而这份规则里有两条踩过坑的约束：

    1. **店铺 id ≠ 站点 id**：真实数据 954 行站点中 ``ch_site.id ≡ ch_site.shop_id``
       的占 953 行、1 行不同。必须经既有 ``site_ids_by_shops``
       （``ch_site.shop_id → ch_site.id``）映射，直接互用只在例外行上出错。
    2. **两种空集合都不放行**，且各记一条可区分原因：未绑定店铺（账号漏登记）
       与店铺在充电库里没有站点（数据缺口）。数量超界或上游不可达时以带错误码的
       ``ScopeError`` 失败关闭。

    跨库字符集冲突在本链路不出现（站点集合来自充电库单库查询，无跨库 JOIN）。
    """
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
