from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aiops_diagnostics.agent_contracts import (
    AgentDiagnosis,
    Confidence,
    DiagnosisStatus,
    Hypothesis,
    IncidentManifest,
    ToolName,
)
from aiops_diagnostics.agent_validator import AgentResultValidator
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.i18n import chinese_leak
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request

VALIDATOR_SOURCE = Path(__file__).parents[1] / "src" / "aiops_diagnostics" / "agent_validator.py"


def _context(tmp_path: Path):
    manifest = IncidentManifest.from_request(
        parse_request("订单 TEST-OCPP-0003 金额异常", tenant_id="TENANT-1")
    )
    workspace = AgentWorkspace.create(Path(__file__).parents[1], tmp_path, manifest)
    journal = EvidenceJournal(workspace, manifest)
    entry = journal.record(
        tool=ToolName.ORDER_SNAPSHOT,
        source="mysql:ch_order_info",
        status="success",
        request={"order_no": manifest.order_no},
        payload={"orders": [{"order_no": manifest.order_no, "tenant_id": manifest.tenant_id}]},
        row_count=1,
    )
    return manifest, journal, entry


def _diagnosis(manifest: IncidentManifest, evidence_id: str) -> AgentDiagnosis:
    return AgentDiagnosis(
        incident_id=manifest.incident_id,
        order_no=manifest.order_no,
        tenant_id=manifest.tenant_id,
        status=DiagnosisStatus.DIAGNOSED,
        summary="订单事实已确认",
        root_cause="订单主数据表明该订单存在",
        confidence=Confidence.MEDIUM,
        evidence_ids=[evidence_id],
        hypotheses=[
            Hypothesis(
                title="主数据存在",
                explanation="订单行可读取",
                evidence_ids=[evidence_id],
            )
        ],
        limitations=[],
        failed_sources=[],
        next_steps=[],
    )


def test_validator_accepts_identity_and_hashed_evidence(tmp_path: Path) -> None:
    manifest, journal, entry = _context(tmp_path)

    errors = AgentResultValidator(manifest, journal).validate(_diagnosis(manifest, entry.evidence_id))

    assert errors == []


def test_validator_rejects_identity_drift_and_secret_leak(tmp_path: Path) -> None:
    manifest, journal, entry = _context(tmp_path)
    result = _diagnosis(manifest, entry.evidence_id).model_copy(
        update={"order_no": "OTHER-ORDER", "root_cause": "leaked-runtime-secret"}
    )

    errors = AgentResultValidator(
        manifest,
        journal,
        sensitive_values=("runtime-secret",),
    ).validate(result)

    assert any("order_no" in error for error in errors)
    assert any("敏感值" in error for error in errors)


@pytest.mark.parametrize(
    "claim",
    [
        "已经退款",
        "执行了订单重算",
        "服务重启成功",
        "the order was refunded",
    ],
)
def test_validator_rejects_claims_that_forbidden_actions_were_executed(
    tmp_path: Path,
    claim: str,
) -> None:
    manifest, journal, entry = _context(tmp_path)
    result = _diagnosis(manifest, entry.evidence_id).model_copy(update={"root_cause": claim})

    errors = AgentResultValidator(manifest, journal).validate(result)

    assert any("禁止的业务变更动作" in error for error in errors)


def test_validator_allows_recommending_a_human_review_without_execution_claim(tmp_path: Path) -> None:
    manifest, journal, entry = _context(tmp_path)
    result = _diagnosis(manifest, entry.evidence_id).model_copy(
        update={"next_steps": ["由工程师确认是否需要退款，本工具未执行任何业务动作"]}
    )

    assert AgentResultValidator(manifest, journal).validate(result) == []


def test_validator_caps_confidence_when_a_source_failed(tmp_path: Path) -> None:
    manifest, journal, entry = _context(tmp_path)
    journal.record(
        tool=ToolName.GUN_TIMESERIES,
        source="tdengine:charging-gun_property",
        status="failed",
        request={},
        payload={},
        error="timeout",
    )
    result = _diagnosis(manifest, entry.evidence_id).model_copy(
        update={
            "confidence": Confidence.HIGH,
            "failed_sources": ["tdengine:charging-gun_property"],
            "limitations": ["TDengine unavailable"],
        }
    )

    errors = AgentResultValidator(manifest, journal).validate(result)

    assert any("不得为 high" in error for error in errors)


def test_validator_rejects_known_runbook_as_the_only_causal_evidence(tmp_path: Path) -> None:
    manifest, journal, _ = _context(tmp_path)
    runbook = journal.record(
        tool=ToolName.KNOWN_RUNBOOK,
        source="deterministic:known_runbook",
        status="success",
        request={"order_no": manifest.order_no},
        payload={"report": {"failed_sources": []}},
    )
    result = _diagnosis(manifest, runbook.evidence_id)

    errors = AgentResultValidator(manifest, journal).validate(result)

    assert any("非 known_runbook" in error for error in errors)


