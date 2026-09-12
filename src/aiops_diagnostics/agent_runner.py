from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_contracts import AgentDiagnosis
from aiops_diagnostics.agent_engine import AgentCoordinator, ProgressCallback
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import AgentRuntimeError, resolve_provider_api_key
from aiops_diagnostics.config import Settings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, language_name
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.models import DiagnosticRequest
from aiops_diagnostics.query_scope import QueryScope
from aiops_diagnostics.sources import (
    DiagnosticSources,
    FixtureSources,
    live_sources,
    scoped_live_sources,
)


def run_agent_diagnosis(
    workspace: AgentWorkspace,
    request: DiagnosticRequest,
    settings: Settings,
    fixture: Path | None,
    *,
    progress_callback: ProgressCallback | None = None,
    allowed_tenants: set[str] | None = None,
    provider: str | None = None,
    key_slot: str | None = None,
    scope: QueryScope | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> AgentDiagnosis:
    """Run the shared read-only agent path for local CLI and gateway workers."""
    manifest = workspace.load_manifest()
    selected_provider = settings.agent.select_provider(provider)
    provider_key = resolve_provider_api_key(settings.agent, provider=selected_provider, key_slot=key_slot)
    journal = EvidenceJournal(workspace, manifest)
    with _agent_sources(settings, fixture, scope=scope) as sources:
        tools = DiagnosticToolExecutor(
            sources,
            request,
            manifest,
            journal,
            safety=settings.safety,
            allowed_tenants=allowed_tenants,
        )
        coordinator = AgentCoordinator(
            workspace,
            manifest,
            journal,
            tools,
            settings.agent,
            provider=selected_provider,
            sensitive_values=(
                settings.mysql.password,
                settings.tdengine.password,
                settings.redis.password,
                provider_key,
            ),
            progress_callback=progress_callback,
            language=language,
        )
        return coordinator.run()


def run_zero_order_answer(
    question: str,
    settings: Settings,
    *,
    provider: str | None = None,
    key_slot: str | None = None,
    project_root: Path | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> dict[str, Any]:
    """Answer a general (zero-order) question via the shared read-only Agent.

    No order identity is created, so the model can reach only the staged
    references (SOP/backend docs) plus its own knowledge — it cannot read any
    order data. Returns ``{"text": str, "reminder": bool}`` (T3/#153).
    """
    from aiops_diagnostics.codex_runtime import SDKCodexSession

    selected_provider = settings.agent.select_provider(provider)
    root = project_root or Path(__file__).resolve().parents[1]
    workspace = AgentWorkspace.create_qa(
        root,
        Path(settings.agent.run_root).expanduser().resolve(),
        provider_base_url=selected_provider.base_url,
        provider=selected_provider.name,
        key_slot=key_slot or selected_provider.resolved_key_slot(),
    )
    session: SDKCodexSession | None = None
    try:
        session = SDKCodexSession(
            workspace,
            settings.agent,
            provider=selected_provider,
        )
        prompt = (
            "请回答用户的这个一般问题，只输出 JSON（遵循结构化输出 schema），"
            f"text 字段必须使用{language_name(language)}书写（面向用户的呈现语言），"
            "reminder 字段为 true。\n\n问题：" + question
        )
        result = session.run(prompt)
        payload = json.loads(result.final_response)
        # tolerate markdown fence
        if not isinstance(payload, dict):
            start, end = str(result.final_response).find("{"), str(result.final_response).rfind("}")
            payload = json.loads(str(result.final_response)[start : end + 1])
        text = str(payload.get("text") or "")
        reminder = bool(payload.get("reminder"))
        if not text:
            raise AgentRuntimeError("zero-order answer returned empty text")
        return {"text": text, "reminder": reminder}
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()


@contextmanager
def _agent_sources(
    settings: Settings,
    fixture: Path | None,
    *,
    scope: QueryScope | None = None,
) -> Iterator[DiagnosticSources]:
    if fixture:
        yield FixtureSources(fixture)
        return
    if scope is not None:
        with scoped_live_sources(settings, scope=scope) as sources:
            yield sources
        return
    with live_sources(settings) as sources:
        yield sources
