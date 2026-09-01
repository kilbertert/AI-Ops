from __future__ import annotations

import urllib.error
from dataclasses import FrozenInstanceError
from typing import Any
from urllib.parse import urlsplit

import pytest

from aiops_diagnostics.config import DisSettings
from aiops_diagnostics.query_scope import (
    DIS_POINT_BY_USER_PATH,
    SCOPE_ERROR_DIS_AUTH_FAILED,
    SCOPE_ERROR_DIS_CONFIG_MISSING,
    SCOPE_ERROR_DIS_UNAVAILABLE,
    SCOPE_ERROR_SCOPE_TOO_LARGE,
    DisHttpDirectory,
    QueryScope,
    ScopeError,
    resolve_query_scope,
    static_site_mapper,
)
from aiops_diagnostics.scope_context import (
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


class _FakeDis:
    def __init__(self, point_ids: tuple[str, ...] = ()) -> None:
        self.point_ids = point_ids
        self.calls: list[tuple[str, str]] = []

    def point_ids_for_user(self, c_user_id: str, tenant_id: str) -> tuple[str, ...]:
        self.calls.append((c_user_id, tenant_id))
        return self.point_ids


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


def test_query_scope_all_scope_with_delegation_applies_dis_intersection() -> None:
    context = _context(
        data_scope=_scope(type_=SCOPE_TYPE_ALL),
        target_b_user_id="B-TARGET-2",
        delegated_permissions=True,
    )
    dis = _FakeDis(point_ids=("P-1", "P-2"))
    mapper = static_site_mapper(sites_by_point={"P-1": ("SITE-9",), "P-2": ("SITE-8",)})
    scope = resolve_query_scope(context, dis=dis, mapper=mapper)

    assert scope.site_ids == ("SITE-8", "SITE-9")
    assert dis.calls == [("C-TARGET-2", TENANT)]


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


def test_query_scope_fails_closed_when_delegation_needs_dis_but_none_configured() -> None:
    context = _context(
        target_b_user_id="B-TARGET-2",
        delegated_permissions=True,
    )

    with pytest.raises(ScopeError) as excinfo:
        resolve_query_scope(context)

    assert excinfo.value.code == SCOPE_ERROR_DIS_CONFIG_MISSING


def test_query_scope_merges_dis_target_sites_with_caller_scope() -> None:
    context = _context(target_b_user_id="B-TARGET-2", delegated_permissions=True)
    dis = _FakeDis(point_ids=("P-1",))
    mapper = static_site_mapper(sites_by_point={"P-1": ("SITE-A-1", "SITE-X")})
    scope = resolve_query_scope(context, dis=dis, mapper=mapper)

    assert scope.site_ids == ("SITE-A-1",)  # 交集去掉 SITE-X


def test_query_scope_intercept_keeps_the_immutable_contract() -> None:
    context = _context()
    scope = resolve_query_scope(context)

    with pytest.raises(FrozenInstanceError):
        scope.tenant_id = "TENANT-B"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        scope.site_ids = ("SITE-Y",)  # type: ignore[misc]


def test_query_scope_fails_closed_on_oversized_dis_point_set() -> None:
    context = _context(target_b_user_id="B-TARGET-2", delegated_permissions=True)
    dis = _FakeDis(point_ids=tuple(f"P-{i}" for i in range(1001)))
    mapper = static_site_mapper(sites_by_point={f"P-{i}": (f"S-{i}",) for i in range(1001)})

    with pytest.raises(ScopeError) as excinfo:
        resolve_query_scope(context, dis=dis, mapper=mapper)

    assert excinfo.value.code == SCOPE_ERROR_SCOPE_TOO_LARGE


def test_query_scope_audit_summary_has_no_credentials() -> None:
    context = _context()
    scope = resolve_query_scope(context)

    payload = scope.audit_summary()
    assert payload["tenant_id"] == TENANT
    assert "token" not in str(payload).lower() or "platform-token" not in str(payload)


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = payload
        self.status = 200

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _FakeDisTransport:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: Any, timeout: int | None = None) -> _FakeResponse:
        split = urlsplit(request.full_url)
        self.requests.append((split.path, {name: value for name, value in request.header_items()}))
        return _FakeResponse(self.payload if not isinstance(self.payload, bytes) else self.payload)


def _dis_directory() -> DisHttpDirectory:
    return DisHttpDirectory(
        DisSettings(base_url="https://dis.example.test", token="dis-service-token", timeout_seconds=5)
    )


def test_dis_http_directory_forwards_token_and_tenant_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeDisTransport(b'{"code":0,"msg":"ok","data":[{"pointId":"P-1"},{"pointId":"P-2"}]}')
    monkeypatch.setattr("aiops_diagnostics.query_scope.urllib.request.urlopen", transport)
    directory = _dis_directory()

    point_ids = directory.point_ids_for_user("C-TARGET-2", TENANT)

    assert point_ids == ("P-1", "P-2")
    path, headers = transport.requests[0]
    assert path == DIS_POINT_BY_USER_PATH.format(user_id="C-TARGET-2")
    folded = {key.casefold(): value for key, value in headers.items()}
    assert folded["authorization"] == "dis-service-token"
    assert folded["tenantid"] == TENANT
    assert folded["site"] == TENANT
    assert folded["saastype"] == "STANDARD"


def test_dis_http_directory_fails_closed_on_http_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(request: Any, timeout: int | None = None) -> _FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr("aiops_diagnostics.query_scope.urllib.request.urlopen", forbidden)

    with pytest.raises(ScopeError) as excinfo:
        _dis_directory().point_ids_for_user("C-TARGET-2", TENANT)

    assert excinfo.value.code == SCOPE_ERROR_DIS_AUTH_FAILED


def test_dis_http_directory_fails_closed_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(request: Any, timeout: int | None = None) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("aiops_diagnostics.query_scope.urllib.request.urlopen", unreachable)

    with pytest.raises(ScopeError) as excinfo:
        _dis_directory().point_ids_for_user("C-TARGET-2", TENANT)

    assert excinfo.value.code == SCOPE_ERROR_DIS_UNAVAILABLE


def test_dis_http_directory_fails_closed_without_config() -> None:
    directory = DisHttpDirectory(DisSettings())

    with pytest.raises(ScopeError) as excinfo:
        directory.point_ids_for_user("C-TARGET-2", TENANT)

    assert excinfo.value.code == SCOPE_ERROR_DIS_CONFIG_MISSING


def test_dis_http_directory_rejects_path_injection_in_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aiops_diagnostics.query_scope.urllib.request.urlopen",
        _FakeDisTransport(b"{}"),
    )

    with pytest.raises(ValueError):
        _dis_directory().point_ids_for_user("../user/info", TENANT)


def test_query_scope_rejects_oversized_business_scope_ids() -> None:
    context = _context(data_scope=_scope(site_ids=tuple(f"S-{i}" for i in range(1001))))

    with pytest.raises(ScopeError) as excinfo:
        resolve_query_scope(context)

    assert excinfo.value.code == SCOPE_ERROR_SCOPE_TOO_LARGE
