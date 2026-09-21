from __future__ import annotations

import ast
import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops_diagnostics.agent_contracts import (
    AgentDiagnosis,
    Confidence,
    DiagnosisStatus,
    IncidentManifest,
    ToolName,
)
from aiops_diagnostics.agent_runner import _agent_sources
from aiops_diagnostics.config import Settings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import DIAGNOSIS_ORDER_OUT_OF_SCOPE, GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.sources import FixtureSources

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "aiops_diagnostics"


def _scope() -> ScopeContext:
    subject = SubjectRecord(b_user_id="B-1", c_user_id="C-1", tenant_id="T-1")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:diagnoses:write"}),
    )


def _runtime(tmp_path: Path) -> tuple[GatewayRuntime, GatewayStore, Settings]:
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    gateway_settings.server_config_file.write_text("# test\n", encoding="utf-8")
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(gateway_settings.database_file)
    return GatewayRuntime(store, gateway_settings, settings), store, settings


def test_standard_diagnosis_runs_existing_agent_path(tmp_path: Path, monkeypatch) -> None:
    runtime, store, settings = _runtime(tmp_path)
    calls = []

    def diagnose(workspace, request, selected_settings, fixture, **kwargs):
        calls.append((workspace.run_id, request.order_no, fixture, kwargs["scope"].tenant_id))
        return AgentDiagnosis(
            incident_id=workspace.load_manifest().incident_id,
            order_no=request.order_no,
            tenant_id=request.tenant_id,
            status=DiagnosisStatus.DIAGNOSED,
            summary="已完成诊断",
            root_cause="测试根因",
            confidence=Confidence.HIGH,
            evidence_ids=[],
            hypotheses=[],
            limitations=[],
            failed_sources=[],
            next_steps=[],
        )

    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.run_agent_diagnosis", diagnose)

    created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None)
    deadline = time.monotonic() + 2
    current = created
    while current["status"] not in {"completed", "failed", "inconclusive"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        current = store.get_standard_diagnosis(created["diagnosis_id"], _scope().scope_fingerprint)

    runtime.shutdown()
    assert current["status"] == "completed"
    assert current["result"]["summary"] == "已完成诊断"
    assert calls and calls[0][1:] == ("ORDER-1", None, "T-1")
    assert current["internal_run_id"].startswith("run-")


def test_blocked_diagnosis_is_failed_with_model_output_code(tmp_path: Path, monkeypatch) -> None:
    """A blocked run (model could not produce structured output) is surfaced as
    failed DIAGNOSIS_BLOCKED, NOT a generic inconclusive — so the frontend can
    distinguish provider/task failure from insufficient evidence."""
    runtime, store, settings = _runtime(tmp_path)

    def diagnose(workspace, request, selected_settings, fixture, **kwargs):
        del workspace, request, selected_settings, fixture, kwargs
        return AgentDiagnosis(
            incident_id="incident-x",
            order_no="ORDER-1",
            tenant_id="T-1",
            status=DiagnosisStatus.BLOCKED,
            summary="诊断未能生成结论",
            root_cause="Codex 多次返回无效结构化输出",
            confidence=Confidence.LOW,
            evidence_ids=[],
            hypotheses=[],
            limitations=["Codex 多次返回无效结构化输出"],
            failed_sources=[],
            next_steps=[],
        )

    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.run_agent_diagnosis", diagnose)

    created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None)
    deadline = time.monotonic() + 2
    current = created
    while current["status"] not in {"completed", "failed", "inconclusive"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        current = store.get_standard_diagnosis(created["diagnosis_id"], _scope().scope_fingerprint)
    runtime.shutdown()
    assert current["status"] == "failed"
    assert current["error_code"] == "DIAGNOSIS_BLOCKED"


def _blocked_diagnosis(order_no: str, reason: str) -> AgentDiagnosis:
    return AgentDiagnosis(
        incident_id="incident-x",
        order_no=order_no,
        tenant_id="T-1",
        status=DiagnosisStatus.BLOCKED,
        summary="诊断运行未能生成满足证据合同的结论",
        root_cause=reason,
        confidence=Confidence.LOW,
        evidence_ids=[],
        hypotheses=[],
        limitations=[reason],
        failed_sources=[],
        next_steps=[],
    )


def _foreign_tenant_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "foreign-tenant-orders.json"
    path.write_text(
        json.dumps({"orders": [{"order_no": "ORDER-1", "tenant_id": "TENANT-OTHER"}]}),
        encoding="utf-8",
    )
    return path


def test_the_standard_worker_hands_the_agent_one_range_object() -> None:
    """Guard: the caller path must not carry the tenant twice.

    It used to pass ``scope=query_scope`` *and*
    ``allowed_tenants={context.effective_tenant_id}``: the tenant once as a SQL
    push-down and once as a Python post-filter over the rows that push-down had
    already returned. They agree only while one call site keeps reading the same
    field twice, and nothing pinned that equation — so the two mechanisms could
    drift apart with no test noticing.

    Source-level rather than behavioural, because what is being prevented is a
    future edit. The device path legitimately still passes ``allowed_tenants``
    (it has no SQL to push into), which is why the check is scoped to the
    standard-diagnosis worker.
    """
    tree = ast.parse(
        (SOURCE_ROOT / "gateway_runtime.py").read_text(encoding="utf-8"),
        filename="gateway_runtime.py",
    )
    worker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_standard_diagnosis"
    )
    calls = [
        call
        for call in ast.walk(worker)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "run_agent_diagnosis"
    ]
    assert calls, "_execute_standard_diagnosis must run the shared agent path"
    offenders = [
        f"gateway_runtime.py:{call.lineno}"
        for call in calls
        if any(keyword.arg == "allowed_tenants" for keyword in call.keywords)
    ]
    assert not offenders, (
        "the caller path must pass one range object (scope); the tenant must not also "
        f"be post-filtered via allowed_tenants: found {', '.join(offenders)}"
    )
    assert all(any(keyword.arg == "scope" for keyword in call.keywords) for call in calls)


