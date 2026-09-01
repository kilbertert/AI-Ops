from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


class _Resolver:
    def resolve(self, token: str, *, required_scope: str) -> ScopeContext:
        subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="T-1")
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
        return order_no == "O-1"


class _Runtime:
    def __init__(self, store: GatewayStore) -> None:
        self.store = store

    def start_health_report(self, context: ScopeContext, order_no: str):
        job, _ = self.store.create_or_reuse_health_job(context.scope_fingerprint, order_no, "health-v1")
        return job

    def get_health_report(self, context: ScopeContext, job_id: str):
        return self.store.get_health_job(job_id, context.scope_fingerprint)

    def shutdown(self) -> None:
        pass


def _client(tmp_path: Path) -> tuple[TestClient, GatewayStore]:
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
        runtime=_Runtime(store),  # type: ignore[arg-type]
        caller_resolver=_Resolver(),
        order_authorizer=_Orders(),
    )
    return TestClient(app), store


def test_health_report_job_create_and_poll(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    with client:
        created = client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-1"},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        store.update_health_job(job_id, status="running")
        store.update_health_job(job_id, status="completed", report={"summary": "ok"})
        result = client.get(
            f"/v1/health-report-jobs/{job_id}",
            headers={"Authorization": "Bearer token"},
        )

    assert result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["report"] == {"summary": "ok"}
    assert "scope_fingerprint" not in result.json()


def test_health_report_job_rejects_unauthorized_order(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    with client:
        response = client.post(
            "/v1/health-report-jobs",
            headers={"Authorization": "Bearer token"},
            json={"order_no": "O-OTHER"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ORDER_NOT_FOUND"
    assert store.count_health_jobs() == 0


def test_health_report_job_create_meets_p95_target(tmp_path: Path) -> None:
    """Off-process POST create-call stays under the documented 500 ms P95 target.

    This is the offline smoke for the PRD #87 Performance decision. Real-environment
    measurement belongs to the #92 acceptance plan; here we only assert that the
    gateway handoff (auth + scope + order authorization + store insert) does not
    blow the budget before any real source work begins.
    """
    client, _ = _client(tmp_path)
    samples_ms: list[float] = []
    with client:
        for i in range(50):
            started = time.perf_counter()
            response = client.post(
                "/v1/health-report-jobs",
                headers={"Authorization": "Bearer token"},
                json={"order_no": f"O-{i % 5}"},  # reuse to exercise the reuse path
            )
            samples_ms.append((time.perf_counter() - started) * 1000)
            assert response.status_code in {202, 404}

    p95_index = max(0, int(len(samples_ms) * 0.95) - 1)
    p95 = sorted(samples_ms)[p95_index]
    assert p95 < 500, f"create-job P95 {p95:.1f} ms exceeds 500 ms target"
