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
    progress_events = []
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=lambda *_: session,
        progress_callback=progress_events.append,
    )

    result = coordinator.run()

    assert result.status.value == "diagnosed"
    assert [entry.tool for entry in journal.entries()] == ["order_snapshot"]
    assert "Tool outcomes" in session.prompts[1]
    assert '"payload"' in session.prompts[1]
    assert workspace.load_state().thread_id == "thread-test"
    assert workspace.load_state().phase == "completed"
    assert session.closed is True
    event_types = [event["type"] for event in progress_events]
    assert event_types[:3] == ["diagnosis_started", "codex_session_starting", "codex_thread_ready"]
    assert "codex_turn_waiting" in event_types
    assert "tool_batch_started" in event_types
    assert "tool_batch_completed" in event_types
    assert event_types[-1] == "diagnosis_completed"


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


def _diagnosis_json() -> str:
    return json.dumps(
        {
            "kind": "diagnosis",
            "diagnosis": {
                "schema_version": "1.0",
                "incident_id": "incident-abc123",
                "order_no": "ORDER-1",
                "tenant_id": None,
                "status": "diagnosed",
                "summary": "正常结束",
                "root_cause": "后端异常结束",
                "confidence": "high",
                "evidence_ids": ["ev-1"],
                "hypotheses": [{"title": "通信中断", "explanation": "遥测停止", "evidence_ids": ["ev-1"]}],
                "limitations": [],
                "failed_sources": [],
                "next_steps": ["检查通信链路"],
            },
        },
        ensure_ascii=False,
    )


def test_parse_agent_turn_accepts_raw_json() -> None:
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    turn = _parse_agent_turn(_diagnosis_json())
    assert turn.kind == "diagnosis"
    assert turn.diagnosis is not None
    assert turn.diagnosis.summary == "正常结束"


def test_parse_agent_turn_accepts_markdown_fenced_json() -> None:
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    fenced = "```json\n" + _diagnosis_json() + "\n```"
    turn = _parse_agent_turn(fenced)
    assert turn.kind == "diagnosis"


def test_parse_agent_turn_accepts_prose_prefix_with_fenced_json() -> None:
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    text = "Based on the SOP, I need to return the diagnosis.\n\n```json\n" + _diagnosis_json() + "\n```"
    turn = _parse_agent_turn(text)
    assert turn.kind == "diagnosis"
    assert turn.diagnosis.confidence.value == "high"


def test_parse_agent_turn_rejects_genuinely_invalid_output() -> None:
    from pydantic import ValidationError

    from aiops_diagnostics.agent_engine import _parse_agent_turn

    with pytest.raises((ValidationError, ValueError)):
        _parse_agent_turn("this is not json at all")
    with pytest.raises((ValidationError, ValueError)):
        _parse_agent_turn("```json\n{not valid json}\n```")


def _diagnosis_payload() -> dict:
    """The inner AgentDiagnosis object, unwrapped (no kind discriminator)."""
    return {
        "schema_version": "1.0",
        "incident_id": "incident-abc123",
        "order_no": "ORDER-1",
        "tenant_id": None,
        "status": "diagnosed",
        "summary": "正常结束",
        "root_cause": "后端异常结束",
        "confidence": "high",
        "evidence_ids": ["ev-1"],
        "hypotheses": [{"title": "通信中断", "explanation": "遥测停止", "evidence_ids": ["ev-1"]}],
        "limitations": [],
        "failed_sources": [],
        "next_steps": ["检查通信链路"],
    }


def test_parse_agent_turn_accepts_unwrapped_diagnosis() -> None:
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    turn = _parse_agent_turn(json.dumps(_diagnosis_payload(), ensure_ascii=False))
    assert turn.kind == "diagnosis"
    assert turn.diagnosis is not None
    assert turn.diagnosis.root_cause == "后端异常结束"


def test_parse_agent_turn_accepts_unwrapped_diagnosis_in_fence() -> None:
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    text = "```json\n" + json.dumps(_diagnosis_payload(), ensure_ascii=False) + "\n```"
    turn = _parse_agent_turn(text)
    assert turn.kind == "diagnosis"
    assert turn.diagnosis.confidence.value == "high"


def test_parse_agent_turn_accepts_unwrapped_tool_requests() -> None:
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    payload = {"tool_requests": [{"tool": "order_snapshot", "reason": "需要订单快照"}]}
    turn = _parse_agent_turn(json.dumps(payload, ensure_ascii=False))
    assert turn.kind == "tool_requests"
    assert turn.tool_requests[0].tool.value == "order_snapshot"


def test_parse_agent_turn_does_not_wrap_arbitrary_json() -> None:
    from pydantic import ValidationError

    from aiops_diagnostics.agent_engine import _parse_agent_turn

    # an object that is neither a wrapped turn nor an unwrapped payload
    with pytest.raises((ValidationError, ValueError)):
        _parse_agent_turn(json.dumps({"unrelated": "field"}))


