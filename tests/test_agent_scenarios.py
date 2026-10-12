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
                    # The coordinator runs these scenarios with language="en", so the
                    # scripted answer must read as English — a Chinese answer here is
                    # exactly the leak the language check rejects.
                    "summary": "The synthetic scenario finished checking its evidence",
                    "root_cause": "The synthetic root cause is determined by the evidence chain",
                    "confidence": "medium" if failed_sources else "high",
                    "evidence_ids": evidence_ids,
                    "hypotheses": [
                        {
                            "title": "Scenario hypothesis",
                            "explanation": "Supported by the evidence the tools returned",
                            "evidence_ids": evidence_ids,
                        }
                    ],
                    "limitations": ["one data source was unavailable"] if failed_sources else [],
                    "failed_sources": failed_sources,
                    "next_steps": ["A human reviews the synthetic evidence"],
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
        language="en",
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


def test_initial_prompt_asks_for_evidence_translation_but_keeps_identifiers(
    tmp_path: Path,
) -> None:
    """41 live (2026-09-18): an English diagnosis read

        summary: Order ... finished with status 1 and stop reason
                 '余额耗尽停止订单' (stopped_reason_code=-1, ...)

    The surrounding sentence was English, but the raw Chinese enum value was
    copied in verbatim. The old prompt asked to keep "quoted evidence verbatim
    regardless of output language", so the model was following instructions —
    on a response the END USER reads (product call: these are for users;
    engineers have other tooling).

    The rule now distinguishes prose from identifiers: translate the evidence,
    keep identifiers/codes/numbers byte-identical so they still match the system
    of record."""
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path / "en", manifest)
    journal = EvidenceJournal(workspace, manifest)
    executor = DiagnosticToolExecutor(
        FixtureSources(FIXTURES / "ocpp_consistent.json"),
        request,
        manifest,
        journal,
        safety=SafetySettings(),
    )
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
        language="en",
    ).run()

    prompt = session.prompts[0]
    # The output language is named.
    assert "English" in prompt
    # Evidence is to be translated, with the stored-Chinese cause spelled out.
    assert "Translate evidence" in prompt
    assert "余额耗尽停止订单" in prompt, "the concrete case should be exemplified"
    # Identifiers must survive translation.
    assert "EXACTLY as stored" in prompt
    normalised = " ".join(prompt.split())
    for kept in ("order numbers", "evidence IDs", "field names", "error and status codes", "timestamps"):
        assert kept in normalised, f"{kept} must be named as verbatim"
    # The old blanket rule must be gone: it is what produced the report.
    assert "quoted evidence verbatim regardless of output language" not in prompt


def _prompt_for(tmp_path: Path, *, methodology: str = "") -> str:
    """The initial prompt a coordinator builds, without running a turn."""
    request = parse_request("订单 TEST-OCPP-0003 金额异常")
    manifest = IncidentManifest.from_request(request)
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    executor = DiagnosticToolExecutor(
        FixtureSources(FIXTURES / "ocpp_consistent.json"),
        request,
        manifest,
        journal,
        safety=SafetySettings(),
    )
    coordinator = AgentCoordinator(
        workspace,
        manifest,
        journal,
        executor,
        AgentSettings(codex_bin="/bin/true"),
        language="en",
        methodology=methodology,
    )
    return coordinator._initial_prompt()


def test_an_empty_methodology_leaves_the_diagnosis_prompt_byte_identical(tmp_path: Path) -> None:
    """#695 的回归边界：没有运维配方时，提示词与本票之前逐字相同。

    这条断言的对象是**字符串相等**而不是「含有某段」。任何一次无意改动——多加一个
    空行、把段落顺序挪一下——都会在 consumer 那条链上产生一个用户可见的差异，而
    「看起来差不多」的断言抓不到它。
    """
    baseline = _prompt_for(tmp_path / "baseline")
    explicit_empty = _prompt_for(tmp_path / "empty", methodology="")
    whitespace = _prompt_for(tmp_path / "ws", methodology="  \n  ")

    assert explicit_empty == baseline
    assert whitespace == baseline


