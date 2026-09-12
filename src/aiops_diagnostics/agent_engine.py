from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable, Iterable
from typing import Any

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
from aiops_diagnostics.config import AgentSettings, ProviderConfig
from aiops_diagnostics.diagnostic_tools import (
    TOOL_DESCRIPTIONS,
    DiagnosticToolExecutor,
    ToolOutcome,
)
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, language_name
from aiops_diagnostics.journal import EvidenceJournal

SessionFactory = Callable[[AgentWorkspace, AgentSettings, ProviderConfig | None, str | None], CodexSession]
ProgressCallback = Callable[[dict[str, Any]], None]

_JSON_FENCE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _parse_agent_turn(final_response: str) -> AgentTurn:
    """Parse the agent's structured turn, tolerating non-OpenAI output styles.

    OpenAI Responses honors ``output_schema`` and returns the wrapped
    ``{kind, tool_requests, diagnosis}`` object as raw JSON. Other providers
    (e.g. GLM via Volcengine Ark) deviate in two ways: they wrap the JSON in a
    markdown code fence (with optional prose prefix), and they may return the
    inner ``AgentDiagnosis`` or ``tool_requests`` payload unwrapped (without the
    ``kind`` discriminator). Try the raw response, each fenced block, and the
    outermost ``{...}`` span; for each, also try wrapping an unwrapped payload
    so the run reaches the contract validator with useful feedback instead of
    failing on ``extra_forbidden``.
    """
    text = final_response.strip()
    candidates: list[str] = [text]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE.finditer(text))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    last_error: ValidationError | ValueError | None = None
    for candidate in candidates:
        try:
            return AgentTurn.model_validate_json(candidate)
        except (ValidationError, ValueError) as exc:
            last_error = exc
        wrapped = _wrap_unstructured_turn(candidate)
        if wrapped is not None:
            try:
                return AgentTurn.model_validate(wrapped)
            except (ValidationError, ValueError) as exc:
                last_error = exc
    assert last_error is not None
    raise last_error


_DIAGNOSIS_FIELDS = frozenset(AgentDiagnosis.model_fields)
# Substantive AgentDiagnosis content — present only when a provider actually
# flattened a diagnosis payload, as opposed to echoing incident identity on a
# tool_requests turn (incident_id/order_no/tenant_id are diagnosis fields too).
_DIAGNOSIS_PAYLOAD_FIELDS = frozenset(
    {"status", "summary", "root_cause", "confidence", "evidence_ids", "hypotheses"}
)


def _wrap_unstructured_turn(candidate: str) -> dict[str, Any] | None:
    """Normalize a provider turn object into the AgentTurn shape.

    GLM and other non-OpenAI providers deviate from the strict
    ``{kind, tool_requests, diagnosis}`` schema in three ways: they omit the
    ``kind`` discriminator, they may flatten the inner ``AgentDiagnosis``
    fields to the top level instead of nesting them under ``diagnosis``, and
    they may echo read-only identity context (``incident_id``, ``order_no``,
    ``tenant_id``, ``source_hash``) alongside a valid payload. ``AgentTurn``
    is ``extra="forbid"``, so those extra keys fail validation even when the
    payload itself is well-formed (observed 2026-09-11 on canary-dashscope
    qwen3.8-max: three valid ``tool_requests`` turns rejected with a
    misleading "diagnosis fields missing" error). This rebuilds the canonical
    shape by extracting the diagnosis (nested or flattened, stripped to
    contract fields) and tool_requests. Returns ``None`` when the candidate
    is not a JSON object or cannot be shaped into a turn.
    """
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    kind = obj.get("kind")
    diagnosis = obj.get("diagnosis") if isinstance(obj.get("diagnosis"), dict) else None
    if diagnosis is not None:
        diagnosis = {key: value for key, value in diagnosis.items() if key in _DIAGNOSIS_FIELDS}
    elif _DIAGNOSIS_PAYLOAD_FIELDS & obj.keys():
        # Only treat the top level as a flattened diagnosis when it carries
        # substantive payload fields (status/summary/root_cause/...). Mere
        # identity echoes (incident_id/order_no/tenant_id) accompany valid
        # tool_requests turns and must not be mistaken for a diagnosis.
        diagnosis = {key: value for key, value in obj.items() if key in _DIAGNOSIS_FIELDS}
    tool_requests = obj.get("tool_requests") if isinstance(obj.get("tool_requests"), list) else []
    if kind in ("diagnosis", "tool_requests"):
        if kind == "tool_requests" and not tool_requests and "requests" in obj:
            # Providers occasionally rename the array (observed "requests");
            # recover the payload instead of failing the whole turn.
            alt = obj["requests"] if isinstance(obj["requests"], list) else []
            tool_requests = [item for item in alt if isinstance(item, dict)]
        if kind == "tool_requests":
            # A tool_requests turn carrying an (interim) diagnosis is a valid
            # intermediate answer in the agent loop's contract in practice:
            # models attach a status=blocked/inconclusive progress note. The
            # requests win; the note is dropped.
            diagnosis = None
        return {"kind": kind, "tool_requests": tool_requests, "diagnosis": diagnosis}
    if diagnosis is not None:
        return {"kind": "diagnosis", "tool_requests": [], "diagnosis": diagnosis}
    if tool_requests:
        return {"kind": "tool_requests", "tool_requests": tool_requests, "diagnosis": None}
    return None


