from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import CallerAuthError
from aiops_diagnostics.charging_status import ChargingStatusError, read_charging_status
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


def _scope(**kwargs):
    subject = SubjectRecord(b_user_id="", c_user_id="USER-A", tenant_id="TENANT-A")
    return ScopeContext.build(
        **{
            "caller": subject,
            "subject": subject,
            "delegated": False,
            "effective_tenant_id": "TENANT-A",
            "data_scope": DataScope(type="self"),
            "roles": frozenset(),
            "permissions": frozenset({"aiops:orders:read"}),
            **kwargs,
        }
    )


class Resolver:
    def __init__(self, context):
        self.context = context

    def resolve(self, token, *, required_scope, **kwargs):
        assert required_scope == "aiops:orders:read"
        if token != "verified-token":
            raise CallerAuthError("invalid", code="caller_auth.invalid")
        return self.context


class Orders:
    def __init__(self):
        self.calls = []
        self.unavailable = False

    def can_access(self, context, order_no):
        self.calls.append((context, order_no))
        if self.unavailable:
            raise CallerAuthError("unavailable", code="caller_auth.unavailable")
        return order_no == "ORDER-A"


class Runtime:
    def shutdown(self):
        pass


@pytest.fixture
def upstream():
    state = {
        "status": 200,
        "body": {
            "code": 0,
            "data": {
                "orderNo": "ORDER-A",
                "status": "0",
                "plateNo": "DO-NOT-RETURN",
                "userId": "USER-A",
            },
        },
        "calls": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["calls"].append((self.path, dict(self.headers)))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", f"http://127.0.0.1:{server.server_port}/redirect-target")
            self.end_headers()
            self.wfile.write(json.dumps(state["body"]).encode())

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state["base"] = f"http://127.0.0.1:{server.server_port}/charging-pile"
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _app(tmp_path, upstream, *, scope=None, enabled=True):
    config = tmp_path / "production.env"
    config.write_text("# synthetic verification only\n")
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
        charging_status_base_url=upstream["base"] if enabled else "",
        execution_environment="test-A",
    )
    orders = Orders()
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=Runtime(),
        caller_resolver=Resolver(scope or _scope()),
        order_authorizer=orders,
    )
    return app, orders


HEADERS = {"Authorization": "Bearer verified-token", "X-Business-Entry": "consumer"}
PATH = "/v1/orders/ORDER-A/charging-status"


def test_owned_order_uses_verified_identity_and_minimal_response(tmp_path, upstream):
    app, orders = _app(tmp_path, upstream)
    with TestClient(app) as client:
        response = client.get(PATH, headers={**HEADERS, "tenant-id": "EVIL", "user-id": "OTHER"})
    assert response.status_code == 200
    body = response.json()
    assert body["status_role"] == "charging" and body["status_code"] == "0"
    assert body["environment"] == "test-A" and body["gateway_version"]
    assert datetime.fromisoformat(body["observed_at"]).utcoffset().total_seconds() == 0
    assert set(body) == {
        "order_no",
        "status_code",
        "status_role",
        "observed_at",
        "source",
        "environment",
        "gateway_version",
    }
    assert len(orders.calls) == 1
    path, headers = upstream["calls"][0]
    assert path == "/charging-pile/chOrderInfo/chargingInfo/ORDER-A"
    assert headers["Tenant-Id"] == "TENANT-A" and headers["User-Id"] == "USER-A"


@pytest.mark.parametrize("order", ["OTHER-PERSON", "MISSING"])
def test_no_visible_order_never_calls_cpms(tmp_path, upstream, order):
    app, _ = _app(tmp_path, upstream)
    with TestClient(app) as client:
        response = client.get(f"/v1/orders/{order}/charging-status", headers=HEADERS)
    assert response.status_code == 404 and response.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert not upstream["calls"]


