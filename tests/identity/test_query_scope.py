from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from aiops_diagnostics.identity.query_scope import (
    SCOPE_ERROR_SCOPE_TOO_LARGE,
    QueryScope,
    ScopeError,
    operator_site_scope_from_shops,
    resolve_query_scope,
    static_site_mapper,
)
from aiops_diagnostics.identity.scope_context import (
    SCOPE_ERROR_EMPTY_SCOPE,
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeContext,
    ScopePolicy,
    ScopeRequest,
    ScopeResolver,
    SubjectRecord,
    UserContext,
)

TENANT = "TENANT-A"


def _caller(tenant_id: str = TENANT, **overrides: Any) -> UserContext:
    data: dict[str, Any] = {
        "b_user_id": "B-CALLER-1",
        "c_user_id": "C-CALLER-1",
        "username": "ops.caller",
        "tenant_id": tenant_id,
    }
    data.update(overrides)
    return UserContext(
        subject=SubjectRecord(
            b_user_id=data["b_user_id"],
            c_user_id=data["c_user_id"],
            username=data["username"],
            tenant_id=data.get("tenant_id"),
        )
    )


def _scope(
    type_: str = SCOPE_TYPE_ORGAN,
    organ_ids: tuple[str, ...] = ("ORG-A-1",),
    shop_ids: tuple[str, ...] = ("SHOP-A-1",),
    site_ids: tuple[str, ...] = ("SITE-A-1",),
) -> DataScope:
    return DataScope(type=type_, organ_ids=organ_ids, shop_ids=shop_ids, site_ids=site_ids)


class _FakeDirectory:
    """T1 相同的内存 PlatformDirectory，仅返回构造好的用户上下文。"""

    def __init__(self, caller: UserContext) -> None:
        self.caller = caller

    def user_info(self, credential: str) -> UserContext:
        return self.caller

    def user_by_b_user_id(self, credential: str, b_user_id: str) -> SubjectRecord | None:
        return None

    def users_by_c_user_id(self, credential: str, c_user_id: str) -> tuple[SubjectRecord, ...]:
        return ()

    def data_scope(self, credential: str) -> DataScope:
        return _scope()


def _context(
    *,
    caller: UserContext | None = None,
    data_scope: DataScope | None = None,
    target_b_user_id: str | None = None,
    tenant_id: str | None = None,
    delegated_permissions: bool = False,
) -> ScopeContext:
    caller = caller or _caller()
    if delegated_permissions:
        caller = UserContext(
            subject=caller.subject,
            roles=caller.roles,
            permissions=caller.permissions | {"user:delegate:view"},
        )
    directory = _FakeDirectory(caller)

    class _DirectoryWithScope(_FakeDirectory):
        def data_scope(self, credential: str) -> DataScope:  # type: ignore[override]
            return data_scope if data_scope is not None else _scope()

    directory = _DirectoryWithScope(caller)

    if target_b_user_id:
        # 提供内存中的目标记录
        directory.user_by_b_user_id = lambda credential, b: SubjectRecord(  # type: ignore[method-assign]
            b_user_id=b,
            c_user_id="C-TARGET-2",
            username="merchant.target",
            tenant_id=TENANT,
        )

    resolver = ScopeResolver(
        directory,
        policy=ScopePolicy(delegated_lookup_permissions=frozenset({"user:delegate:view"})),
    )
    return resolver.resolve(
        ScopeRequest(
            credential="platform-token-ops-1",
            target_b_user_id=target_b_user_id,
            tenant_id=tenant_id,
        )
    )


def test_query_scope_resolves_plain_organ_scope() -> None:
    context = _context()
    scope = resolve_query_scope(context)

    assert scope.tenant_id == TENANT
    assert scope.site_ids == ("SITE-A-1",)
    assert scope.user_id is None
    assert not scope.empty_site_scope


def test_query_scope_expands_shops_through_site_mapper() -> None:
    context = _context()
    mapper = static_site_mapper(sites_by_shop={"SHOP-A-1": ("SITE-1", "SITE-2")})
    scope = resolve_query_scope(context, mapper=mapper)

    assert set(scope.site_ids) == {"SITE-A-1", "SITE-1", "SITE-2"}


def test_query_scope_all_scope_leaves_sites_unrestricted() -> None:
    context = _context(data_scope=_scope(type_=SCOPE_TYPE_ALL))
    scope = resolve_query_scope(context)

    assert scope.site_ids is None
    assert not scope.empty_site_scope