def test_validator_rejects_invented_failures_and_high_confidence_with_blocked_tools(
    tmp_path: Path,
) -> None:
    manifest, journal, entry = _context(tmp_path)
    journal.record(
        tool=ToolName.GUN_TIMESERIES,
        source="harness:dependency",
        status="blocked",
        request={},
        payload={"reason": "missing dependency"},
        error="missing dependency",
    )
    result = _diagnosis(manifest, entry.evidence_id).model_copy(
        update={
            "confidence": Confidence.HIGH,
            "failed_sources": ["tdengine:invented"],
            "limitations": [],
        }
    )

    errors = AgentResultValidator(manifest, journal).validate(result)

    assert any("不存在的失败数据源" in error for error in errors)
    assert any("blocked 工具" in error and "high" in error for error in errors)
    assert any("blocked 工具" in error and "limitations" in error for error in errors)


def test_validator_caps_high_confidence_without_broad_direct_evidence(tmp_path: Path) -> None:
    manifest, journal, entry = _context(tmp_path)
    result = _diagnosis(manifest, entry.evidence_id).model_copy(update={"confidence": Confidence.HIGH})

    errors = AgentResultValidator(manifest, journal).validate(result)

    assert any("三类直接成功证据" in error for error in errors)


def test_validator_rejects_diagnosis_when_order_snapshot_is_not_unique(tmp_path: Path) -> None:
    manifest, journal, _ = _context(tmp_path)
    duplicate = journal.record(
        tool=ToolName.ORDER_SNAPSHOT,
        source="mysql:ch_order_info",
        status="success",
        request={"order_no": manifest.order_no},
        payload={"orders": []},
        row_count=0,
    )
    result = _diagnosis(manifest, duplicate.evidence_id)

    errors = AgentResultValidator(manifest, journal).validate(result)

    assert any("不得返回 diagnosed" in error for error in errors)
    assert any("必须为 low" in error for error in errors)


def test_validator_rejects_stored_chinese_echoed_in_another_language(tmp_path: Path) -> None:
    """Reproduces the live 41 leak: the stop-reason enum was translated AND echoed."""
    manifest, journal, entry = _context(tmp_path)
    leaked = _diagnosis(manifest, entry.evidence_id).model_copy(
        update={
            "summary": (
                'The order-level stop reason is code -1 with content "余额耗尽停止订单" '
                "(balance exhausted, order stopped)."
            ),
        }
    )

    errors = AgentResultValidator(manifest, journal, language="en").validate(leaked)

    assert any("仍含中文字符" in error for error in errors)


def test_validator_allows_chinese_for_the_chinese_answer(tmp_path: Path) -> None:
    """zh answers are Chinese by design; the check must not fire for the default language."""
    manifest, journal, entry = _context(tmp_path)

    errors = AgentResultValidator(manifest, journal, language="zh").validate(
        _diagnosis(manifest, entry.evidence_id)
    )

    assert not any("中文字符" in error for error in errors)


def test_validator_allows_translated_prose_with_ascii_identifiers(tmp_path: Path) -> None:
    """The intended shape: prose translated, identifiers kept byte-identical, no CJK."""
    manifest, journal, entry = _context(tmp_path)
    translated = _diagnosis(manifest, entry.evidence_id).model_copy(
        update={
            "summary": 'stopped_reason_content = "balance exhausted, order stopped"; '
            "stopped_reason_code=-1; balance_insufficient_stop=1.",
            "root_cause": "The order was ended by an automatic remote stop (ev-001) at 2026-09-12 16:58:51.",
            "hypotheses": [
                Hypothesis(
                    title="Primary data exists",
                    explanation="The order row is readable",
                    evidence_ids=[entry.evidence_id],
                )
            ],
        }
    )

    errors = AgentResultValidator(manifest, journal, language="en").validate(translated)

    assert not any("中文字符" in error for error in errors)


# --------------------------------------------------------------------------
# The language verdict comes from the shared guard (#365, ADR-0007)
#
# ADR-0007 unifies the JUDGEMENT, not the disposition: the diagnostic surface is
# the one with a repair-retry loop, so a leak lands in these validation errors
# rather than in a localized fallback, and that difference stays. What may not
# stay is a second copy of the predicate — the validator used to import
# `chinese_leak` and rewrite the language gate beside it, so "who decides" was
# defined twice. Source-level assertions, because what is being prevented is a
# future edit rather than a current behaviour.
# --------------------------------------------------------------------------


def _validator_module() -> ast.Module:
    """The validator's own source. What is guarded here is an edit to it."""
    return ast.parse(VALIDATOR_SOURCE.read_text(encoding="utf-8"), filename=str(VALIDATOR_SOURCE))


