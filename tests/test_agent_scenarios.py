import json
from pathlib import Path

import pytest

from aiops_diagnostics.agent_contracts import IncidentManifest
from aiops_diagnostics.agent_engine import AgentCoordinator
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import CodexTurnOutput
from aiops_diagnostics.config import AgentSettings, SafetySettings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.sources import FixtureSources, SourceError

FIXTURES = Path(__file__).parents[1] / "examples/fixtures"


class _ScriptedSession:
    def __init__(self, tool_names: list[str], *, failed_source: str | None = None) -> None:
        self.tool_names = tool_names
        self.failed_source = failed_source
        self._thread_id = "scripted-thread"
        self.prompts: list[str] = []

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def run(self, prompt: str) -> CodexTurnOutput:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            response = {
                "kind": "tool_requests",
                "tool_requests": [
                    {"tool": name, "reason": "收集该场景所需的最小证据"} for name in self.tool_names
                ],
                "diagnosis": None,
            }
        else:
            evidence_ids = [f"ev-{index:03d}" for index in range(1, len(self.tool_names) + 1)]
            failed_sources = [self.failed_source] if self.failed_source else []
            response = {
                "kind": "diagnosis",
                "tool_requests": [],
                "diagnosis": {
                    "schema_version": "1.0",
                    "incident_id": "incident-placeholder",
                    "order_no": "placeholder",
                    "tenant_id": None,
                    "status": "diagnosed",
                    "summary": "合成场景已完成证据核对",
                    "root_cause": "合成场景根因由证据链决定",
                    "confidence": "medium" if failed_sources else "high",
                    "evidence_ids": evidence_ids,
                    "hypotheses": [
                        {
                            "title": "场景假设",
                            "explanation": "由工具返回的证据支持",
                            "evidence_ids": evidence_ids,
                        }
                    ],
                    "limitations": ["一个数据源不可用"] if failed_sources else [],
                    "failed_sources": failed_sources,
                    "next_steps": ["人工复核合成证据"],
                },
            }
            response["diagnosis"]["incident_id"] = self._manifest.incident_id
            response["diagnosis"]["order_no"] = self._manifest.order_no
        return CodexTurnOutput(
            turn_id=f"turn-{len(self.prompts)}",
            final_response=json.dumps(response, ensure_ascii=False),
            usage={},
        )

    def bind_manifest(self, manifest: IncidentManifest) -> None:
        self._manifest = manifest

    def close(self) -> None:
        return None


class _FailingGunSources(FixtureSources):
    def get_gun_samples(self, device, start_time, end_time, tx_serial_no):
        raise SourceError("synthetic TDengine outage")


def _run_scenario(tmp_path: Path, fixture_name: str, tools: list[str], failing: bool = False):
    order_numbers = {
        "ykc_amount_mismatch": "TEST-YKC-0001",
        "missing_tx_data": "TEST-MISSING-0002",
        "ocpp_consistent": "TEST-OCPP-0003",
    }
    request = parse_request(f"订单 {order_numbers[fixture_name]} 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    source = (
        _FailingGunSources(FIXTURES / f"{fixture_name}.json")
        if failing
        else FixtureSources(FIXTURES / f"{fixture_name}.json")
    )
    executor = DiagnosticToolExecutor(
        source,
        request,
        manifest,
        journal,
        safety=SafetySettings(),
    )
    session = _ScriptedSession(tools, failed_source="tdengine:charging-gun_property" if failing else None)
    session.bind_manifest(manifest)
    settings = AgentSettings(codex_bin="/bin/true", max_turns=4)
    result = AgentCoordinator(
        workspace,
        manifest,
        journal,
        executor,
        settings,
        session_factory=lambda *_: session,
    ).run()
    return result, journal, manifest


