from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterable

from pydantic import ValidationError

from aiops_diagnostics.agent_contracts import (
    AgentDiagnosis,
    AgentTurn,
    IncidentManifest,
    RunState,
)
from aiops_diagnostics.agent_validator import AgentResultValidator
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import (
    AgentRuntimeError,
    AgentTurnTimeout,
    CodexSession,
    SDKCodexSession,
)
from aiops_diagnostics.config import AgentSettings
from aiops_diagnostics.diagnostic_tools import (
    TOOL_DESCRIPTIONS,
    DiagnosticToolExecutor,
    ToolOutcome,
)
from aiops_diagnostics.journal import EvidenceJournal

SessionFactory = Callable[[AgentWorkspace, AgentSettings, str | None], CodexSession]


class AgentCoordinator:
    def __init__(
        self,
        workspace: AgentWorkspace,
        manifest: IncidentManifest,
        journal: EvidenceJournal,
        tools: DiagnosticToolExecutor,
        settings: AgentSettings,
        *,
        sensitive_values: Iterable[str] = (),
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.workspace = workspace
        self.manifest = manifest
        self.journal = journal
        self.tools = tools
        self.settings = settings
        self.validator = AgentResultValidator(
            manifest,
            journal,
            sensitive_values=sensitive_values,
        )
        self.session_factory = session_factory or _default_session_factory

    def run(self) -> AgentDiagnosis:
        state = self.workspace.load_state()
        if state.incident_id != self.manifest.incident_id:
            raise ValueError("state.json 的 incident_id 与不可变 manifest 不一致")
        if state.phase == "completed" and (self.workspace.path / "result.json").is_file():
            result = self.workspace.load_result()
            errors = self.validator.validate(result)
            if errors:
                raise ValueError("已完成结果校验失败: " + "; ".join(errors))
            return result
        if not state.next_prompt:
            state = state.model_copy(update={"next_prompt": self._initial_prompt()})
            self.workspace.save_state(state)

        session: CodexSession | None = None
        try:
            session = self.session_factory(self.workspace, self.settings, state.thread_id)
            state = state.model_copy(update={"thread_id": session.thread_id, "phase": "running"})
            self.workspace.save_state(state)
            while state.turn_count < self.settings.max_turns:
                try:
                    output = session.run(state.next_prompt)
                except AgentTurnTimeout:
                    state = state.model_copy(update={"phase": "interrupted"})
                    self.workspace.save_state(state)
                    raise
                state = state.model_copy(update={"turn_count": state.turn_count + 1})
                self.workspace.save_state(state)
                try:
                    turn = AgentTurn.model_validate_json(output.final_response)
                except (ValidationError, ValueError) as exc:
                    state = self._request_contract_repair(state, [f"结构化输出无效: {exc}"])
                    if state.phase == "blocked":
                        return self._finish_blocked(state, "Codex 多次返回无效结构化输出")
                    continue

                if turn.kind == "tool_requests":
                    requested = len(turn.tool_requests)
                    if state.tool_call_count + requested > self.settings.max_tool_calls:
                        return self._finish_blocked(state, "Codex 请求的工具调用超过运行上限")
                    outcomes = self.tools.execute_many(turn.tool_requests)
                    self.workspace.append_event(
                        {
                            "type": "tool_batch_completed",
                            "outcomes": [item.to_dict() for item in outcomes],
                        }
                    )
                    state = state.model_copy(
                        update={
                            "tool_call_count": state.tool_call_count + requested,
                            "next_prompt": self._tool_results_prompt(outcomes),
                        }
                    )
                    self.workspace.save_state(state)
                    continue

                if turn.diagnosis is None:
                    state = self._request_contract_repair(state, ["diagnosis turn 缺少 diagnosis payload"])
                    if state.phase == "blocked":
                        return self._finish_blocked(state, "Codex diagnosis turn 缺少 diagnosis payload")
                    continue
                errors = self.validator.validate(turn.diagnosis)
                if not errors:
                    return self._finish_success(state, turn.diagnosis)
                state = self._request_contract_repair(state, errors)
                if state.phase == "blocked":
                    return self._finish_blocked(
                        state,
                        "Codex 最终诊断未满足证据与安全合同: " + "; ".join(errors),
                    )
            return self._finish_blocked(state, "达到最大 Codex turn 数仍未形成有效诊断")
        except AgentRuntimeError as exc:
            state = self.workspace.load_state().model_copy(update={"phase": "interrupted"})
            self.workspace.save_state(state)
            self.workspace.append_event(
                {"type": "diagnosis_interrupted", "error_type": exc.__class__.__name__}
            )
            raise
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    session.close()

    def _request_contract_repair(self, state: RunState, errors: list[str]) -> RunState:
        attempts = state.validation_attempts + 1
        if attempts > self.settings.max_validation_retries:
            return state.model_copy(update={"validation_attempts": attempts, "phase": "blocked"})
        next_prompt = (
            "Your previous response did not satisfy the immutable delivery contract. "
            "Do not change incident identity or invent evidence. Correct the response using the same "
            "thread and existing journal. If evidence is insufficient, request more tools or return an "
            "inconclusive/blocked diagnosis.\n\nValidation errors:\n- " + "\n- ".join(errors)
        )
        updated = state.model_copy(update={"validation_attempts": attempts, "next_prompt": next_prompt})
        self.workspace.append_event(
            {"type": "diagnosis_validation_failed", "attempt": attempts, "errors": errors}
        )
        self.workspace.save_state(updated)
        return updated

    def _finish_success(self, state: RunState, result: AgentDiagnosis) -> AgentDiagnosis:
        self.workspace.save_result(result)
        self.workspace.save_state(state.model_copy(update={"phase": "completed", "next_prompt": ""}))
        self.workspace.append_event({"type": "diagnosis_completed", "status": result.status.value})
        return result

    def _finish_blocked(self, state: RunState, reason: str) -> AgentDiagnosis:
        result = self.validator.blocked_result(reason)
        self.workspace.save_result(result)
        self.workspace.save_state(state.model_copy(update={"phase": "blocked", "next_prompt": ""}))
        self.workspace.append_event({"type": "diagnosis_blocked", "reason": reason})
        return result

    def _initial_prompt(self) -> str:
        tools = "\n".join(f"- {name.value}: {description}" for name, description in TOOL_DESCRIPTIONS.items())
        incident = json.dumps(self.manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
        return f"""Diagnose the immutable incident below.
Start by reading `incident.json`, `AGENTS.md`, and `references/INDEX.md`.
Then inspect only the staged references that are relevant.

Incident manifest:
```json
{incident}
```

Available read-only evidence tools:
{tools}

Choose the smallest useful evidence set. Normally request `order_snapshot` first.
Return only the structured response required by the output schema.
"""

    @staticmethod
    def _tool_results_prompt(outcomes: list[ToolOutcome]) -> str:
        serialized = []
        for item in outcomes:
            result = item.to_dict()
            if item.model_payload is not None:
                result["payload"] = item.model_payload
            serialized.append(result)
        payload = json.dumps(serialized, ensure_ascii=False, indent=2)
        return f"""The harness completed your bounded read-only tool requests.

Tool outcomes:
```json
{payload}
```

The `payload` field contains the sanitized evidence contents needed for causal
reasoning. The referenced artifact remains the audit source of truth. Do not
assume that a local sandbox can read the artifact path; use the payload above
and request another bounded tool if the payload is truncated.
Treat `failed` as unavailable evidence and `blocked` as an unmet dependency,
not as proof that business data is absent. Request more tools if needed;
otherwise return the final diagnosis with evidence citations.
"""


def _default_session_factory(
    workspace: AgentWorkspace,
    settings: AgentSettings,
    thread_id: str | None,
) -> CodexSession:
    return SDKCodexSession(workspace, settings, thread_id=thread_id)
