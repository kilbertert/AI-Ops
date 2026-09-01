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
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from aiops_diagnostics.config import DisSettings
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_EMPTY_SCOPE,
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_SELF,
    ScopeContext,
    ScopeError,
)

SCOPE_ERROR_DIS_CONFIG_MISSING = "scope.dis_config_missing"
SCOPE_ERROR_DIS_UNAVAILABLE = "scope.dis_unavailable"
SCOPE_ERROR_DIS_AUTH_FAILED = "scope.dis_auth_failed"
SCOPE_ERROR_SCOPE_TOO_LARGE = "scope.too_large"

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
        request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", method="GET")
        request.add_header("Authorization", token)
        request.add_header("tenantId", _clean_header(tenant_id))
        request.add_header("site", _clean_header(tenant_id))
        request.add_header("saasType", "STANDARD")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ScopeError("Dis 拒绝服务令牌", code=SCOPE_ERROR_DIS_AUTH_FAILED) from exc
            raise ScopeError(f"Dis 请求失败: HTTP {exc.code}", code=SCOPE_ERROR_DIS_UNAVAILABLE) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ScopeError(
                f"Dis 请求失败: {exc.__class__.__name__}", code=SCOPE_ERROR_DIS_UNAVAILABLE
            ) from exc
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
            shops = mapper.site_ids_by_shops(tuple(data_scope.shop_ids), tenant_id)
            _require_bounded(len(shops), "店铺归属")
            sites.update(shops)

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