def test_parse_agent_turn_accepts_flattened_diagnosis_with_kind() -> None:
    """GLM may emit kind=diagnosis but flatten the diagnosis fields at top level."""
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    payload = {"kind": "diagnosis", **_diagnosis_payload()}
    turn = _parse_agent_turn(json.dumps(payload, ensure_ascii=False))
    assert turn.kind == "diagnosis"
    assert turn.diagnosis is not None
    assert turn.diagnosis.summary == "正常结束"
    assert turn.diagnosis.root_cause == "后端异常结束"


def test_parse_agent_turn_accepts_flattened_diagnosis_in_fence_with_prose() -> None:
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    payload = {"kind": "diagnosis", **_diagnosis_payload()}
    text = "Here is the diagnosis.\n\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    turn = _parse_agent_turn(text)
    assert turn.kind == "diagnosis"
    assert turn.diagnosis.confidence.value == "high"


def test_parse_agent_turn_accepts_identity_echo_on_tool_requests() -> None:
    """Regression 2026-09-11 (canary-dashscope qwen3.8-max, diagnosis dx_70c4fb27):

    A provider echoed incident_id/order_no/tenant_id (which are also
    AgentDiagnosis fields) on a valid tool_requests turn. The strict parse
    rejected it on extra keys and the wrap misread the echo as a flattened
    diagnosis, failing with a misleading "diagnosis fields missing" error
    three times until the run blocked. The echo must be ignored and the
    requests honored.
    """
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    payload = {
        "kind": "tool_requests",
        "incident_id": "incident-abc123",
        "order_no": "ORDER-1",
        "tenant_id": None,
        "tool_requests": [{"tool": "order_snapshot", "reason": "取证"}],
    }
    turn = _parse_agent_turn("```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")
    assert turn.kind == "tool_requests"
    assert len(turn.tool_requests) == 1
    assert turn.tool_requests[0].tool.value == "order_snapshot"
    assert turn.diagnosis is None


def test_parse_agent_turn_recovers_requests_key_rename() -> None:
    """Regression 2026-09-11: a provider named the array "requests" instead
    of "tool_requests". Recover the payload instead of failing the turn."""
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    payload = {
        "kind": "tool_requests",
        "requests": [{"tool": "order_snapshot", "reason": "取证"}],
    }
    turn = _parse_agent_turn(json.dumps(payload, ensure_ascii=False))
    assert turn.kind == "tool_requests"
    assert len(turn.tool_requests) == 1


def test_parse_agent_turn_drops_interim_diagnosis_on_tool_requests() -> None:
    """Regression 2026-09-11: a provider attached a status=blocked interim
    diagnosis alongside valid tool_requests. The requests win; the note is
    dropped instead of failing the turn (previously a value_error on
    "tool_requests turn requires requests and no diagnosis")."""
    from aiops_diagnostics.agent_engine import _parse_agent_turn

    payload = {
        "kind": "tool_requests",
        "tool_requests": [{"tool": "order_snapshot", "reason": "取证"}],
        "diagnosis": {**_diagnosis_payload(), "status": "blocked", "extra_note": "ignored"},
    }
    turn = _parse_agent_turn(json.dumps(payload, ensure_ascii=False))
    assert turn.kind == "tool_requests"
    assert len(turn.tool_requests) == 1
    assert turn.diagnosis is None


def test_parse_agent_turn_still_rejects_identity_echo_without_requests() -> None:
    """Identity keys alone (no payload fields, no requests) are not a turn."""
    from pydantic import ValidationError

    from aiops_diagnostics.agent_engine import _parse_agent_turn

    payload = {"kind": "tool_requests", "incident_id": "incident-abc123"}
    with pytest.raises((ValidationError, ValueError)):
        _parse_agent_turn(json.dumps(payload))


def test_coordinator_injects_output_language_into_prompts(tmp_path: Path) -> None:
    """The diagnosis initial prompt carries the output-language directive (#204)."""
    _, manifest, workspace, journal, tools, settings = _context(tmp_path)
    session = _FakeSession([_tool_request(), _diagnosis(manifest)])
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        tools,
        settings,
        session_factory=lambda *_: session,
        language="de",
    )

    result = coordinator.run()

    assert result.status.value == "diagnosed"
    assert "German" in session.prompts[0]
    # The incident manifest (evidence snapshot) is embedded verbatim.
    assert manifest.order_no in session.prompts[0]


def test_coordinator_default_language_is_simplified_chinese(tmp_path: Path) -> None:
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

    coordinator.run()

    assert "Simplified Chinese" in session.prompts[0]
