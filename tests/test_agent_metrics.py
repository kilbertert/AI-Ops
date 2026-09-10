from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.metrics_store import MetricsStore, MetricsValidationError
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


def _context(
    tenant: str = "tenant-a",
    roles: frozenset[str] = frozenset({"ROLE_AGENT_ADMIN"}),
) -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", tenant_id=tenant)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="self"),
        roles=roles,
        permissions=frozenset({"aiops:agents:manage"}),
    )


# ── MetricsStore unit behavior ────────────────────────────────────────────


def test_record_and_summary_round_trip(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "gateway.db")
    store.record(
        tenant_id="tenant-a",
        route_type="qa",
        outcome="completed",
        agent_id="agt_x",
        agent_version_key="agt_x#v1",
        conversation_id="conv_abc",
        retrieval_status="found",
        searches=2,
        media_count=1,
        duration_ms=1500,
        token_count=120,
    )
    store.record(
        tenant_id="tenant-a",
        route_type="qa",
        outcome="failed",
        error_code="QA_FAILED",
        duration_ms=300,
    )
    store.record(tenant_id="tenant-a", route_type="faq", outcome="completed", duration_ms=12)
    store.record(
        tenant_id="tenant-a",
        route_type="diagnosis",
        outcome="completed",
        duration_ms=8000,
        token_count=500,
    )
    store.record(
        tenant_id="tenant-a",
        route_type="qa",
        outcome="busy",
        error_code="CONVERSATION_BUSY",
    )
    store.record(tenant_id="tenant-b", route_type="qa", outcome="completed")

    summary = store.summary("tenant-a")
    totals = summary["totals"]
    assert totals["runs"] == 5
    assert totals["completed"] == 3
    assert totals["failed"] == 1
    assert totals["busy"] == 1
    assert totals["tokens"] == 620
    assert totals["media"] == 1
    routes = {row["route_type"]: row for row in summary["by_route"]}
    assert routes["qa"]["completed"] == 1
    assert routes["faq"]["runs"] == 1
    retrieval = {row["retrieval_status"]: row["runs"] for row in summary["by_retrieval"]}
    assert retrieval["found"] == 1
    errors = {row["error_code"]: row["runs"] for row in summary["by_error"]}
    assert errors["QA_FAILED"] == 1
    assert errors["CONVERSATION_BUSY"] == 1

    # tenant-b stays isolated
    other = store.summary("tenant-b")
    assert other["totals"]["runs"] == 1

    # agent filter narrows
    only = store.summary("tenant-a", agent_id="agt_x")
    assert only["totals"]["runs"] == 1


def test_store_rejects_unknown_enum_values(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "gateway.db")
    with pytest.raises(MetricsValidationError):
        store.record(tenant_id="tenant-a", route_type="unknown", outcome="completed")
    with pytest.raises(MetricsValidationError):
        store.record(tenant_id="tenant-a", route_type="qa", outcome="exploded")
    with pytest.raises(MetricsValidationError):
        store.record(tenant_id="tenant-a", route_type="qa", outcome="completed", retrieval_status="huh")
    with pytest.raises(MetricsValidationError):
        store.record(tenant_id="", route_type="qa", outcome="completed")
    with pytest.raises(MetricsValidationError):
        store.record(tenant_id="tenant-a", route_type="qa", outcome="completed", error_code="lower-case")
    with pytest.raises(MetricsValidationError):
        store.record(tenant_id="tenant-a", route_type="qa", outcome="completed", media_count=-1)


def test_summary_and_list_never_expose_text(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "gateway.db")
    store.record(tenant_id="tenant-a", route_type="qa", outcome="completed")
    row = store.list_runs("tenant-a")[0]
    assert set(row) == {
        "metric_id",
        "route_type",
        "outcome",
        "agent_id",
        "agent_version_key",
        "conversation_id",
        "retrieval_status",
        "searches",
        "media_count",
        "duration_ms",
        "token_count",
        "error_code",
        "created_at",
    }
    summary = store.summary("tenant-a")
    assert "question" not in str(summary)