def test_out_of_scope_order_fails_with_a_distinguishable_code(tmp_path: Path, monkeypatch) -> None:
    """An order outside the caller's tenant is not a provider failure.

    ``DIAGNOSIS_BLOCKED`` means "the model could not produce structured output"
    — the supplier not honoring ``output_schema``. The same code used to absorb
    "this order is not in your tenant" too, so an operator could not tell an
    authorization failure from a supplier failure on this surface. The shared
    rule records one coded result for out-of-scope; this face now presents it
    instead of collapsing it.
    """
    runtime, store, settings = _runtime(tmp_path)
    fixture = _foreign_tenant_fixture(tmp_path)

    def diagnose(workspace, request, selected_settings, fixture_path, **kwargs):
        # The real tool layer, driven by the one scope object the worker passed.
        # The shared row-level rule turns the foreign-tenant order into its coded
        # blocked record — the same record the device path produces.
        del selected_settings, fixture_path
        executor = DiagnosticToolExecutor(
            FixtureSources(fixture),
            request,
            IncidentManifest.from_request(request),
            EvidenceJournal(workspace, workspace.load_manifest()),
            safety=settings.safety,
            scope=kwargs["scope"],
        )
        executor.execute(ToolName.ORDER_SNAPSHOT)
        return _blocked_diagnosis(request.order_no, "订单不在调用者租户内，无证据可收集")

    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.run_agent_diagnosis", diagnose)

    created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None)
    deadline = time.monotonic() + 2
    current = created
    while current["status"] not in {"completed", "failed", "inconclusive"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        current = store.get_standard_diagnosis(created["diagnosis_id"], _scope().scope_fingerprint)

    runtime.shutdown()
    assert current["status"] == "failed"
    assert current["error_code"] == "DIAGNOSIS_ORDER_OUT_OF_SCOPE"
    assert current["error_code"] != "DIAGNOSIS_BLOCKED"


def test_the_out_of_scope_code_is_defense_in_depth_on_this_face(tmp_path: Path, monkeypatch) -> None:
    """Why the contract docs must not promise ``DIAGNOSIS_ORDER_OUT_OF_SCOPE``.

    The code is produced only when the row-level rule blocks a row the source
    already returned. On the standard API face that can never be the *first*
    check on an order: create-time authorization resolves the same
    ``QueryScope`` and pushes the tenant into SQL, and the worker's source set is
    the scoped one — never a fixture — so the tool layer only ever sees rows the
    predicate already admitted. The code is therefore defense in depth (a source
    that cannot push the rule down, or a scope re-resolved differently between
    create and worker), not a terminal state the frontend should wait for.

    Both halves are pinned here because they are the whole reachability
    argument: reintroducing a fixture source or dropping the scope on this face
    would make the code reachable through a path nobody authorized, and the
    contract docs would keep promising a signal production does not emit.
    """
    runtime, store, settings = _runtime(tmp_path)
    captured: dict[str, object] = {}

    def diagnose(workspace, request, selected_settings, fixture, **kwargs):
        del workspace, request, selected_settings
        captured["fixture"] = fixture
        captured["scope"] = kwargs["scope"]
        return _blocked_diagnosis("ORDER-1", "订单不在调用者租户内，无证据可收集")

    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.Settings.from_config", lambda *_: settings)
    monkeypatch.setattr("aiops_diagnostics.gateway_runtime.run_agent_diagnosis", diagnose)

    created = runtime.start_standard_diagnosis(_scope(), "ORDER-1", "为什么跳枪", None)
    deadline = time.monotonic() + 2
    current = created
    while current["status"] not in {"completed", "failed", "inconclusive"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        current = store.get_standard_diagnosis(created["diagnosis_id"], _scope().scope_fingerprint)
    runtime.shutdown()

    # Half one: the worker never runs this face on fixtures, and it carries the
    # caller's own resolved scope rather than a second copy of the tenant.
    assert captured["fixture"] is None
    scope = captured["scope"]
    assert isinstance(scope, QueryScope)
    assert scope.tenant_id == "T-1"

    # Half two: a fixture-less run with a scope therefore resolves to the scoped
    # source set, which pushes the tenant into SQL, and not to the unscoped one.
    entered: list[str] = []

    @contextmanager
    def _fake_scoped(_settings, *, scope):
        del scope
        entered.append("scoped")
        yield object()

    @contextmanager
    def _fake_live(_settings):
        entered.append("live")
        yield object()

    monkeypatch.setattr("aiops_diagnostics.agent_runner.scoped_live_sources", _fake_scoped)
    monkeypatch.setattr("aiops_diagnostics.agent_runner.live_sources", _fake_live)
    with _agent_sources(settings, None, scope=scope):
        pass
    assert entered == ["scoped"]


def test_the_out_of_scope_code_reaches_the_metrics_row(tmp_path: Path) -> None:
    """Ops read the failure reason from the metrics row, not only from the API.

    ``_record_metric`` is best-effort and swallows its own failures, so a code the
    metrics store would reject is dropped silently — and the operator is back to
    being unable to tell an authorization failure from a supplier failure. Record
    the code the way the worker does and read it back.
    """
    from aiops_diagnostics.metrics_store import MetricsStore

    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(
        tenant_id="T-1",
        route_type="diagnosis",
        outcome="failed",
        error_code=DIAGNOSIS_ORDER_OUT_OF_SCOPE,
        duration_ms=1,
    )

    rows = metrics.list_runs("T-1", route_type="diagnosis")
    assert [row["error_code"] for row in rows] == [DIAGNOSIS_ORDER_OUT_OF_SCOPE]


def test_inconclusive_diagnosis_is_retained_and_late_completion_is_rejected(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.db")
    diagnosis = store.create_standard_diagnosis("scope-1", "O-1", "问题", None)
    store.update_standard_diagnosis(diagnosis["diagnosis_id"], status="running")
    assert store.update_standard_diagnosis(
        diagnosis["diagnosis_id"],
        status="inconclusive",
        result={"summary": "证据不足"},
    )
    retained = store.get_standard_diagnosis(diagnosis["diagnosis_id"], "scope-1")
    assert retained["status"] == "inconclusive"
    assert datetime.fromisoformat(retained["expires_at"]) > datetime.now(UTC) + timedelta(minutes=14)

    late = store.create_standard_diagnosis("scope-1", "O-2", "问题", None)
    store.update_standard_diagnosis(late["diagnosis_id"], status="running")
    with store._connection(write=True) as connection:
        connection.execute(
            "UPDATE standard_diagnoses SET deadline_at = ? WHERE diagnosis_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), late["diagnosis_id"]),
        )
    assert not store.update_standard_diagnosis(
        late["diagnosis_id"],
        status="completed",
        result={"summary": "迟到"},
    )
    assert store.get_standard_diagnosis(late["diagnosis_id"], "scope-1")["status"] == "expired"


def test_diagnosis_deadline_covers_real_agent_runtime() -> None:
    """The deadline must exceed real agent runs (observed ~7 minutes on the
    120-world link, 2026-09-05). A short deadline expires still-running
    diagnoses before the worker can record completed/inconclusive."""
    from aiops_diagnostics.gateway_store import DIAGNOSIS_DEADLINE

    assert timedelta(minutes=15) <= DIAGNOSIS_DEADLINE


def test_diagnosis_survives_long_running_worker(tmp_path: Path) -> None:
    """A worker finishing after the API-contract "tens of seconds to minutes"
    window must still be able to record its result before the deadline."""
    store = GatewayStore(tmp_path / "gateway.db")
    diagnosis = store.create_standard_diagnosis("scope-1", "O-1", "问题", None)
    stored = store.get_standard_diagnosis(diagnosis["diagnosis_id"], "scope-1")
    created = datetime.fromisoformat(stored["created_at"])
    from aiops_diagnostics.gateway_store import DIAGNOSIS_DEADLINE

    with store._connection(write=True) as connection:
        row = connection.execute(
            "SELECT deadline_at FROM standard_diagnoses WHERE diagnosis_id = ?",
            (diagnosis["diagnosis_id"],),
        ).fetchone()
    assert datetime.fromisoformat(row[0]) - created >= DIAGNOSIS_DEADLINE
