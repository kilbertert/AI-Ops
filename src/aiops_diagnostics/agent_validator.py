from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from aiops_diagnostics.agent_contracts import (
    AgentDiagnosis,
    Confidence,
    DiagnosisStatus,
    IncidentManifest,
    ToolName,
)
from aiops_diagnostics.answer_language import (
    AnswerBlock,
    AnswerSurface,
    answer_chinese_leak,
)
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.journal import EvidenceJournal, JournalEntry
from aiops_diagnostics.redaction import contains_secret

EXECUTED_MUTATION = re.compile(
    r"(?:"
    r"(?:已|已经|成功|自动|完成|执行了?|进行了?)\s*"
    r"(?:订单)?(?:重算|退款|补发|重放|重试下发|重启|修改|更新|删除|写入)"
    r"|(?:重算|退款|补发|重放|重试下发|重启|修改订单|更新订单|删除订单|写入数据库)"
    r"\s*(?:已完成|成功|完成|完毕)"
    r"|\b(?:refunded|recalculated|replayed|resent|restarted|modified|updated|deleted)\b"
    r")",
    re.IGNORECASE,
)


class AgentResultValidator:
    def __init__(
        self,
        manifest: IncidentManifest,
        journal: EvidenceJournal,
        *,
        sensitive_values: Iterable[str] = (),
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self.manifest = manifest
        self.journal = journal
        self.sensitive_values = tuple(sensitive_values)
        self.language = language

    def validate(self, result: AgentDiagnosis) -> list[str]:
        errors: list[str] = []
        if result.incident_id != self.manifest.incident_id:
            errors.append("incident_id 与不可变 manifest 不一致")
        if result.order_no != self.manifest.order_no:
            errors.append("order_no 与不可变 manifest 不一致")
        if result.tenant_id != self.manifest.tenant_id:
            errors.append("tenant_id 与不可变 manifest 不一致")

        entries = {entry.evidence_id: entry for entry in self.journal.entries()}
        referenced = set(result.evidence_ids)
        referenced.update(
            evidence_id for hypothesis in result.hypotheses for evidence_id in hypothesis.evidence_ids
        )
        missing = sorted(referenced.difference(entries))
        if missing:
            errors.append("引用了不存在的 evidence_id: " + ", ".join(missing))
        for evidence_id in sorted(referenced.intersection(entries)):
            if not self._artifact_valid(entries[evidence_id]):
                errors.append(f"证据文件缺失或哈希不匹配: {evidence_id}")

        direct_successful = {
            entry.evidence_id
            for entry in entries.values()
            if entry.status == "success" and entry.tool != ToolName.KNOWN_RUNBOOK.value
        }
        if result.status == DiagnosisStatus.DIAGNOSED:
            if not result.root_cause.strip():
                errors.append("diagnosed 结果必须给出 root_cause")
            if not direct_successful.intersection(referenced):
                errors.append("diagnosed 结果至少需要引用一条非 known_runbook 的直接成功证据")
            for hypothesis in result.hypotheses:
                if not direct_successful.intersection(hypothesis.evidence_ids):
                    errors.append(f"假设“{hypothesis.title}”没有引用非 known_runbook 的直接成功证据")

        failed_sources = set(self.journal.failed_sources())
        if not failed_sources.issubset(result.failed_sources):
            missing_failures = sorted(failed_sources.difference(result.failed_sources))
            errors.append("结果遗漏失败数据源: " + ", ".join(missing_failures))
        invented_failures = sorted(set(result.failed_sources).difference(failed_sources))
        if invented_failures:
            errors.append("结果声明了日志中不存在的失败数据源: " + ", ".join(invented_failures))
        if failed_sources and not result.limitations:
            errors.append("存在失败数据源时必须说明 limitations")
        if failed_sources and result.confidence == Confidence.HIGH:
            errors.append("存在失败数据源时置信度不得为 high")

        order_entries = [entry for entry in entries.values() if entry.tool == ToolName.ORDER_SNAPSHOT]
        if (
            order_entries
            and order_entries[-1].status == "failed"
            and (result.status != DiagnosisStatus.BLOCKED or result.confidence != Confidence.LOW)
        ):
            errors.append("订单主数据源失败时结果必须 blocked 且 confidence=low")
        if result.status != DiagnosisStatus.DIAGNOSED and result.confidence == Confidence.HIGH:
            errors.append("inconclusive/blocked 结果置信度不得为 high")

        latest_by_tool: dict[str, JournalEntry] = {}
        for entry in entries.values():
            latest_by_tool[entry.tool] = entry
        latest_order = latest_by_tool.get(ToolName.ORDER_SNAPSHOT.value)
        if latest_order and latest_order.status == "success":
            try:
                order_payload = self.journal.load_payload(latest_order)
            except ValueError:
                order_payload = None
            orders = order_payload.get("orders", []) if isinstance(order_payload, dict) else []
            if len(orders) != 1:
                if result.status == DiagnosisStatus.DIAGNOSED:
                    errors.append("订单主数据未唯一确认时不得返回 diagnosed 结果")
                if result.confidence != Confidence.LOW:
                    errors.append("订单主数据未唯一确认时置信度必须为 low")
            elif str(orders[0].get("type")) == "1" and result.confidence != Confidence.LOW:
                errors.append("两轮车专项规则未实现时置信度必须为 low")

        direct_tools = {
            entry.tool
            for entry in entries.values()
            if entry.status == "success" and entry.tool != ToolName.KNOWN_RUNBOOK.value
        }
        if result.confidence == Confidence.HIGH and len(direct_tools) < 3:
            errors.append("high 置信度至少需要三类直接成功证据")
        unresolved_blocked = sorted(
            entry.tool for entry in latest_by_tool.values() if entry.status == "blocked"
        )
        if unresolved_blocked and result.confidence == Confidence.HIGH:
            errors.append("存在未解决的 blocked 工具时置信度不得为 high")
        if unresolved_blocked and not result.limitations:
            errors.append("存在未解决的 blocked 工具时必须说明 limitations")

        document = result.model_dump(mode="json")
        rendered = json.dumps(document, ensure_ascii=False)
        if contains_secret(rendered, self.sensitive_values):
            errors.append("结果包含运行时敏感值")
        if EXECUTED_MUTATION.search(rendered):
            errors.append("结果声称执行了第一版禁止的业务变更动作")
        # The language verdict is the shared guard's, exactly as on every other
        # surface (ADR-0007): the gate predicate and the raw `chinese_leak` call
        # that used to sit here defined the rule a second time (#365). The
        # DISPOSITION stays this surface's own — ADR-0007 allows that difference,
        # because this is the one surface with a contract-repair retry loop, so a
        # leak arrives as a validation error rather than as the localized-fallback
        # alert the surfaces without a loop raise.
        #
        # A diagnosis carries no media blocks and no titles, so the projection is
        # the degenerate one: every judgeable string the contract holds becomes
        # text, and nothing is exempted.
        leak = answer_chinese_leak(
            AnswerSurface(
                blocks=tuple(AnswerBlock(kind="text", text=text) for text in _judgeable_text(document))
            ),
            self.language,
        )
        if leak:
            errors.append(
                f"结果语言为 {self.language}，但仍含中文字符 {leak}："
                "受控词表值（枚举标签、停因等）须按语义翻译，不得原样附带中文原文"
            )
        return errors

    def blocked_result(self, reason: str) -> AgentDiagnosis:
        entries = self.journal.entries()
        return AgentDiagnosis(
            incident_id=self.manifest.incident_id,
            order_no=self.manifest.order_no,
            tenant_id=self.manifest.tenant_id,
            status=DiagnosisStatus.BLOCKED,
            summary="诊断运行未能生成满足证据合同的结论",
            root_cause=reason,
            confidence=Confidence.LOW,
            evidence_ids=[entry.evidence_id for entry in entries],
            hypotheses=[],
            limitations=[reason],
            failed_sources=self.journal.failed_sources(),
            next_steps=["由工程师检查运行事件和证据日志后决定是否恢复同一诊断线程"],
        )

    def _artifact_valid(self, entry: JournalEntry) -> bool:
        try:
            path = self.journal.artifact_path(entry)
            return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry.artifact_sha256
        except (OSError, ValueError):
            return False


def _judgeable_text(document: Any) -> Iterator[str]:
    """Yield every string a JSON-shaped diagnosis document holds, in order.

    The projection into the guard's contract type. A diagnosis has no media
    blocks, so there is no kind to read and no title the resource-name exemption
    could apply to: every string the model produced — prose, hypothesis titles,
    enum labels pasted out of the Chinese source — belongs in the judgement.
    Walked rather than listed by field name, so a string added to the contract
    later is judged by the same predicate as the rest instead of silently
    escaping it.

    One string per block, not the serialized document as a single block: a
    parenthesised Chinese gloss is only exempted beside the Latin token it
    follows, and that pair always sits inside one value.
    """
    if isinstance(document, str):
        yield document
    elif isinstance(document, Mapping):
        for child in document.values():
            yield from _judgeable_text(child)
    elif isinstance(document, list):
        for child in document:
            yield from _judgeable_text(child)