def test_retention_prunes_rows_after_30_days(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "gateway.db")
    old = datetime.now(UTC) - timedelta(days=31)
    fresh = datetime.now(UTC) - timedelta(days=1)
    store.record(tenant_id="tenant-a", route_type="qa", outcome="completed", created_at=old)
    store.record(tenant_id="tenant-a", route_type="qa", outcome="completed", created_at=fresh)
    # The next write triggers pruning of the 31-day-old row.
    store.record(tenant_id="tenant-a", route_type="faq", outcome="completed")
    assert store.summary("tenant-a")["totals"]["runs"] == 2


def test_list_runs_filters_by_route_and_agent(tmp_path: Path) -> None:
    store = MetricsStore(tmp_path / "gateway.db")
    store.record(tenant_id="tenant-a", route_type="qa", outcome="completed", agent_id="agt_a")
    store.record(tenant_id="tenant-a", route_type="faq", outcome="completed")
    store.record(tenant_id="tenant-a", route_type="qa", outcome="failed", agent_id="agt_b")
    qa_rows = store.list_runs("tenant-a", route_type="qa")
    assert len(qa_rows) == 2
    a_rows = store.list_runs("tenant-a", agent_id="agt_a")
    assert len(a_rows) == 1 and a_rows[0]["route_type"] == "qa"
    # Cross-tenant listing returns nothing
    assert store.list_runs("tenant-b") == []


# ── API surface ──────────────────────────────────────────────────────────


class _Runtime:
    """Fake runtime carrying the metrics seam like the real GatewayRuntime."""

    def __init__(self, store: MetricsStore) -> None:
        self.metrics_store = store
        self.route_calls: list[tuple[str, str]] = []

    def shutdown(self) -> None:
        pass

    def _record_metric(self, **fields: Any) -> None:
        self.metrics_store.record(**fields)

    def record_route_metric(self, context, route_type: str, outcome: str, **extra: Any) -> None:
        self.route_calls.append((route_type, outcome))
        tenant_id = getattr(context, "effective_tenant_id", "") or ""
        if not tenant_id:
            return
        self._record_metric(tenant_id=tenant_id, route_type=route_type, outcome=outcome, **extra)

    def get_agent_metrics_summary(self, context, *, agent_id=None, window_hours=720):
        roles = frozenset(getattr(context, "roles", ()))
        if "ROLE_AGENT_ADMIN" not in roles and "ROLE_AGENT_VIEWER" not in roles:
            from aiops_diagnostics.codex_runtime import AgentRuntimeError

            raise AgentRuntimeError("agent access is not permitted")
        return self.metrics_store.summary(
            getattr(context, "effective_tenant_id", ""), agent_id=agent_id, limit_hours=window_hours
        )

    def list_agent_metrics_runs(self, context, *, agent_id=None, route_type=None, limit=50):
        return self.metrics_store.list_runs(
            getattr(context, "effective_tenant_id", ""),
            agent_id=agent_id,
            route_type=route_type,
            limit=limit,
        )


class _Resolver:
    def __init__(self, context: ScopeContext) -> None:
        self.context = context

    def resolve(self, token: str, *, required_scope: str, third_session: str | None = None) -> ScopeContext:
        del token, required_scope, third_session
        return self.context


def _app(tmp_path: Path, *, context: ScopeContext, runtime: _Runtime) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,  # type: ignore[arg-type]
        caller_resolver=_Resolver(context),
    )
    return TestClient(app)