def test_the_methodology_does_not_outrank_the_harness_rules(tmp_path: Path) -> None:
    """运维配方说明「怎么查」，因此我方的每条规则都排在它**之后**。

    顺序本身**不是**强制机制（工具层与校验器才是），但它决定了模型把哪一段读成
    许可、哪一段读成边界——把运营可编辑的正文放在护栏之后，等于让最后一句成为
    规则；因此这里断言的是「配方在前、规则在后」，而不是随手的拼接顺序。
    """
    prompt = _prompt_for(tmp_path, methodology="OPS-METHOD-先核对设备上报")
    marker = prompt.index("OPS-METHOD-先核对设备上报")

    assert "OPS-METHOD-先核对设备上报" in prompt
    # Every harness rule is stated after the operator-editable text, so the
    # last thing the model reads about language/identifiers/schema is ours.
    assert marker < prompt.index("Write the ENTIRE response in English")
    assert marker < prompt.index("EXACTLY as stored")
    assert marker < prompt.index("Return only the structured response required by the output schema")
    # The methodology does not displace the manifest or the tool list.
    assert marker < prompt.index("Available read-only evidence tools")
    # The block names itself as methodology, not as a second contract.
    assert "运维方法论" in prompt


def test_the_guardrail_text_is_not_parameterised_by_entry_or_tenant() -> None:
    """护栏文本住在**没有入口/租户参数**的函数里，这是一个源码级判据。

    「Dify 的提示词不能覆盖护栏」这句话如果不落成可执行判据，就会在下一个人
    「顺手传个 tenant 进去」时悄悄失效。判据是：这两处文本的生产者签名里没有
    任何能改变它们的输入。
    """
    import inspect

    from aiops_diagnostics.agent_workspace import _qa_runtime_instructions, _runtime_instructions
    from aiops_diagnostics.codex_runtime import _developer_instructions

    assert list(inspect.signature(_runtime_instructions).parameters) == []
    assert list(inspect.signature(_developer_instructions).parameters) == ["language"]
    # The QA contract takes the answer language and the answer *shape* (#700),
    # and nothing else. Both are closed vocabularies owned by this module — the
    # shape is how the operations-analysis narrative turn asks for
    # ``{main_cause, recommendation}`` instead of the zero-order ``{text}``
    # envelope it was otherwise told to produce. Neither can name an entry, a
    # tenant, or a rule, so the guardrails themselves are still unparameterised:
    # everything above the shape block is byte-identical between the two.
    assert list(inspect.signature(_qa_runtime_instructions).parameters) == [
        "language",
        "answer_contract",
    ]
    from aiops_diagnostics.agent_workspace import (  # noqa: PLC0415 - keeps the signature check the subject
        NARRATIVE_ANSWER_CONTRACT,
        QA_ANSWER_CONTRACT,
    )

    assert {QA_ANSWER_CONTRACT, NARRATIVE_ANSWER_CONTRACT} == {"text", "narrative"}
    # The shape is the ONLY difference: everything before the shape block — the
    # "no order data" rule and the filesystem/network guardrail — is shared,
    # byte for byte.
    texts = {c: _qa_runtime_instructions("en", c) for c in (QA_ANSWER_CONTRACT, NARRATIVE_ANSWER_CONTRACT)}
    shared = texts[QA_ANSWER_CONTRACT].split("- Answer the user's general charging")[0]
    assert shared.strip(), "split found nothing — the shape block moved"
    assert texts[NARRATIVE_ANSWER_CONTRACT].startswith(shared)
    # ...and the narrative one really does carry the guardrail, not just the
    # shape line. A review caught an earlier version that had dropped it.
    narrative = texts[NARRATIVE_ANSWER_CONTRACT]
    assert "do not query\n  any order data" in narrative
    assert "Do not inspect parent directories" in narrative
    assert "No network access" in narrative