def _identifiers(tree: ast.Module) -> tuple[set[str], list[str], dict[str, str]]:
    """Names, plain calls and imports in ``tree``.

    Imports are kept separately, so a guard can tell "calls the shared entry"
    from "calls a local function of the same name" — the difference is the whole
    point, and this repo has already had guards that passed while the thing they
    pointed at had quietly been replaced.
    """
    names: set[str] = set()
    calls: list[str] = []
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported = alias.asname or alias.name
                names.add(imported)
                imports[imported] = node.module or ""
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".", 1)[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.append(node.func.id)
    return names, calls, imports


def test_the_validator_has_no_language_gate_of_its_own() -> None:
    """The gate predicate and the verdict exist once, in the shared guard.

    A validator that keeps `if self.language in NON_CHINESE_LANGUAGES` no longer
    inherits the shared entry's other rules — unsupported languages left alone,
    the resource-name exemption, whatever the guard grows next — so the two
    copies drift apart the moment one of them changes.
    """
    tree = _validator_module()
    names, calls, imports = _identifiers(tree)
    function_defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    defined = {node.name for node in ast.walk(tree) if isinstance(node, function_defs)}

    assert "chinese_leak" not in names, (
        "the verdict is i18n.chinese_leak's alone; judge through the shared guard"
    )
    assert "NON_CHINESE_LANGUAGES" not in names, "the gate belongs to the shared guard, not to this surface"
    assert imports.get("answer_chinese_leak") == "aiops_diagnostics.answer_language", (
        "the entry must be the shared guard's own, not this module's copy of the name"
    )
    assert "answer_chinese_leak" not in defined, "a local `answer_chinese_leak` would shadow the shared entry"
    assert calls.count("answer_chinese_leak") == 1, "one call to the shared entry, at the finalisation point"
    assert "record_answer_language_fallback" not in names, (
        "the diagnostic surface disposes of a leak as a validation error; "
        "the fallback alert is for the surfaces with no repair loop"
    )


def test_the_shared_entry_decides_what_the_diagnosis_already_decided(tmp_path: Path) -> None:
    """Sharing the entry must not change the verdict, only who reaches it.

    Pinned against `i18n.chinese_leak` on the whole diagnosis document — the
    judgement this validator made when it owned a copy of the rule — with the
    Chinese moved into a different field of the contract each time, so a
    projection that covered only the field the live 41 leak happened to sit in
    fails here rather than in production.
    """
    manifest, journal, entry = _context(tmp_path)
    translated = {
        "summary": "The order facts were confirmed from the order snapshot.",
        "root_cause": "The order row still exists in the order master data.",
        "hypotheses": [
            Hypothesis(
                title="Primary data exists",
                explanation="The order row is readable",
                evidence_ids=[entry.evidence_id],
            )
        ],
        "limitations": ["The gun time series was not read in this run."],
        "next_steps": ["An engineer reviews the run events before resuming."],
        "failed_sources": ["tdengine:charging-gun_time_series"],
    }
    # The same contract, one field carrying the Chinese the model pasted out of
    # the source data at a time: whichever field is judged, the leaked
    # characters must be exactly the ones that field holds.
    one_chinese_field = {
        "summary": 'The order-level stop reason is "余额耗尽停止订单".',
        "root_cause": "订单主数据表明该订单存在。",
        "hypotheses": [
            Hypothesis(
                title="主数据存在",
                explanation="充电桩未按协议上报心跳。",
                evidence_ids=[entry.evidence_id],
            )
        ],
        "limitations": ["本轮未读取充电枪时序数据。"],
        "next_steps": ["由工程师检查运行事件后决定是否恢复。"],
        "failed_sources": ["tdengine:充电枪时序数据"],
    }
    validator = AgentResultValidator(manifest, journal, language="en")

    for field, value in one_chinese_field.items():
        # Every other field translated, so the leaked characters can only have
        # come from the one field under test.
        update = {field: value, **{name: item for name, item in translated.items() if name != field}}
        diagnosis = _diagnosis(manifest, entry.evidence_id).model_copy(update=update)
        # The verdict the surface must keep: distinct Chinese characters, sorted,
        # exactly as the shared entry is asked for them and reports them.
        expected = chinese_leak(json.dumps(diagnosis.model_dump(mode="json"), ensure_ascii=False))

        assert expected, f"the fixture for {field} must carry Chinese"
        language_errors = [error for error in validator.validate(diagnosis) if "但仍含中文字符" in error]
        assert f"但仍含中文字符 {expected}：" in "".join(language_errors), field


def test_an_unsupported_language_is_left_alone_by_the_shared_exit(tmp_path: Path) -> None:
    """A language the guard does not police is the shared entry's call now.

    The surfaces already fall back to the default language upstream, so this
    exit came with the shared entry rather than being rewritten here — the copy
    of the gate this validator used to hold had no such arm at all.
    """
    manifest, journal, entry = _context(tmp_path)
    leaked = _diagnosis(manifest, entry.evidence_id).model_copy(
        update={"root_cause": "订单主数据表明该订单存在"}
    )

    errors = AgentResultValidator(manifest, journal, language="ja").validate(leaked)

    assert not any("中文字符" in error for error in errors)
