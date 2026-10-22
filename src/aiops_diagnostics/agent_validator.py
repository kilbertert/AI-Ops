from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable

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
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, diagnosis_failure_message
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


#: 标识符内部允许的字符。源码是标识符，它后面紧跟这些字符说明声明的是**另一个**
#: 源码（``tdengine:charging-gun_property_extra``），而不是同一源码加了解释。
_IDENTIFIER_TAIL = re.compile(r"[A-Za-z0-9_:./-]")


def _declares(failed_sources: set[str], declared: str) -> str | None:
    """模型声明的一条 failed_source 指向哪个**已知源码**；指向不了任何一个是 ``None``。

    ``"tdengine:charging-gun_property（查询被拒：…）"`` → ``"tdengine:charging-gun_property"``。

    判据是「已知源码 + 非标识符字符」：源码后要么结束，要么接一段解释。不能反过来
    从声明里切出源码 —— 解释本身带冒号（``Invalid column name: batteryMinTemperature``），
    切出来会把解释混进源码。以已知集合为准，两边都不含糊。

    41 上的真实写法是两种并存：``source：解释`` 与 ``source（解释）``，所以不能只认冒号。
    """
    text = declared.strip()
    if text in failed_sources:
        return text
    for source in failed_sources:
        if not text.startswith(source):
            continue
        tail = text[len(source) :]
        # 必须有解释（纯前缀不算声明），且解释的第一个字符不能是标识符字符。
        if tail and not _IDENTIFIER_TAIL.match(tail):
            return source
    return None


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
        # 假设可以落在**任何**真实观测上，包括失败的观测。
        #
        # 旧规则要求每个假设都引用一条「非 runbook 的**成功**证据」，于是一类
        # 合法的假设永远过不了：陈述「这个通道不可用」的假设，能引用的证据只可能是
        # 那条失败/阻塞的条目本身。41 上实测两轮（run-20261022T092735Z、
        # run-20261022T104113Z）都卡在
        # 「假设“环境数据面缺口导致遥测核对不可用”没有引用非 known_runbook 的直接成功证据」。
        #
        # 约束的本意写在 diagnosed 分支那句「至少需要引用一条非 known_runbook 的
        # 直接成功证据」上：结论不能只挂在 runbook 上。失败条目（查询被拒、表不存在）
        # 同样是真实观测，恰恰是「通道不可用」这类结论的直接依据。所以这里保留
        # 「不能只引 runbook」这一条，去掉「必须成功」那一半 —— 后者没有对应的
        # 风险，只是把一个必然发生的写法判成错误。
        direct_evidence = {
            entry.evidence_id
            for entry in entries.values()
            if entry.tool != ToolName.KNOWN_RUNBOOK.value
        }
        if result.status == DiagnosisStatus.DIAGNOSED:
            if not result.root_cause.strip():
                errors.append("diagnosed 结果必须给出 root_cause")
            if not direct_successful.intersection(referenced):
                errors.append("diagnosed 结果至少需要引用一条非 known_runbook 的直接成功证据")
            for hypothesis in result.hypotheses:
                if not direct_evidence.intersection(hypothesis.evidence_ids):
                    errors.append(f"假设“{hypothesis.title}”没有引用非 known_runbook 的真实证据")

        failed_sources = set(self.journal.failed_sources())
        # 模型据实解释失败原因时，不能用「整串相等」判。
        #
        # 2026-10-22 在 41 上量到的确定性死锁（44/63 个 run 卡在重试上限 2）：
        # journal 的失败源是 ``tdengine:charging-gun_property``，模型写成
        # ``tdengine:charging-gun_property（查询被拒：Invalid column name ...）``。
        # 整串比较下，**同一次响应**同时得到两条互相否定的报错 —— 不写解释判
        # 「遗漏」，写了解释判「不存在」。两条规则合起来是一个没有解的约束，
        # 而模型每次都必然踩到它（环境预检已知的列缺失/表缺失每轮都触发）。
        #
        # 判据回到它本来的意思：源码是标识符，按**源码 + 非标识符字符**认；后面那段
        # 是给人读的解释，不参与比对。所以 ``tdengine:charging-gun`` 与
        # ``tdengine:charging-gun_property_x`` 仍然是「不存在」—— 放宽的是
        # 「可以带解释」，不是「可以只写个开头」。
        declared_sources = {item: _declares(failed_sources, item) for item in result.failed_sources}
        covered = {source for source in declared_sources.values() if source is not None}
        if not failed_sources.issubset(covered):
            missing_failures = sorted(failed_sources.difference(covered))
            errors.append("结果遗漏失败数据源: " + ", ".join(missing_failures))
        invented_failures = sorted(item for item, source in declared_sources.items() if source is None)
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

        rendered = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
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
        # the degenerate one: the whole serialised contract is one text block,
        # and nothing in it is exempted. Judging it as one string is the same
        # verdict as judging each value on its own — the JSON separators are
        # never `[A-Za-z0-9]`, so no gloss can be built across two values, and
        # `extra='forbid'` fixes every key as an English field name.
        leak = answer_chinese_leak(
            AnswerSurface(blocks=(AnswerBlock(kind="text", text=rendered),)),
            self.language,
        )
        if leak:
            errors.append(
                f"结果语言为 {self.language}，但仍含中文字符 {leak}："
                "受控词表值（枚举标签、停因等）须按语义翻译，不得原样附带中文原文"
            )
        return errors

    def blocked_result(self, failure_key: str) -> AgentDiagnosis:
        """The delivery contract for a run that produced no conclusion.

        ``failure_key`` selects from a CLOSED set of user-facing endings; it is
        not the engine's free-text reason. The precise reason belongs in the run
        record (`_finish_blocked` records it as a `diagnosis_blocked` event), not
        in text handed to a 管家端 user — those reasons name tool budgets,
        evidence IDs and contract rules, which tell the user nothing about their
        order and arrive in whatever language the harness was written in.

        This is the same engineer/user split the codebase already applies to a
        QA failure: the record is read by an engineer, the response by a user.

        The language comes from ``self.language`` — the validator has always
        held it, but this path never used it.
        """
        entries = self.journal.entries()
        text = diagnosis_failure_message(self.language, failure_key)
        return AgentDiagnosis(
            incident_id=self.manifest.incident_id,
            order_no=self.manifest.order_no,
            tenant_id=self.manifest.tenant_id,
            status=DiagnosisStatus.BLOCKED,
            summary=text,
            root_cause=text,
            confidence=Confidence.LOW,
            evidence_ids=[entry.evidence_id for entry in entries],
            hypotheses=[],
            limitations=[text],
            failed_sources=self.journal.failed_sources(),
            next_steps=[text],
        )

    def _artifact_valid(self, entry: JournalEntry) -> bool:
        try:
            path = self.journal.artifact_path(entry)
            return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry.artifact_sha256
        except (OSError, ValueError):
            return False
