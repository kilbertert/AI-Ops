from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aiops_diagnostics.agent_contracts import AgentDiagnosis
from aiops_diagnostics.agent_engine import AgentCoordinator, ProgressCallback
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import resolve_provider_api_key
from aiops_diagnostics.config import Settings
from aiops_diagnostics.diagnostic_tools import DiagnosticToolExecutor
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
        )
        return coordinator.run()


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
