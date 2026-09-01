from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Runtime:
    def shutdown(self) -> None:
        pass


class _Resolver:
    def __init__(self, context: ScopeContext | Exception) -> None:
        self.context = context
        self.tokens: list[str] = []

    def resolve(self, token: str, *, required_scope: str) -> ScopeContext:
        self.tokens.append(token)
        if isinstance(self.context, Exception):
            raise self.context
        assert required_scope == "aiops:orders:read"
        return self.context


class _Orders:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str]] = []

    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        self.calls.append((context.scope_fingerprint, order_no))
        return order_no in self.allowed


def _scope() -> ScopeContext:
    subject = SubjectRecord(
        b_user_id="B-1",
        c_user_id="C-1",
        tenant_id="TENANT-1",
    )
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="TENANT-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:orders:read"}),
    )


def _client(tmp_path: Path, resolver: _Resolver, orders: _Orders) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=GatewayStore(settings.database_file),
            runtime=_Runtime(),  # type: ignore[arg-type]
            caller_resolver=resolver,
            order_authorizer=orders,
        )
    )


def test_standard_order_access_uses_verified_scope(tmp_path: Path) -> None:
    resolver = _Resolver(_scope())
    orders = _Orders({"O-ALLOW"})

    with _client(tmp_path, resolver, orders) as client:
        response = client.get(
            "/v1/orders/O-ALLOW/access",
            headers={"Authorization": "Bearer access-token"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "order_no": "O-ALLOW",
        "accessible": True,
        "scope_fingerprint": _scope().scope_fingerprint,
    }
    assert resolver.tokens == ["access-token"]
    assert orders.calls == [(_scope().scope_fingerprint, "O-ALLOW")]


def test_standard_order_access_hides_forbidden_and_missing_orders(tmp_path: Path) -> None:
    resolver = _Resolver(_scope())
    orders = _Orders(set())

    with _client(tmp_path, resolver, orders) as client:
        response = client.get(
            "/v1/orders/O-OTHER/access",
            headers={"Authorization": "Bearer access-token"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "ORDER_NOT_FOUND",
            "message": "order not found",
            "retryable": False,
        }
    }


def test_standard_order_access_rejects_missing_and_device_tokens(tmp_path: Path) -> None:
    resolver = _Resolver(_scope())
    orders = _Orders({"O-ALLOW"})

    with _client(tmp_path, resolver, orders) as client:
        missing = client.get("/v1/orders/O-ALLOW/access")
        device = client.get(
            "/v1/orders/O-ALLOW/access",
            headers={"Authorization": "Bearer aops_device-token"},
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "ACCESS_TOKEN_REQUIRED"
    assert device.status_code == 401
    assert device.json()["error"]["code"] == "INVALID_ACCESS_TOKEN"
    assert orders.calls == []


def test_standard_order_access_rejects_invalid_order_before_query(tmp_path: Path) -> None:
    resolver = _Resolver(_scope())
    orders = _Orders(set())

    with _client(tmp_path, resolver, orders) as client:
        response = client.get(
            "/v1/orders/ORDER%20WITH%20SPACE/access",
            headers={"Authorization": "Bearer access-token"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ORDER_NO"
    assert orders.calls == []