@pytest.mark.parametrize(
    ("fixture", "tools"),
    [
        ("ykc_amount_mismatch", ["order_snapshot", "fee_snapshot", "gun_timeseries"]),
        ("missing_tx_data", ["order_snapshot", "gun_timeseries", "comm_messages"]),
        ("ocpp_consistent", ["order_snapshot", "fee_snapshot", "gun_timeseries"]),
    ],
)
def test_supported_synthetic_fault_matrix_keeps_identity_and_evidence(
    tmp_path: Path,
    fixture: str,
    tools: list[str],
) -> None:
    first, first_journal, first_manifest = _run_scenario(tmp_path / "first", fixture, tools)
    second, second_journal, second_manifest = _run_scenario(tmp_path / "second", fixture, tools)
    third, third_journal, third_manifest = _run_scenario(tmp_path / "third", fixture, tools)

    assert first.status.value == "diagnosed"
    assert (
        first.incident_id
        == first_manifest.incident_id
        == second_manifest.incident_id
        == third_manifest.incident_id
    )
    assert first.order_no == second.order_no == third.order_no
    assert [entry.tool for entry in first_journal.entries()] == tools
    assert [entry.tool for entry in second_journal.entries()] == tools
    assert [entry.tool for entry in third_journal.entries()] == tools
    assert first.evidence_ids == second.evidence_ids == third.evidence_ids
    assert first.root_cause == second.root_cause == third.root_cause


def test_source_outage_caps_confidence_and_is_not_false_high(tmp_path: Path) -> None:
    result, journal, _ = _run_scenario(
        tmp_path / "outage",
        "ocpp_consistent",
        ["order_snapshot", "gun_timeseries"],
        failing=True,
    )

    assert result.status.value == "diagnosed"
    assert result.confidence.value == "medium"
    assert result.failed_sources == ["tdengine:charging-gun_property"]
    assert journal.entries()[-1].status == "failed"


def test_ladder_scenario_diagnosed_with_failed_peripheral_source(tmp_path: Path) -> None:
    """外围 TDengine 源失败但订单/费用证据完整自洽时，诊断仍是 diagnosed+medium
    （意图相关阶梯的核心验收形态；validator 只封 high，不封结论本身）。"""
    result, journal, _ = _run_scenario(
        tmp_path / "ladder",
        "ocpp_consistent",
        ["order_snapshot", "fee_snapshot", "device_snapshot", "gun_timeseries"],
        failing=True,
    )
    assert result.status.value == "diagnosed", "外围源失败不扣留结论"
    assert result.confidence.value == "medium", "有失败源时置信度封顶 medium"
    assert "tdengine:charging-gun_property" in result.failed_sources
    assert result.limitations, "缺失通道必须写入 limitations"


def test_initial_prompt_includes_environment_notes(tmp_path: Path) -> None:
    """环境预检注记进入初始提示（第一轮规划即可绕开缺失通道）。"""
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    source = FixtureSources(FIXTURES / "ocpp_consistent.json")
    executor = DiagnosticToolExecutor(source, request, manifest, journal, safety=SafetySettings())
    session = _ScriptedSession(["order_snapshot"])
    session.bind_manifest(manifest)
    settings = AgentSettings(codex_bin="/bin/true", max_turns=2)

    AgentCoordinator(
        workspace,
        manifest,
        journal,
        executor,
        settings,
        session_factory=lambda *_: session,
        environment_notes=("环境数据面缺口: charging-gun_property 缺少列 batteryMinTemperature（预检）",),
    ).run()

    first_prompt = session.prompts[0]
    assert "环境能力预检" in first_prompt
    assert "batteryMinTemperature" in first_prompt

    # 无注记时不出现该段
    session2 = _ScriptedSession(["order_snapshot"])
    session2.bind_manifest(manifest)
    workspace2 = AgentWorkspace.create(Path(__file__).parents[1], tmp_path / "plain", manifest)
    journal2 = EvidenceJournal(workspace2, manifest)
    executor2 = DiagnosticToolExecutor(
        FixtureSources(FIXTURES / "ocpp_consistent.json"),
        request,
        manifest,
        journal2,
        safety=SafetySettings(),
    )
    AgentCoordinator(
        workspace2,
        manifest,
        journal2,
        executor2,
        settings,
        session_factory=lambda *_: session2,
    ).run()
    assert "环境能力预检" not in session2.prompts[0]