class AgentCoordinator:
    def __init__(
        self,
        workspace: AgentWorkspace,
        manifest: IncidentManifest,
        journal: EvidenceJournal,
        tools: DiagnosticToolExecutor,
        settings: AgentSettings,
        *,
        provider: ProviderConfig | None = None,
        sensitive_values: Iterable[str] = (),
        session_factory: SessionFactory | None = None,
        progress_callback: ProgressCallback | None = None,
        language: str = DEFAULT_LANGUAGE,
        environment_notes: tuple[str, ...] = (),
    ) -> None:
        self.workspace = workspace
        self.manifest = manifest
        self.journal = journal
        self.tools = tools
        self.settings = settings
        self._provider = provider
        self.language = language
        self.environment_notes = tuple(environment_notes)
        self.validator = AgentResultValidator(
            manifest,
            journal,
            sensitive_values=sensitive_values,
        )
        self.session_factory = session_factory or _default_session_factory
        self.progress_callback = progress_callback

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

        self._record_event(
            {
                "type": "diagnosis_started",
                "run_id": self.workspace.run_id,
                "incident_id": self.manifest.incident_id,
                "resumed": bool(state.thread_id),
                "events_path": str(self.workspace.path / "events.jsonl"),
            }
        )

        session: CodexSession | None = None
        try:
            self._record_event({"type": "codex_session_starting"})
            session = self.session_factory(self.workspace, self.settings, self._provider, state.thread_id)
            set_progress_callback = getattr(session, "set_progress_callback", None)
            if callable(set_progress_callback):
                set_progress_callback(self.progress_callback)
            state = state.model_copy(update={"thread_id": session.thread_id, "phase": "running"})
            self.workspace.save_state(state)
            self._record_event({"type": "codex_thread_ready", "thread_id": session.thread_id})
            while state.turn_count < self.settings.max_turns:
                self._record_event(
                    {
                        "type": "codex_turn_waiting",
                        "turn_number": state.turn_count + 1,
                        "max_turns": self.settings.max_turns,
                    }
                )
                try:
                    output = session.run(state.next_prompt)
                except AgentTurnTimeout:
                    state = state.model_copy(update={"phase": "interrupted"})
                    self.workspace.save_state(state)
                    raise
                state = state.model_copy(update={"turn_count": state.turn_count + 1})
                self.workspace.save_state(state)
                try:
                    turn = _parse_agent_turn(output.final_response)
                except (ValidationError, ValueError) as exc:
                    state = self._request_contract_repair(state, [f"结构化输出无效: {exc}"])
                    if state.phase == "blocked":
                        return self._finish_blocked(state, "Codex 多次返回无效结构化输出")
                    continue

                if turn.kind == "tool_requests":
                    requested = len(turn.tool_requests)
                    if state.tool_call_count + requested > self.settings.max_tool_calls:
                        return self._finish_blocked(state, "Codex 请求的工具调用超过运行上限")
                    self._record_event(
                        {
                            "type": "tool_batch_started",
                            "tools": [item.tool.value for item in turn.tool_requests],
                        }
                    )
                    outcomes = self.tools.execute_many(turn.tool_requests)
                    self._record_event(
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
            self._record_event({"type": "diagnosis_interrupted", "error_type": exc.__class__.__name__})
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
        self._record_event({"type": "diagnosis_validation_failed", "attempt": attempts, "errors": errors})
        self.workspace.save_state(updated)
        return updated

    def _finish_success(self, state: RunState, result: AgentDiagnosis) -> AgentDiagnosis:
        self.workspace.save_result(result)
        self.workspace.save_state(state.model_copy(update={"phase": "completed", "next_prompt": ""}))
        self._record_event({"type": "diagnosis_completed", "status": result.status.value})
        return result

    def _finish_blocked(self, state: RunState, reason: str) -> AgentDiagnosis:
        result = self.validator.blocked_result(reason)
        self.workspace.save_result(result)
        self.workspace.save_state(state.model_copy(update={"phase": "blocked", "next_prompt": ""}))
        self._record_event({"type": "diagnosis_blocked", "reason": reason})
        return result

    def _record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = self.workspace.append_event(event)
        if self.progress_callback is not None:
            with contextlib.suppress(Exception):
                self.progress_callback(payload)
        return payload

    def _initial_prompt(self) -> str:
        tools = "\n".join(f"- {name.value}: {description}" for name, description in TOOL_DESCRIPTIONS.items())
        incident = json.dumps(self.manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
        notes_block = ""
        if self.environment_notes:
            notes = "\n".join(f"- {note}" for note in self.environment_notes)
            notes_block = f"""
环境能力预检（advisory）——以下证据通道在本环境已知缺失，请从第一轮规划起绕开，
并将其影响写入 limitations 与 next_steps；如仍需请求，工具会自然失败：
{notes}
"""
        return f"""Diagnose the immutable incident below.
Start by reading `incident.json`, `AGENTS.md`, and `references/INDEX.md`.
Then inspect only the staged references that are relevant.

Incident manifest:
```json
{incident}
```

Available read-only evidence tools:
{tools}
{notes_block}
Choose the smallest useful evidence set. Normally request `order_snapshot` first.
Write every human-readable output field (summary, root_cause, evidence notes,
recommendations) in {language_name(self.language)}. Keep identifiers, codes,
numbers and quoted evidence verbatim regardless of output language.
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
    provider: ProviderConfig | None,
    thread_id: str | None,
) -> CodexSession:
    return SDKCodexSession(workspace, settings, provider=provider, thread_id=thread_id)
