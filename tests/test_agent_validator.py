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
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request


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
