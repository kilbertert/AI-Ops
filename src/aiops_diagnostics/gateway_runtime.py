from __future__ import annotations

import contextlib
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_contracts import IncidentManifest
from aiops_diagnostics.agent_runner import run_agent_diagnosis
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.codex_runtime import AgentRuntimeError
from aiops_diagnostics.config import Settings, canonical_provider_base_url, validate_key_slot_name
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayDevice, GatewayStore
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.platform_paths import reference_root
from aiops_diagnostics.redaction import redact_text
from aiops_diagnostics.sources import SourceError

FIXTURE_NAMES = frozenset({"ocpp_consistent.json", "ykc_amount_mismatch.json", "missing_tx_data.json"})


class GatewayRuntime:
    """Server-side owner of database credentials, Codex sessions, and run execution."""

    def __init__(
        self,
        store: GatewayStore,
        gateway_settings: GatewayServerSettings,
        diagnostic_settings: Settings,
    ) -> None:
        self.store = store
        self.gateway_settings = gateway_settings
        self.diagnostic_settings = diagnostic_settings
        self.diagnostic_settings.agent.run_root = str(gateway_settings.data_home / "runs")
        allowed = gateway_settings.allowed_key_slots or (diagnostic_settings.agent.key_slot,)
        self.allowed_key_slots = frozenset(allowed)
        self._executor = ThreadPoolExecutor(
            max_workers=gateway_settings.max_workers,
            thread_name_prefix="aiops-gateway-run",
        )
        self._futures: dict[str, Future[None]] = {}

    @classmethod
    def from_settings(
        cls,
        store: GatewayStore,
        gateway_settings: GatewayServerSettings,
    ) -> GatewayRuntime:
        diagnostic_settings = Settings.from_config(gateway_settings.server_config_file)
        diagnostic_settings.agent.validate()
        diagnostic_settings.ssh.validate()
        return cls(store, gateway_settings, diagnostic_settings)

    def start_run(
        self,
        device: GatewayDevice,
        *,
        problem: str,
        order_no: str | None,
        tenant_id: str | None,
        key_slot: str | None,
        provider: str | None,
        fixture_name: str | None,
    ) -> dict[str, Any]:
        effective_tenant = self._tenant_for_device(device, tenant_id)
        allowed_tenants = {effective_tenant} if effective_tenant else None
        selected_provider = self.diagnostic_settings.agent.select_provider(provider)
        selected_key_slot = validate_key_slot_name(key_slot or selected_provider.resolved_key_slot())
        if selected_key_slot not in self.allowed_key_slots:
            raise ValueError("requested key slot is not allowed by the gateway")
        request = parse_request(problem, order_no=order_no, tenant_id=effective_tenant)
        fixture = self._fixture_path(fixture_name)
        manifest = IncidentManifest.from_request(request)
        workspace = AgentWorkspace.create(
            reference_root(),
            Path(self.diagnostic_settings.agent.run_root),
            manifest,
            fixture_path=fixture,
            provider_base_url=canonical_provider_base_url(selected_provider.base_url),
            provider=selected_provider.name,
            key_slot=selected_key_slot,
        )
        run = self.store.create_run(
            run_id=workspace.run_id,
            workspace_id=device.workspace_id,
            incident_id=manifest.incident_id,
            problem=redact_text(request.problem, preserve=(request.order_no,)),
            order_no=request.order_no,
            tenant_id=request.tenant_id,
            key_slot=selected_key_slot,
            provider=selected_provider.name,
            fixture_name=fixture_name,
            created_by_device=device.device_id,
        )
        self.store.append_event(
            workspace.run_id,
            {"type": "gateway_run_queued", "run_id": workspace.run_id},
        )
        future = self._executor.submit(
            self._execute_run,
            workspace,
            request,
            fixture,
            allowed_tenants,
            selected_provider.name,
        )
        self._futures[workspace.run_id] = future
        future.add_done_callback(lambda _: self._futures.pop(workspace.run_id, None))
        return run

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def list_evidence(self, run_id: str) -> list[dict[str, Any]]:
        """Return redacted evidence metadata for a run (no business payloads)."""
        run_root = Path(self.diagnostic_settings.agent.run_root).expanduser().resolve()
        try:
            workspace = AgentWorkspace.open(run_root, run_id)
            manifest = workspace.load_manifest()
        except (FileNotFoundError, ValueError, OSError):
            return []
        journal = EvidenceJournal(workspace, manifest)
        return [
            {
                "evidence_id": entry.evidence_id,
                "tool": entry.tool,
                "source": entry.source,
                "status": entry.status,
                "request": entry.request,
                "row_count": entry.row_count,
                "error": entry.error,
            }
            for entry in journal.entries()
        ]

    def _execute_run(
        self,
        workspace: AgentWorkspace,
        request,
        fixture: Path | None,
        allowed_tenants: set[str] | None = None,
        provider: str | None = None,
    ) -> None:
        run_id = workspace.run_id
        self.store.update_run(run_id, status="running")
        self.store.append_event(run_id, {"type": "gateway_worker_started", "run_id": run_id})
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        settings.agent.run_root = self.diagnostic_settings.agent.run_root
        state_key_slot = workspace.load_state().key_slot
        settings.agent.key_slot = state_key_slot
        try:
            result = run_agent_diagnosis(
                workspace,
                request,
                settings,
                fixture,
                progress_callback=lambda event: self.store.append_event(run_id, _public_event(event)),
                allowed_tenants=allowed_tenants,
                provider=provider,
                key_slot=state_key_slot,
            )
        except (AgentRuntimeError, SourceError) as exc:
            error_message = _public_error_message(exc, request.order_no)
            self.store.update_run(
                run_id,
                status="interrupted",
                error_type=exc.__class__.__name__,
                error_message=error_message,
            )
            self.store.append_event(
                run_id,
                {
                    "type": "gateway_run_interrupted",
                    "error_type": exc.__class__.__name__,
                    "error_message": error_message,
                },
            )
            return
        except Exception as exc:
            error_message = _public_error_message(exc, request.order_no)
            self.store.update_run(
                run_id,
                status="failed",
                error_type=exc.__class__.__name__,
                error_message=error_message,
            )
            self.store.append_event(
                run_id,
                {
                    "type": "gateway_run_failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": error_message,
                },
            )
            return
        self.store.update_run(
            run_id,
            status=result.status.value,
            confidence=result.confidence.value,
            summary=result.summary,
            result=result.model_dump(mode="json"),
        )
        self.store.append_event(
            run_id,
            {"type": "gateway_run_completed", "status": result.status.value},
        )

    def _fixture_path(self, fixture_name: str | None) -> Path | None:
        if fixture_name is None:
            return None
        if not self.gateway_settings.allow_fixtures:
            raise ValueError("gateway fixture execution is disabled")
        if fixture_name not in FIXTURE_NAMES:
            raise ValueError("unsupported fixture name")
        path = reference_root() / "examples" / "fixtures" / fixture_name
        if not path.is_file():
            raise ValueError("gateway fixture is missing")
        return path

    @staticmethod
    def _tenant_for_device(device: GatewayDevice, requested: str | None) -> str | None:
        if device.tenant_id is None:
            return requested
        if requested and requested != device.tenant_id:
            raise ValueError("requested tenant does not match the enrolled device scope")
        return device.tenant_id


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "at",
        "type",
        "run_id",
        "incident_id",
        "resumed",
        "thread_id",
        "turn_id",
        "turn_number",
        "max_turns",
        "elapsed_seconds",
        "tools",
        "attempt",
        "status",
        "error_type",
        "error_message",
    }
    result = {key: value for key, value in event.items() if key in allowed}
    outcomes = event.get("outcomes")
    if isinstance(outcomes, list):
        result["outcomes"] = [
            {key: item.get(key) for key in ("tool", "status", "evidence_id", "source", "reused")}
            for item in outcomes
            if isinstance(item, dict)
        ]
    return result


def _public_error_message(error: Exception, order_no: str | None) -> str:
    """Expose a bounded, redacted diagnostic reason without server secrets."""
    message = redact_text(str(error), preserve=(order_no or "",))
    return message[:1000] if message else error.__class__.__name__


def close_gateway_runtime(runtime: GatewayRuntime) -> None:
    with contextlib.suppress(Exception):
        runtime.shutdown()