def test_metrics_summary_endpoint_returns_tenant_aggregate(tmp_path: Path) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id="tenant-a", route_type="qa", outcome="completed", retrieval_status="found")
    metrics.record(tenant_id="tenant-a", route_type="qa", outcome="failed", error_code="QA_FAILED")
    with _app(tmp_path, context=_context(), runtime=_Runtime(metrics)) as client:
        resp = client.get(
            "/v1/agent-metrics/summary",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "tenant-a"
        assert body["totals"]["runs"] == 2
        assert body["totals"]["failed"] == 1
        assert "question" not in resp.text and "prompt" not in resp.text


def test_metrics_runs_endpoint_lists_redacted_rows(tmp_path: Path) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id="tenant-a", route_type="debug", outcome="completed", agent_id="agt_z")
    with _app(tmp_path, context=_context(), runtime=_Runtime(metrics)) as client:
        resp = client.get(
            "/v1/agent-metrics/runs?route_type=debug",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status_code == 200, resp.text
        runs = resp.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["agent_id"] == "agt_z"
        assert "question" not in runs[0]


def test_metrics_endpoints_require_view_role(tmp_path: Path) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    viewer = _context(roles=frozenset({"ROLE_AGENT_VIEWER"}))
    with _app(tmp_path, context=viewer, runtime=_Runtime(metrics)) as client:
        assert (
            client.get("/v1/agent-metrics/summary", headers={"Authorization": "Bearer token"}).status_code
            == 200
        )

    outsider = _context(roles=frozenset({"ROLE_ORDER_VIEWER"}))
    with _app(tmp_path, context=outsider, runtime=_Runtime(metrics)) as client:
        assert (
            client.get("/v1/agent-metrics/summary", headers={"Authorization": "Bearer token"}).status_code
            == 403
        )
        assert (
            client.get("/v1/agent-metrics/runs", headers={"Authorization": "Bearer token"}).status_code == 403
        )


def test_metrics_endpoints_isolate_tenants(tmp_path: Path) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id="tenant-a", route_type="qa", outcome="completed")
    metrics.record(tenant_id="tenant-b", route_type="qa", outcome="completed")
    tenant_b = _context(tenant="tenant-b")
    with _app(tmp_path, context=tenant_b, runtime=_Runtime(metrics)) as client:
        summary = client.get("/v1/agent-metrics/summary", headers={"Authorization": "Bearer token"})
        assert summary.json()["totals"]["runs"] == 1  # only tenant-b's row
        runs = client.get("/v1/agent-metrics/runs", headers={"Authorization": "Bearer token"})
        assert all(row.get("agent_id") is None for row in runs.json()["runs"])
        assert len(runs.json()["runs"]) == 1


def test_unauthenticated_metrics_request_is_rejected(tmp_path: Path) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    with _app(tmp_path, context=_context(), runtime=_Runtime(metrics)) as client:
        assert client.get("/v1/agent-metrics/summary").status_code == 401


def test_invalid_filters_are_422(tmp_path: Path) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    with _app(tmp_path, context=_context(), runtime=_Runtime(metrics)) as client:
        resp = client.get(
            "/v1/agent-metrics/runs?route_type=bogus",
            headers={"Authorization": "Bearer token"},
        )
        assert resp.status_code == 422


# ── Real GatewayRuntime integration ─────────────────────────────────────


def test_runtime_records_failed_qa_metric(tmp_path: Path, monkeypatch) -> None:
    """The real runtime's QA failure path writes the redacted metric row.

    Uses the actual ``GatewayRuntime`` executor and store: a failing model
    produces a failed job AND a (qa, failed, QA_FAILED) metrics row with a
    duration — proving the completion-point wiring, not just the store.
    """
    import os
    import time as time_module

    from aiops_diagnostics.agent_runner import AgentRuntimeError
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    os.chmod(tmp_path, 0o750)
    config = tmp_path / "production.env"
    config.write_text("# test\n", encoding="utf-8")
    os.chmod(config, 0o600)
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
    )
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(gateway_settings.database_file)
    runtime = GatewayRuntime(store, gateway_settings, settings)

    subject = SubjectRecord(b_user_id="B-1", tenant_id="T-1")
    context = ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:qa:write"}),
    )

    calls: list[int] = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AgentRuntimeError("model exploded")

    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.run_zero_order_answer", boom)
    try:
        qa = runtime.start_assistant_qa(context, "怎么拔枪")
        deadline = time_module.time() + 15
        job = None
        while time_module.time() < deadline:
            job = runtime.get_assistant_qa(context, qa["qa_id"])
            if job and job["status"] in ("completed", "failed"):
                break
            time_module.sleep(0.3)
    finally:
        runtime.shutdown()

    assert job is not None and job["status"] == "failed"
    assert calls, "model path did not run"
    rows = MetricsStore(tmp_path / "gateway.db").list_runs("T-1")
    row = next(r for r in rows if r["route_type"] == "qa")
    assert row["outcome"] == "failed"
    assert row["error_code"] == "QA_FAILED"
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    # The redacted row never carries the question or answer text.
    assert "怎么拔枪" not in str(row)
