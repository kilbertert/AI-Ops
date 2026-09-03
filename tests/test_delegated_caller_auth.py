from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_INVALID,
    CALLER_AUTH_UNAVAILABLE,
    CallerAuthError,
    DelegatedCallerResolver,
    DelegationSettings,
)
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import ScopeContext


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _settings() -> DelegationSettings:
    return DelegationSettings(
        redemption_url="https://bff.example.test/internal/aiops/delegations/redeem",
        caller_token="java-to-aiops-secret",
        redemption_token="aiops-to-java-secret",
        audience="aiops-api",
        service_id="java-bff",
    )


def _active(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "active": True,
        "audience": "aiops-api",
        "purpose": "aiops:orders:read",
        "subject": {"user_id": "C-1", "tenant_id": "TENANT-1"},
    }
    payload.update(overrides)
    return payload


def test_delegated_health_report_uses_redeemed_user_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests = []

    def redeem(request, **_kwargs):
        requests.append(request)
        return _Response(_active())

    monkeypatch.setattr("aiops_diagnostics.caller_auth.urllib.request.urlopen", redeem)
    resolver = DelegatedCallerResolver(_settings())

    class Orders:
        def can_access(self, context: ScopeContext, order_no: str) -> bool:
            return context.subject.c_user_id == "C-1" and order_no == "O-1"

    class Runtime:
        def __init__(self, store: GatewayStore) -> None:
            self.store = store

        def start_health_report(self, context: ScopeContext, order_no: str):
            return self.store.create_or_reuse_health_job(context.scope_fingerprint, order_no, "health-v1")[0]

        def get_health_report(self, context: ScopeContext, job_id: str):
            return self.store.get_health_job(job_id, context.scope_fingerprint)

        def shutdown(self) -> None:
            pass

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    app = create_gateway_app(
        settings=settings,
        store=store,
        runtime=Runtime(store),  # type: ignore[arg-type]
        caller_resolver=resolver,
        order_authorizer=Orders(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/health-report-jobs",
            headers={
                "Authorization": "Bearer java-to-aiops-secret",
                "X-AIOps-Delegation": "opaque-once-0123456789abcdef",
                "X-User-Id": "C-FORGED",
                "X-Tenant-Id": "TENANT-FORGED",
            },
            json={"order_no": "O-1"},
        )

    assert response.status_code == 202
    assert requests[0].get_header("Authorization") == "Bearer aiops-to-java-secret"
    assert json.loads(requests[0].data) == {
        "handle": "opaque-once-0123456789abcdef",
        "audience": "aiops-api",
        "purpose": "aiops:orders:read",
    }


@pytest.mark.parametrize("handle", [None, "", "bad handle", "x" * 257])
def test_delegation_handle_is_required_and_bounded(handle: str | None) -> None:
    with pytest.raises(CallerAuthError) as excinfo:
        DelegatedCallerResolver(_settings()).resolve(
            "java-to-aiops-secret",
            required_scope="aiops:orders:read",
            delegation_handle=handle,
        )

    assert excinfo.value.code in {CALLER_AUTH_INVALID, "caller_auth.forbidden"}


def test_delegation_rejects_wrong_service_without_redemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aiops_diagnostics.caller_auth.urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("must not redeem"),
    )

    with pytest.raises(CallerAuthError) as excinfo:
        DelegatedCallerResolver(_settings()).resolve(
            "wrong-service",
            required_scope="aiops:orders:read",
            delegation_handle="opaque-once-0123456789abcdef",
        )

    assert excinfo.value.code in {CALLER_AUTH_INVALID, "caller_auth.forbidden"}


@pytest.mark.parametrize(
    "payload",
    [
        _active(active=False),
        _active(audience="other-api"),
        _active(purpose="aiops:diagnoses:write"),
        _active(subject={"user_id": "", "tenant_id": "TENANT-1"}),
        _active(subject={"user_id": "C-1", "tenant_id": ""}),
    ],
)
def test_delegation_redemption_fails_closed(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    monkeypatch.setattr(
        "aiops_diagnostics.caller_auth.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    with pytest.raises(CallerAuthError) as excinfo:
        DelegatedCallerResolver(_settings()).resolve(
            "java-to-aiops-secret",
            required_scope="aiops:orders:read",
            delegation_handle="opaque-once-0123456789abcdef",
        )

    assert excinfo.value.code in {CALLER_AUTH_INVALID, "caller_auth.forbidden"}


def test_delegation_redemption_outage_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aiops_diagnostics.caller_auth.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )

    with pytest.raises(CallerAuthError) as excinfo:
        DelegatedCallerResolver(_settings()).resolve(
            "java-to-aiops-secret",
            required_scope="aiops:orders:read",
            delegation_handle="opaque-once-0123456789abcdef",
        )

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable
