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
from aiops_diagnostics.sources import DiagnosticSources, FixtureSources, live_sources


def run_agent_diagnosis(
    workspace: AgentWorkspace,
    request: DiagnosticRequest,
    settings: Settings,
    fixture: Path | None,
    *,
    progress_callback: ProgressCallback | None = None,
) -> AgentDiagnosis:
    """Run the shared read-only agent path for local CLI and gateway workers."""
    manifest = workspace.load_manifest()
    provider_key = resolve_provider_api_key(settings.agent)
    journal = EvidenceJournal(workspace, manifest)
    with _agent_sources(settings, fixture) as sources:
        tools = DiagnosticToolExecutor(
            sources,
            request,
            manifest,
            journal,
            safety=settings.safety,
        )
        coordinator = AgentCoordinator(
            workspace,
            manifest,
            journal,
            tools,
            settings.agent,
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
def _agent_sources(settings: Settings, fixture: Path | None) -> Iterator[DiagnosticSources]:
    if fixture:
        yield FixtureSources(fixture)
        return
    with live_sources(settings) as sources:
        yield sources
