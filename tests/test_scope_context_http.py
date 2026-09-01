from __future__ import annotations

import json
import urllib.error
from typing import Any
from urllib.parse import urlsplit

import pytest

from aiops_diagnostics.config import UpmsSettings
from aiops_diagnostics.scope_context import (
    SCOPE_ERROR_AUTH_FAILED,
    SCOPE_ERROR_CONFIG_MISSING,
    SCOPE_ERROR_UPMS_UNAVAILABLE,
    DataScope,
    ScopeError,
    ScopePolicy,
    ScopeRequest,
    ScopeResolver,
    UpmsDirectory,
)

UPMS_BASE_URL = "https://upms.example.test"
CREDENTIAL = "platform-token-ops-1"


class _FakeResponse:
    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _FakeUpmsTransport:
    """Serve the documented UPMS endpoint contract and record every request."""

    def __init__(self, *, user_info: Any, data_scope: Any, by_id: Any = None, by_user_id: Any = None) -> None:
        self.user_info = user_info
        self.data_scope = data_scope
        self.by_id = by_id
        self.by_user_id = by_user_id
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: Any, timeout: int | None = None) -> _FakeResponse:
        split = urlsplit(request.full_url)
        self.requests.append((split.path, {name: value for name, value in request.header_items()}))
        if split.path == "/user/info":
            return _FakeResponse({"code": 0, "msg": "ok", "data": self.user_info})
        if split.path == "/user/ds":
            return _FakeResponse({"code": 0, "msg": "ok", "data": self.data_scope})
        if split.path.startswith("/user/inside/byId/"):
            user_id = split.path.rsplit("/", 1)[-1]
            record = self.by_id.get(user_id) if self.by_id else None
            return _FakeResponse({"code": 0, "msg": "ok", "data": record})
        if split.path.startswith("/user/inside/byUserId/"):
            return _FakeResponse({"code": 0, "msg": "ok", "data": self.by_user_id or []})
        return _FakeResponse({"code": 404, "msg": "not found", "data": None})


_USER_INFO: dict[str, Any] = {
    "sysUser": {
        "id": "B-CALLER-1",
        "userId": "C-CALLER-1",
        "username": "ops.caller",
        "tenantId": "TENANT-A",
        "organId": "ORG-A-1",
        "shopId": "SHOP-A-1",
    },
    "roles": [
        {"roleCode": "ROLE_OPS", "parentCodes": ["ROLE_BASE"]},
        {"roleCode": "ROLE_BASE"},
    ],
    "permissions": ["order:diag:view", "user:delegate:view"],
}

_DATA_SCOPE: dict[str, Any] = {
    "type": 1,
    "organIds": ["ORG-A-1"],
    "shopIds": ["SHOP-A-1"],
    "siteIds": ["SITE-A-1"],
}

_POLICY = ScopePolicy(
    delegated_lookup_permissions=frozenset({"user:delegate:view"}),
    tenant_admin_roles=frozenset({"ROLE_PLATFORM_ADMIN"}),
)


def _directory() -> UpmsDirectory:
    return UpmsDirectory(UpmsSettings(base_url=UPMS_BASE_URL, timeout_seconds=5))


def _resolver(transport: _FakeUpmsTransport, monkeypatch: pytest.MonkeyPatch) -> ScopeResolver:
    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", transport)
    return ScopeResolver(_directory(), policy=_POLICY)


