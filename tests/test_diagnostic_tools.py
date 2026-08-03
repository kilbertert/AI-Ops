from pathlib import Path

from aiops_diagnostics.agent_contracts import IncidentManifest, ToolName
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.sources import FixtureSources

FIXTURE = Path(__file__).parents[1] / "examples/fixtures/ocpp_consistent.json"


def _executor(tmp_path: Path) -> tuple[DiagnosticToolExecutor, EvidenceJournal]:
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    return (
        DiagnosticToolExecutor(
            FixtureSources(FIXTURE),
            request,
            manifest,
            journal,
            safety=SafetySettings(),
        ),
        journal,
    )


def test_codex_selected_tools_enforce_dependencies_and_reuse_evidence(tmp_path: Path) -> None:
    executor, journal = _executor(tmp_path)

    blocked = executor.execute(ToolName.GUN_TIMESERIES)
    order = executor.execute(ToolName.ORDER_SNAPSHOT)
    gun = executor.execute(ToolName.GUN_TIMESERIES)
    reused = executor.execute(ToolName.ORDER_SNAPSHOT)

    assert blocked.status == "blocked"
    assert order.status == "success"
    assert gun.status == "success"
    assert reused.reused is True
    assert reused.evidence_id == order.evidence_id
    assert order.model_payload["orders"]  # type: ignore[index]
    assert reused.model_payload == order.model_payload
    assert len(journal.entries()) == 3
    payload = journal.load_payload(journal.get(gun.evidence_id))  # type: ignore[arg-type]
    assert "samples" in payload


def test_known_runbook_is_optional_advisory_evidence(tmp_path: Path) -> None:
    executor, journal = _executor(tmp_path)

    blocked = executor.execute(ToolName.KNOWN_RUNBOOK)
    executor.execute(ToolName.ORDER_SNAPSHOT)
    outcome = executor.execute(ToolName.KNOWN_RUNBOOK)

    payload = journal.load_payload(journal.get(outcome.evidence_id))  # type: ignore[arg-type]
    assert blocked.status == "blocked"
    assert outcome.status == "success"
    assert payload["report"]["request"]["order_no"] == "TEST-OCPP-0003"


def test_order_scoped_tools_require_a_unique_order_snapshot(tmp_path: Path) -> None:
    executor, _ = _executor(tmp_path)

    assert executor.execute(ToolName.FEE_SNAPSHOT).status == "blocked"
    assert executor.execute(ToolName.REDIS_SYNC).status == "blocked"
    executor.execute(ToolName.ORDER_SNAPSHOT)
    assert executor.execute(ToolName.FEE_SNAPSHOT).status == "success"
    assert executor.execute(ToolName.REDIS_SYNC).status == "success"
