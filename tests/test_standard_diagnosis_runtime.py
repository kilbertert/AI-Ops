from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops_diagnostics.agent_contracts import AgentDiagnosis, Confidence, DiagnosisStatus
from aiops_diagnostics.config import Settings
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_runtime import GatewayRuntime
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


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