def test_query_scope_self_scope_carries_target_user_id() -> None:
    context = _context(
        data_scope=_scope(type_=SCOPE_TYPE_SELF, site_ids=()),
        target_b_user_id="B-TARGET-2",
        delegated_permissions=True,
    )
    scope = resolve_query_scope(context)

    assert scope.user_id == "C-TARGET-2"
    assert scope.site_ids is None  # self 范围按用户过滤，不引入站点依赖


def test_query_scope_empty_site_scope_is_marked_for_short_circuit() -> None:
    scope = QueryScope(tenant_id=TENANT, site_ids=(), user_id=None)

    assert scope.empty_site_scope


def test_query_scope_fails_closed_when_delegation_is_requested() -> None:
    """历史 Dis 点位代查已退役（ADR-0342）：无论目标是否绑定 C 端 ID，非 self 代查一律 fail closed。"""
    context = _context(
        target_b_user_id="B-TARGET-2",
        delegated_permissions=True,
    )

    with pytest.raises(ScopeError) as excinfo:
        resolve_query_scope(context)

    assert excinfo.value.code == SCOPE_ERROR_EMPTY_SCOPE

    without_c_user = ScopeContext(
        caller=context.caller,
        subject=SubjectRecord(b_user_id="B-TARGET-3", c_user_id=None, username="b.only", tenant_id=TENANT),
        delegated=True,
        effective_tenant_id=TENANT,
        data_scope=context.data_scope,
        roles=context.roles,
        permissions=context.permissions,
    )
    with pytest.raises(ScopeError) as excinfo_no_c:
        resolve_query_scope(without_c_user)

    assert excinfo_no_c.value.code == SCOPE_ERROR_EMPTY_SCOPE


def test_query_scope_intercept_keeps_the_immutable_contract() -> None:
    context = _context()
    scope = resolve_query_scope(context)

    with pytest.raises(FrozenInstanceError):
        scope.tenant_id = "TENANT-B"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        scope.site_ids = ("SITE-Y",)  # type: ignore[misc]


def test_query_scope_audit_summary_has_no_credentials() -> None:
    context = _context()
    scope = resolve_query_scope(context)

    payload = scope.audit_summary()
    assert payload["tenant_id"] == TENANT
    assert "token" not in str(payload).lower() or "platform-token" not in str(payload)


def test_query_scope_rejects_oversized_business_scope_ids() -> None:
    context = _context(data_scope=_scope(site_ids=tuple(f"S-{i}" for i in range(1001))))

    with pytest.raises(ScopeError) as excinfo:
        resolve_query_scope(context)

    assert excinfo.value.code == SCOPE_ERROR_SCOPE_TOO_LARGE


# --- #425 范围解析的接缝：店铺集合来源可换，映射规则只有一份 -------------------
#
# 提取 ``operator_site_scope_from_shops`` 的动机是 #441：管家端 OAuth2 会话的
# 店铺集合**来自令牌**（``shop_ids``），而运营商账号来自 ``/shopuser/getShops``。
# 两个来源必须共用同一份映射规则——多一份实现就多一处会漂移的地方。
# 这组用例只钉住「新接缝自己成立」，既有 ``resolve_operator_site_scope`` 的
# 用例（工厂/单站点/多站点/无绑定/无站点）原样保留，用来证明重构没有改变它的行为。


def test_the_shared_seam_pins_the_factory_case() -> None:
    """未绑定店铺（账号漏登记）⇒ 空范围，且这是**设计内**的失败关闭。"""
    scope = operator_site_scope_from_shops((), TENANT, mapper=static_site_mapper())
    assert scope.site_ids == ()


def test_the_shared_seam_maps_shops_through_the_ownership_table() -> None:
    """店铺 → 站点必须走归属映射，不能直接把店铺 id 当站点 id。"""
    mapper = static_site_mapper(sites_by_shop={"SHOP-1": ("SITE-A", "SITE-B")})
    scope = operator_site_scope_from_shops(("SHOP-1",), TENANT, mapper=mapper)
    assert scope.site_ids == ("SITE-A", "SITE-B")
    assert scope.tenant_id == TENANT


def test_the_shared_seam_keeps_a_shop_without_sites_empty() -> None:
    """店铺已登记但充电库里没有站点 ⇒ 同样空范围（与「没绑店铺」成因不同）。"""
    scope = operator_site_scope_from_shops(("SHOP-EMPTY",), TENANT, mapper=static_site_mapper())
    assert scope.site_ids == ()