@pytest.mark.parametrize("scope", [_scope(data_scope=DataScope(type="all")), _scope(delegated=True)])
def test_non_consumer_scope_refuses_before_authorization(tmp_path, upstream, scope):
    app, orders = _app(tmp_path, upstream, scope=scope)
    with TestClient(app) as client:
        assert client.get(PATH, headers=HEADERS).status_code == 403
    assert not orders.calls and not upstream["calls"]


def test_authentication_operator_entry_and_disabled_route(tmp_path, upstream):
    app, orders = _app(tmp_path, upstream)
    with TestClient(app) as client:
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(PATH, headers={**HEADERS, "X-Business-Entry": "operator"}).status_code == 403
        # 缺 `X-Business-Entry` 也拒绝：这个头由前端自报，缺省成本人等于让一个漏发的头
        # 静默换成本人范围。同时 `OPERATOR` / ` consumer ` 要按规范化处理。
        assert client.get(PATH, headers={"Authorization": "Bearer verified-token"}).status_code == 403
        assert client.get(PATH, headers={**HEADERS, "X-Business-Entry": "OPERATOR"}).status_code == 403
        assert client.get(PATH, headers={**HEADERS, "X-Business-Entry": " consumer "}).status_code == 200
    # 被拒的四次一次都没问过授权，也没打上游；只有规范化后认下的那次走完全程。
    assert len(orders.calls) == 1 and len(upstream["calls"]) == 1
    app, _ = _app(tmp_path, upstream, enabled=False)
    with TestClient(app) as client:
        assert client.get(PATH, headers=HEADERS).status_code == 404


def test_authorization_outage_is_not_missing_order(tmp_path, upstream):
    app, orders = _app(tmp_path, upstream)
    orders.unavailable = True
    with TestClient(app) as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert not upstream["calls"]


@pytest.mark.parametrize(
    "status,role", [("1", "finished"), (5, "controlled_abnormal_end"), ("99", "unknown"), ("0.5", "unknown")]
)
def test_status_interpretation_uses_existing_roles(upstream, status, role):
    upstream["body"]["data"]["status"] = status
    result = read_charging_status(upstream["base"], "ORDER-A", tenant_id="TENANT-A", user_id="USER-A")
    assert result["status_code"] == status and result["status_role"] == role


@pytest.mark.parametrize(
    "body",
    [
        {"code": 0, "data": {"orderNo": "OTHER", "status": "0"}},
        {"code": 0, "data": {"orderNo": "ORDER-A", "status": True}},
        {"code": 0, "data": None},
        {"code": 500, "msg": "PRIVATE-UPSTREAM-DETAIL"},
    ],
)
def test_wrong_order_or_invalid_upstream_is_not_returned(tmp_path, upstream, body):
    upstream["body"] = body
    app, _ = _app(tmp_path, upstream)
    with TestClient(app) as client:
        response = client.get(PATH, headers=HEADERS)
    assert response.status_code == 502
    assert "PRIVATE-UPSTREAM-DETAIL" not in response.text


def test_redirect_does_not_forward_identity(upstream):
    upstream["status"] = 302
    with pytest.raises(ChargingStatusError):
        read_charging_status(upstream["base"], "ORDER-A", tenant_id="TENANT-A", user_id="USER-A")
    assert len(upstream["calls"]) == 1


@contextmanager
def _live_gateway(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


def test_gateway_over_real_http_on_temporary_root(tmp_path, upstream):
    app, _ = _app(tmp_path, upstream)
    with _live_gateway(app) as base, httpx.Client(base_url=base, trust_env=False) as client:
        assert client.get("/health").json()["business_mutations"] == "disabled"
        assert client.get(PATH).status_code == 401
        ok = client.get(PATH, headers=HEADERS)
        assert ok.status_code == 200 and ok.json()["status_role"] == "charging"
        assert client.get("/v1/orders/OTHER/charging-status", headers=HEADERS).status_code == 404
    assert len(upstream["calls"]) == 1
