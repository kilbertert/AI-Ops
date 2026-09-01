"""``/user/ds`` 平台缺陷降级推导的契约测试。

生产 UPMS 的 ``/user/ds`` 对所有用户 500（组织 ID 列表为空时生成非法
``IN ()`` SQL）。``UpmsDirectory.data_scope`` 在收到服务侧错误时必须降级为
「角色 ``dsType`` + ``dsScope`` + ``/shopuser/getShops``」推导，且凭证失败
时保持 fail closed，绝不在服务错误时静默扩大范围。
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from urllib.parse import urlsplit

import pytest

from aiops_diagnostics.config import UpmsSettings
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_AUTH_FAILED,
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_ORGAN,
    DataScope,
    ScopeError,
    UpmsDirectory,
)

UPMS_BASE_URL = "https://upms.example.test"
CREDENTIAL = "platform-token-ops-1"

_SYS_USER: dict[str, Any] = {
    "id": "B-CALLER-1",
    "userId": None,
    "username": "test.admin",
    "tenantId": "TENANT-A",
    "organId": "ORG-A-1",
    "roleIds": ["ROLE-1"],
}


def _user_info() -> dict[str, Any]:
    return {"code": 0, "msg": None, "data": {"sysUser": _SYS_USER, "roles": [], "permissions": []}}


def _roles(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return items


def _role(ds_type: Any, ds_scope: Any = None) -> dict[str, Any]:
    return {"id": "ROLE-1", "roleCode": "ROLE_SHOP_USER", "dsType": ds_type, "dsScope": ds_scope}


class _FakeTransport:
    """按路径分派响应；``/user/ds`` 可配置为服务错误。"""

    def __init__(
        self,
        *,
        ds_response: dict[str, Any],
        roles: list[dict[str, Any]],
        shops: Any = None,
        sys_user: dict[str, Any] | None = None,
    ) -> None:
        self.ds_response = ds_response
        self.roles = roles
        self.shops = shops if shops is not None else []
        self.sys_user = sys_user or _SYS_USER
        self.requests: list[str] = []

    def __call__(self, request: Any, timeout: int | None = None) -> Any:
        path = urlsplit(request.full_url).path
        self.requests.append(path)
        if path == "/user/ds":
            payload = self.ds_response
        elif path == "/user/info":
            payload = {
                "code": 0,
                "msg": None,
                "data": {"sysUser": self.sys_user, "roles": [], "permissions": []},
            }
        elif path == "/role/list":
            payload = self.roles
        elif path.startswith("/shopuser/getShops"):
            payload = {"code": 0, "msg": None, "data": self.shops}
        else:
            payload = {"code": 404, "msg": "not found", "data": None}
        body = json.dumps(payload).encode("utf-8")
        return _FakeResponse(body)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _directory(transport: _FakeTransport, monkeypatch: pytest.MonkeyPatch) -> UpmsDirectory:
    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", transport)
    return UpmsDirectory(UpmsSettings(base_url=UPMS_BASE_URL, timeout_seconds=5))


def test_ds_service_error_falls_back_to_role_all_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles=[_role(0)],
    )

    scope = _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert scope == DataScope(type=SCOPE_TYPE_ALL, organ_ids=(), shop_ids=(), site_ids=())
    assert "/role/list" in transport.requests
    assert "/shopuser/getShops" in transport.requests


def test_ds_fallback_own_child_level_uses_caller_organ(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles=[_role(2)],
        shops=["SHOP-A-1", "SHOP-A-2"],
    )

    scope = _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert scope.type == SCOPE_TYPE_ORGAN
    assert scope.organ_ids == ("ORG-A-1",)
    assert scope.shop_ids == ("SHOP-A-1", "SHOP-A-2")
    assert scope.site_ids == ()


def test_ds_fallback_custom_scope_parses_ds_scope_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles=[_role(1, "ORG-CUSTOM-1, ORG-CUSTOM-2")],
    )

    scope = _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert scope.type == SCOPE_TYPE_ORGAN
    assert scope.organ_ids == ("ORG-CUSTOM-1", "ORG-CUSTOM-2")


def test_ds_fallback_picks_widest_role_when_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles=[_role(3), {"id": "ROLE-1", "roleCode": "ROLE_EXTRA", "dsType": 0, "dsScope": None}],
    )

    scope = _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert scope.type == SCOPE_TYPE_ALL


def test_ds_fallback_ignores_roles_not_assigned_to_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles=[{"id": "ROLE-OTHER", "roleCode": "ROLE_SUPER", "dsType": 0, "dsScope": None}, _role(2)],
    )

    scope = _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert scope.type == SCOPE_TYPE_ORGAN
    assert scope.organ_ids == ("ORG-A-1",)


def test_ds_auth_failure_is_not_masked_by_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 401, "msg": "凭证失效", "data": None},
        roles=[_role(0)],
    )

    with pytest.raises(ScopeError) as excinfo:
        _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_AUTH_FAILED
    assert "/role/list" not in transport.requests


def test_ds_fallback_fails_closed_without_caller_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles=[_role(0)],
        sys_user={**_SYS_USER, "roleIds": []},
    )

    with pytest.raises(ScopeError) as excinfo:
        _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


def test_ds_fallback_fails_closed_on_unknown_ds_type(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles=[_role(9)],
    )

    with pytest.raises(ScopeError) as excinfo:
        _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


def test_ds_fallback_fails_closed_when_role_list_is_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={"code": 1, "msg": "系统错误！", "data": None},
        roles={"unexpected": "shape"},
    )

    with pytest.raises(ScopeError) as excinfo:
        _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


def test_ds_fallback_unreachable_upms_stays_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(request: Any, timeout: int | None = None) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", unavailable)
    directory = UpmsDirectory(UpmsSettings(base_url=UPMS_BASE_URL, timeout_seconds=5))

    with pytest.raises(ScopeError) as excinfo:
        directory.data_scope(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


def test_ds_success_path_unaffected_by_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport(
        ds_response={
            "code": 0,
            "msg": None,
            "data": {"type": 1, "organIds": ["ORG-A-1"], "shopIds": ["SHOP-A-1"], "siteIds": []},
        },
        roles=[_role(0)],
    )

    scope = _directory(transport, monkeypatch).data_scope(CREDENTIAL)

    assert scope.organ_ids == ("ORG-A-1",)
    assert scope.shop_ids == ("SHOP-A-1",)
    assert "/role/list" not in transport.requests