def test_upms_directory_forwards_credential_and_resolves_context(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeUpmsTransport(user_info=_USER_INFO, data_scope=_DATA_SCOPE)
    resolver = _resolver(transport, monkeypatch)

    context = resolver.resolve(ScopeRequest(credential=CREDENTIAL))

    assert context.caller.b_user_id == "B-CALLER-1"
    assert context.caller.c_user_id == "C-CALLER-1"
    assert context.caller.username == "ops.caller"
    assert context.caller.tenant_id == "TENANT-A"
    assert context.caller.organ_id == "ORG-A-1"
    assert context.caller.shop_id == "SHOP-A-1"
    assert context.roles == frozenset({"ROLE_OPS", "ROLE_BASE"})
    assert context.permissions == frozenset({"order:diag:view", "user:delegate:view"})
    assert context.data_scope == DataScope(
        type="organ",
        organ_ids=("ORG-A-1",),
        shop_ids=("SHOP-A-1",),
        site_ids=("SITE-A-1",),
    )
    assert context.effective_tenant_id == "TENANT-A"
    for _path, headers in transport.requests:
        assert headers["Authorization"] == f"Bearer {CREDENTIAL}"


def test_upms_directory_maps_c_user_id_through_inside_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeUpmsTransport(
        user_info=_USER_INFO,
        data_scope=_DATA_SCOPE,
        by_user_id=[
            {
                "id": "B-TARGET-2",
                "userId": "C-TARGET-2",
                "username": "merchant.target",
                "tenantId": "TENANT-A",
            }
        ],
    )
    resolver = _resolver(transport, monkeypatch)

    context = resolver.resolve(ScopeRequest(credential=CREDENTIAL, target_c_user_id="C-TARGET-2"))

    assert context.delegated is True
    assert context.subject.b_user_id == "B-TARGET-2"
    assert context.subject.c_user_id == "C-TARGET-2"
    assert [path for path, _ in transport.requests] == [
        "/user/info",
        "/user/inside/byUserId/C-TARGET-2",
        "/user/ds",
    ]


def test_upms_directory_resolves_b_user_id_through_inside_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeUpmsTransport(
        user_info=_USER_INFO,
        data_scope=_DATA_SCOPE,
        by_id={
            "B-TARGET-2": {
                "id": "B-TARGET-2",
                "userId": "C-TARGET-2",
                "username": "merchant.target",
                "tenantId": "TENANT-A",
            }
        },
    )
    resolver = _resolver(transport, monkeypatch)

    context = resolver.resolve(ScopeRequest(credential=CREDENTIAL, target_b_user_id="B-TARGET-2"))

    assert context.subject.b_user_id == "B-TARGET-2"
    assert [path for path, _ in transport.requests] == [
        "/user/info",
        "/user/inside/byId/B-TARGET-2",
        "/user/ds",
    ]


def test_upms_directory_accepts_flat_role_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    user_info = dict(_USER_INFO)
    user_info["roles"] = ["ROLE_OPS"]
    transport = _FakeUpmsTransport(user_info=user_info, data_scope=_DATA_SCOPE)
    resolver = _resolver(transport, monkeypatch)

    context = resolver.resolve(ScopeRequest(credential=CREDENTIAL))

    assert context.roles == frozenset({"ROLE_OPS"})


def test_upms_directory_normalizes_data_scope_types(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw, expected in ((0, "all"), (4, "self"), ("all", "all"), ("self", "self"), ("organ", "organ")):
        transport = _FakeUpmsTransport(
            user_info=_USER_INFO, data_scope={"type": raw, "organIds": ["ORG-A-1"]}
        )
        resolver = _resolver(transport, monkeypatch)
        context = resolver.resolve(ScopeRequest(credential=CREDENTIAL))
        assert context.data_scope.type == expected, raw


def test_upms_directory_fails_closed_on_unreachable_service(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(request: Any, timeout: int | None = None) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", unavailable)

    with pytest.raises(ScopeError) as excinfo:
        _directory().user_info(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


def test_upms_directory_fails_closed_on_http_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(request: Any, timeout: int | None = None) -> _FakeResponse:
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", forbidden)

    with pytest.raises(ScopeError) as excinfo:
        _directory().user_info(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_AUTH_FAILED


def test_upms_directory_fails_closed_on_rejected_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeUpmsTransport(user_info=_USER_INFO, data_scope=_DATA_SCOPE)
    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", transport)

    def rejected(request: Any, timeout: int | None = None) -> _FakeResponse:
        return _FakeResponse({"code": 403, "msg": "令牌无效", "data": None})

    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", rejected)

    with pytest.raises(ScopeError) as excinfo:
        _directory().user_info(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_AUTH_FAILED


def test_upms_directory_fails_closed_on_malformed_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeUpmsTransport(user_info={"sysUser": {"id": ""}}, data_scope=_DATA_SCOPE)
    resolver = _resolver(transport, monkeypatch)

    with pytest.raises(ScopeError) as excinfo:
        resolver.resolve(ScopeRequest(credential=CREDENTIAL))

    assert excinfo.value.code == SCOPE_ERROR_UPMS_UNAVAILABLE


def test_upms_directory_fails_closed_without_base_url() -> None:
    directory = UpmsDirectory(UpmsSettings())

    with pytest.raises(ScopeError) as excinfo:
        directory.user_info(CREDENTIAL)

    assert excinfo.value.code == SCOPE_ERROR_CONFIG_MISSING


def test_upms_directory_rejects_path_injection_in_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeUpmsTransport(user_info=_USER_INFO, data_scope=_DATA_SCOPE)
    directory = _directory()
    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", transport)

    with pytest.raises(ValueError):
        directory.user_by_b_user_id(CREDENTIAL, "../user/info")
    with pytest.raises(ValueError):
        directory.users_by_c_user_id(CREDENTIAL, "C-1/../../x")
    assert transport.requests == []


def test_upms_directory_error_messages_never_leak_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(request: Any, timeout: int | None = None) -> _FakeResponse:
        raise urllib.error.URLError(f"boom {CREDENTIAL}")

    monkeypatch.setattr("aiops_diagnostics.scope_context.urllib.request.urlopen", unavailable)

    with pytest.raises(ScopeError) as excinfo:
        _directory().user_info(CREDENTIAL)

    assert CREDENTIAL not in str(excinfo.value)
