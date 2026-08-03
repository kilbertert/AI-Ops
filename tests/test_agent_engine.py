import json
from pathlib import Path

import pytest

from aiops_diagnostics.agent_contracts import IncidentManifest
from aiops_diagnostics.agent_engine import AgentCoordinator
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import AgentRuntimeError, AgentTurnTimeout, CodexTurnOutput
from aiops_diagnostics.config import AgentSettings, SafetySettings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.sources import FixtureSources

FIXTURE = Path(__file__).parents[1] / "examples/fixtures/ocpp_consistent.json"


class _FakeSession:
    def __init__(self, responses, thread_id: str = "thread-test") -> None:
        self.responses = list(responses)
        self._thread_id = thread_id
        self.prompts = []
        self.closed = False

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def run(self, prompt: str) -> CodexTurnOutput:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return CodexTurnOutput(turn_id=f"turn-{len(self.prompts)}", final_response=response, usage={})

    def close(self) -> None:
        self.closed = True


def _context(tmp_path: Path):
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest, fixture_path=FIXTURE)
    journal = EvidenceJournal(workspace, manifest)
    tools = DiagnosticToolExecutor(
        FixtureSources(FIXTURE),
        request,
        manifest,
        journal,
        safety=SafetySettings(),
    )
    settings = AgentSettings(codex_bin="/bin/true", max_turns=5)
    return request, manifest, workspace, journal, tools, settings


def _tool_request() -> str:
    return json.dumps(
        {
            "kind": "tool_requests",
            "tool_requests": [{"tool": "order_snapshot", "reason": "confirm immutable order facts"}],
            "diagnosis": None,
        }
    )


def _diagnosis(manifest: IncidentManifest, evidence_id: str = "ev-001") -> str:
    return json.dumps(
        {
            "kind": "diagnosis",
            "tool_requests": [],
            "diagnosis": {
                "schema_version": "1.0",
                "incident_id": manifest.incident_id,
                "order_no": manifest.order_no,
                "tenant_id": manifest.tenant_id,
                "status": "diagnosed",
                "summary": "订单已找到，需继续按金额证据确认",
                "root_cause": "当前证据确认订单存在，金额路径尚无矛盾",
                "confidence": "medium",
                "evidence_ids": [evidence_id],
                "hypotheses": [
                    {
                        "title": "订单主数据存在",
                        "explanation": "订单快照返回唯一记录",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "limitations": [],
                "failed_sources": [],
                "next_steps": ["工程师可继续核对费用模板"],
            },
        },
        ensure_ascii=False,
    )


def test_coordinator_keeps_codex_in_control_of_tool_selection(tmp_path: Path) -> None:
    _, manifest, workspace, journal, tools, settings = _context(tmp_path)
    session = _FakeSession([_tool_request(), _diagnosis(manifest)])
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=lambda *_: session,
    )

    result = coordinator.run()

    assert result.status.value == "diagnosed"
    assert [entry.tool for entry in journal.entries()] == ["order_snapshot"]
    assert "Tool outcomes" in session.prompts[1]
    assert '"payload"' in session.prompts[1]
    assert workspace.load_state().thread_id == "thread-test"
    assert workspace.load_state().phase == "completed"
    assert session.closed is True


def test_coordinator_requests_contract_repair_on_invalid_identity(tmp_path: Path) -> None:
    _, manifest, workspace, journal, tools, settings = _context(tmp_path)
    invalid = json.loads(_diagnosis(manifest))
    invalid["diagnosis"]["order_no"] = "OTHER-ORDER"
    session = _FakeSession([_tool_request(), json.dumps(invalid), _diagnosis(manifest)])
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=lambda *_: session,
    )

    result = coordinator.run()

    assert result.order_no == manifest.order_no
    assert any("Validation errors" in prompt for prompt in session.prompts)
    assert workspace.load_state().validation_attempts == 1


def test_coordinator_repairs_malformed_structured_output(tmp_path: Path) -> None:
    _, manifest, workspace, journal, tools, settings = _context(tmp_path)
    session = _FakeSession(["not-json", _tool_request(), _diagnosis(manifest)])
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=lambda *_: session,
    )

    result = coordinator.run()

    assert result.status.value == "diagnosed"
    assert workspace.load_state().validation_attempts == 1
    assert "结构化输出无效" in session.prompts[1]


def test_coordinator_preserves_thread_for_resume_after_timeout(tmp_path: Path) -> None:
    _, manifest, workspace, journal, tools, settings = _context(tmp_path)
    session = _FakeSession([AgentTurnTimeout("timeout")], thread_id="thread-resume")
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=lambda *_: session,
    )

    with pytest.raises(AgentTurnTimeout):
        coordinator.run()

    state = workspace.load_state()
    assert state.phase == "interrupted"
    assert state.thread_id == "thread-resume"


def test_coordinator_marks_provider_failure_interrupted(tmp_path: Path) -> None:
    _, manifest, workspace, journal, tools, settings = _context(tmp_path)
    session = _FakeSession([AgentRuntimeError("provider unavailable")], thread_id="thread-failed")
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=lambda *_: session,
    )

    with pytest.raises(AgentRuntimeError):
        coordinator.run()

    assert workspace.load_state().phase == "interrupted"


def test_coordinator_marks_session_initialization_failure_interrupted(tmp_path: Path) -> None:
    _, manifest, workspace, journal, tools, settings = _context(tmp_path)

    def fail_session(*_args):
        raise AgentRuntimeError("provider initialization unavailable")

    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=fail_session,
    )

    with pytest.raises(AgentRuntimeError):
        coordinator.run()

    assert workspace.load_state().phase == "interrupted"
