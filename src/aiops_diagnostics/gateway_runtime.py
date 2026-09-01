from __future__ import annotations

import contextlib
import time
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
from aiops_diagnostics.health_curves import build_curves
from aiops_diagnostics.health_report import (
    HEALTH_RULE_VERSION,
    HealthReportError,
    build_minimal_health_report,
)
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.platform_paths import reference_root
from aiops_diagnostics.query_scope import resolve_query_scope
from aiops_diagnostics.redaction import redact_text
from aiops_diagnostics.scope_context import ScopeContext
from aiops_diagnostics.sources import SourceError, scoped_live_sources

FIXTURE_NAMES = frozenset({"ocpp_consistent.json", "ykc_amount_mismatch.json", "missing_tx_data.json"})


def _as_datetime(value):
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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

    def start_health_report(self, context: ScopeContext, order_no: str) -> dict[str, Any]:
        job, created = self.store.create_or_reuse_health_job(
            context.scope_fingerprint,
            order_no,
            HEALTH_RULE_VERSION,
        )
        if not created:
            return job
        future = self._executor.submit(self._execute_health_report, job["job_id"], context, order_no)
        self._futures[job["job_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(job["job_id"], None))
        return job

    def get_health_report(self, context: ScopeContext, job_id: str) -> dict[str, Any] | None:
        return self.store.get_health_job(job_id, context.scope_fingerprint)

    def start_standard_diagnosis(
        self,
        context: ScopeContext,
        order_no: str,
        question: str,
        indicator_code: str | None,
    ) -> dict[str, Any]:
        selected_provider = self.diagnostic_settings.agent.select_provider(None)
        selected_key_slot = validate_key_slot_name(selected_provider.resolved_key_slot())
        if selected_key_slot not in self.allowed_key_slots:
            raise ValueError("default key slot is not allowed by the gateway")
        problem = f"指标 {indicator_code}：{question}" if indicator_code else question
        request = parse_request(
            problem,
            order_no=order_no,
            tenant_id=context.effective_tenant_id,
        )
        manifest = IncidentManifest.from_request(request)
        workspace = AgentWorkspace.create(
            reference_root(),
            Path(self.diagnostic_settings.agent.run_root),
            manifest,
            provider_base_url=canonical_provider_base_url(selected_provider.base_url),
            provider=selected_provider.name,
            key_slot=selected_key_slot,
        )
        diagnosis = self.store.create_standard_diagnosis(
            context.scope_fingerprint,
            order_no,
            question,
            indicator_code,
            internal_run_id=workspace.run_id,
        )
        future = self._executor.submit(
            self._execute_standard_diagnosis,
            diagnosis["diagnosis_id"],
            workspace,
            request,
            context,
            selected_provider.name,
            selected_key_slot,
        )
        self._futures[diagnosis["diagnosis_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(diagnosis["diagnosis_id"], None))
        return diagnosis

    def get_standard_diagnosis(
        self,
        context: ScopeContext,
        diagnosis_id: str,
    ) -> dict[str, Any] | None:
        return self.store.get_standard_diagnosis(diagnosis_id, context.scope_fingerprint)

    def list_standard_diagnoses(
        self,
        context: ScopeContext,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.store.list_standard_diagnoses(context.scope_fingerprint, limit=limit)

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

    def _execute_health_report(
        self,
        job_id: str,
        context: ScopeContext,
        order_no: str,
    ) -> None:
        if not self.store.update_health_job(job_id, status="running"):
            return
        started = time.monotonic()
        try:
            scope = resolve_query_scope(context)
            with scoped_live_sources(self.diagnostic_settings, scope=scope) as sources:
                report = build_minimal_health_report(
                    sources,
                    order_no,
                    self.diagnostic_settings.safety,
                )
                order = sources.get_orders(order_no)[0]
                device = str(order.get("child_device_code") or order.get("device_code"))
                try:
                    samples = sources.get_gun_samples(
                        device,
                        _as_datetime(order["created_time"]),
                        _as_datetime(order["stop_time"]),
                        None,
                    )
                except SourceError:
                    samples = []
                    report["source_summary"]["telemetry"] = "unavailable"
                else:
                    report["source_summary"]["telemetry"] = "available" if samples else "unavailable"
                report["curves"] = build_curves(samples)
            if time.monotonic() - started > 30:
                self.store.update_health_job(
                    job_id,
                    status="failed",
                    error_code="REPORT_TIMEOUT",
                    error_message="health report timed out",
                )
                return
            self.store.update_health_job(job_id, status="completed", report=report)
        except HealthReportError as exc:
            self.store.update_health_job(
                job_id,
                status="failed",
                error_code=exc.code,
                error_message=str(exc),
            )
        except (SourceError, ValueError) as exc:
            self.store.update_health_job(
                job_id,
                status="failed",
                error_code="SOURCE_UNAVAILABLE",
                error_message=f"{exc.__class__.__name__}: {exc}",
            )

    def _execute_standard_diagnosis(
        self,
        diagnosis_id: str,
        workspace: AgentWorkspace,
        request,
        context: ScopeContext,
        provider: str,
        key_slot: str,
    ) -> None:
        if not self.store.update_standard_diagnosis(diagnosis_id, status="running"):
            return
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        settings.agent.run_root = self.diagnostic_settings.agent.run_root
        try:
            query_scope = resolve_query_scope(context)
            result = run_agent_diagnosis(
                workspace,
                request,
                settings,
                None,
                allowed_tenants={context.effective_tenant_id},
                provider=provider,
                key_slot=key_slot,
                scope=query_scope,
            )
        except (AgentRuntimeError, SourceError, ValueError) as exc:
            self.store.update_standard_diagnosis(
                diagnosis_id,
                status="failed",
                error_code="DIAGNOSIS_FAILED",
                error_message=_public_error_message(exc, request.order_no),
            )
            return
        public_status = "completed" if result.status.value == "diagnosed" else "inconclusive"
        self.store.update_standard_diagnosis(
            diagnosis_id,
            status=public_status,
            result=result.model_dump(mode="json"),
        )

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
