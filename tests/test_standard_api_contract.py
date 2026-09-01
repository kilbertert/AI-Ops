from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Resolver:
    def resolve(self, token: str, *, required_scope: str) -> ScopeContext:
        subject = SubjectRecord(b_user_id=token, c_user_id=token, tenant_id="T-1")
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id="T-1",
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
        )


class _Orders:
    def can_access(self, context: ScopeContext, order_no: str) -> bool:
        return order_no == "ORDER-1"


class _Runtime:
    def __init__(self, store: GatewayStore) -> None:
        self.store = store

    def start_health_report(self, context: ScopeContext, order_no: str):
        return self.store.create_or_reuse_health_job(context.scope_fingerprint, order_no, "health-v2")[0]

    def get_health_report(self, context: ScopeContext, job_id: str):
        return self.store.get_health_job(job_id, context.scope_fingerprint)

    def start_standard_diagnosis(self, context, order_no, question, indicator_code):
        return self.store.create_standard_diagnosis(
            context.scope_fingerprint, order_no, question, indicator_code
        )

    def get_standard_diagnosis(self, context, diagnosis_id):
        return self.store.get_standard_diagnosis(diagnosis_id, context.scope_fingerprint)

    def list_standard_diagnoses(self, context, *, limit):
        return self.store.list_standard_diagnoses(context.scope_fingerprint, limit=limit)

    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    store = GatewayStore(settings.database_file)
    return TestClient(
        create_gateway_app(
            settings=settings,
            store=store,
            runtime=_Runtime(store),  # type: ignore[arg-type]
            caller_resolver=_Resolver(),
            order_authorizer=_Orders(),
        )
    )


def test_standard_contract_uses_same_error_shape_and_independent_resources(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        invalid = client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer caller"},
            json={"order_no": "bad order"},
        )
        health = client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer caller"},
            json={"order_no": "ORDER-1"},
        )
        diagnosis = client.post(
            "/v1/standard/diagnoses",
            headers={"Authorization": "Bearer caller"},
            json={"order_no": "ORDER-1", "question": "why"},
        )

    assert invalid.status_code == 422
    assert set(invalid.json()) == {"error"}
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert health.status_code == 202
    assert set(health.json()) >= {"job_id", "status", "retry_after_ms", "error"}
    assert diagnosis.status_code == 202
    assert set(diagnosis.json()) >= {"diagnosis_id", "status", "retry_after_ms", "error"}
