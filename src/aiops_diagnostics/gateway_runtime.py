from __future__ import annotations

import contextlib
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_contracts import IncidentManifest
from aiops_diagnostics.agent_runner import classify_lightweight, run_agent_diagnosis, run_zero_order_answer
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.answer_language import (
    answer_chinese_leak,
    record_answer_language_fallback,
)
from aiops_diagnostics.codex_runtime import AgentContractError, AgentRuntimeError
from aiops_diagnostics.config import Settings, canonical_provider_base_url, validate_key_slot_name
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayDevice, GatewayStore
from aiops_diagnostics.health_curves import build_curves
from aiops_diagnostics.health_metrics import enrich_report
from aiops_diagnostics.health_report import (
    HEALTH_RULE_VERSION,
    HealthReportError,
    build_minimal_health_report,
)
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, QA_FALLBACK_MESSAGES
from aiops_diagnostics.journal import EvidenceJournal
from aiops_diagnostics.knowledge_retrieval import (
    KbServiceClient,
    KnowledgeSearchClient,
    KnowledgeSearchUnavailable,
    MediaGrant,
    MediaProxy,
    MediaResourceSigner,
    MediaResponse,
)
from aiops_diagnostics.order_visibility import resolve_device_tenant
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
        *,
        kb_search_client: KnowledgeSearchClient | None = None,
        media_signer: MediaResourceSigner | None = None,
        agent_store: Any = None,
    ) -> None:
        self.store = store
        self.gateway_settings = gateway_settings
        self.diagnostic_settings = diagnostic_settings
        self.diagnostic_settings.agent.run_root = str(gateway_settings.data_home / "runs")
        allowed = gateway_settings.allowed_key_slots or (diagnostic_settings.agent.key_slot,)
        self.allowed_key_slots = frozenset(allowed)
        # Customer QA RAG path (T3/#170): bounded kb-service search + signed
        # media. Both stay None unless configured, in which case the QA branch
        # keeps the plain zero-order answer (no knowledge, no media).
        self.kb_search_client = kb_search_client
        self.media_signer = media_signer
        self.agent_store = agent_store
        # Lazy media proxy (T3/#170 follow-up): built on first /v1/media hit;
        # without the kb-service + media-signing configuration it stays None
        # and the route answers a uniform 404.
        self._media_proxy: MediaProxy | None = None
        # Conversation turn backfill (T4/#172): same DB file, lazy-built.
        from aiops_diagnostics.conversation_store import ConversationStore

        self.conversation_store = ConversationStore(store.path)
        # Redacted run metrics (T7/#174): same DB file, lazy-built. Writes are
        # best-effort — a metrics failure must never fail the run itself.
        from aiops_diagnostics.metrics_store import MetricsStore

        self.metrics_store = MetricsStore(store.path)
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
        from aiops_diagnostics.agent_lifecycle import AgentStore

        diagnostic_settings = Settings.from_config(gateway_settings.server_config_file)
        diagnostic_settings.agent.validate()
        diagnostic_settings.ssh.validate()
        kb_client = None
        if gateway_settings.kb_service_base_url:
            kb_client = KbServiceClient(
                gateway_settings.kb_service_base_url,
                tenant_id="aiops",  # per-request tenant is applied by the QA worker
                timeout=gateway_settings.kb_service_timeout_seconds,
            )
        media_signer = None
        if gateway_settings.media_signing_secret:
            media_signer = MediaResourceSigner(
                gateway_settings.media_signing_secret,
                ttl_seconds=gateway_settings.media_ttl_seconds,
            )
        return cls(
            store,
            gateway_settings,
            diagnostic_settings,
            kb_search_client=kb_client,
            media_signer=media_signer,
            agent_store=AgentStore(store.path),
        )

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
        # The entry rule is the shared definition (#331): it refuses a request
        # that names a tenant outside the enrolled device scope and returns the
        # effective tenant already normalized, so the allowed set below and the
        # run's own tenant (stripped by parse_request) cannot disagree.
        effective_tenant = resolve_device_tenant(device.tenant_id, tenant_id)
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
        language: str = DEFAULT_LANGUAGE,
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
            language=language,
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
            language,
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

    def _record_metric(self, **fields: Any) -> None:
        """Best-effort redacted metric row (T7/#174); never fails the run."""
        with contextlib.suppress(Exception):
            self.metrics_store.record(**fields)

    def record_route_metric(
        self,
        context: ScopeContext,
        route_type: str,
        outcome: str,
        *,
        agent_id: str | None = None,
        agent_version_key: str | None = None,
        conversation_id: str | None = None,
        retrieval_status: str = "",
        searches: int = 0,
        media_count: int = 0,
        duration_ms: int | None = None,
        token_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        """Record one redacted interaction row scoped to the caller's tenant.

        The tenant always comes from the authenticated scope — callers never
        submit it. An empty tenant (unauthenticated edge) records nothing.
        """
        tenant_id = getattr(context, "effective_tenant_id", "") or ""
        if not tenant_id:
            return
        self._record_metric(
            tenant_id=tenant_id,
            route_type=route_type,
            outcome=outcome,
            agent_id=agent_id,
            agent_version_key=agent_version_key,
            conversation_id=conversation_id,
            retrieval_status=retrieval_status,
            searches=searches,
            media_count=media_count,
            duration_ms=duration_ms,
            token_count=token_count,
            error_code=error_code,
        )

    def get_agent_metrics_summary(
        self,
        context: ScopeContext,
        *,
        agent_id: str | None = None,
        window_hours: int = 720,
    ) -> dict[str, Any]:
        """Tenant-scoped aggregate for the monitoring API (VIEW_ROLES gate
        happens at the API layer; the tenant is taken from the scope)."""
        from aiops_diagnostics.agent_lifecycle import VIEW_ROLES

        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_roles.intersection(VIEW_ROLES):
            raise AgentRuntimeError("agent access is not permitted")
        return self.metrics_store.summary(
            getattr(context, "effective_tenant_id", "") or "",
            agent_id=agent_id,
            limit_hours=window_hours,
        )

    def list_agent_metrics_runs(
        self,
        context: ScopeContext,
        *,
        agent_id: str | None = None,
        route_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from aiops_diagnostics.agent_lifecycle import VIEW_ROLES

        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_roles.intersection(VIEW_ROLES):
            raise AgentRuntimeError("agent access is not permitted")
        return self.metrics_store.list_runs(
            getattr(context, "effective_tenant_id", "") or "",
            agent_id=agent_id,
            route_type=route_type,
            limit=limit,
        )

    def start_assistant_qa(
        self,
        context: ScopeContext,
        question: str,
        *,
        conversation: dict[str, Any] | None = None,
        conversation_turn_no: int | None = None,
        language: str = DEFAULT_LANGUAGE,
        promo_target: str | None = None,
        promo_intent: str | None = None,
    ) -> dict[str, Any]:
        """Start a zero-order general-question job (T3/#153).

        With ``conversation`` + ``conversation_turn_no`` (T4/#172) the finished
        answer is written back into the conversation's turn row; failures drop
        the turn so an interrupted generation never survives as a reply.
        ``promo_target`` + ``promo_intent`` (#231) pin a published promotional
        agent version (``agt_xxx#vN``) and the card kind (case/solution) its
        knowledge bases serve instead of the customer-service agent.
        """
        selected_provider = self.diagnostic_settings.agent.select_provider(None)
        selected_key_slot = validate_key_slot_name(selected_provider.resolved_key_slot())
        if selected_key_slot not in self.allowed_key_slots:
            raise ValueError("default key slot is not allowed by the gateway")
        workspace = AgentWorkspace.create_qa(
            reference_root(),
            Path(self.diagnostic_settings.agent.run_root),
            provider_base_url=canonical_provider_base_url(selected_provider.base_url),
            provider=selected_provider.name,
            key_slot=selected_key_slot,
        )
        qa = self.store.create_assistant_question(
            context.scope_fingerprint,
            question,
            internal_run_id=workspace.run_id,
        )
        future = self._executor.submit(
            self._execute_assistant_qa,
            qa["qa_id"],
            workspace,
            question,
            selected_provider.name,
            selected_key_slot,
            context.effective_tenant_id or None,
            (
                conversation["conversation_id"],
                conversation["scope_fingerprint"],
                conversation_turn_no,
            )
            if conversation is not None and conversation_turn_no is not None
            else None,
            language,
            promo_target,
            promo_intent,
        )
        self._futures[qa["qa_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(qa["qa_id"], None))
        return qa

    def classify_lightweight(self, question: str, *, language: str = DEFAULT_LANGUAGE) -> dict[str, Any]:
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        settings.agent.run_root = self.diagnostic_settings.agent.run_root
        provider = settings.agent.select_provider(None)
        return classify_lightweight(question, settings, provider=provider.name, language=language)

    def get_assistant_qa(
        self,
        context: ScopeContext,
        qa_id: str,
    ) -> dict[str, Any] | None:
        return self.store.get_assistant_question(qa_id, context.scope_fingerprint)

    def list_assistant_qa(
        self,
        context: ScopeContext,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.store.list_assistant_questions(context.scope_fingerprint, limit=limit)

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
                report = enrich_report(report, samples, order)
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
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        if not self.store.update_standard_diagnosis(diagnosis_id, status="running"):
            return
        started_ms = time.monotonic()
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
                language=language,
            )
        except (AgentRuntimeError, SourceError, ValueError) as exc:
            self.store.update_standard_diagnosis(
                diagnosis_id,
                status="failed",
                error_code="DIAGNOSIS_FAILED",
                error_message=_public_error_message(exc, request.order_no),
            )
            self._record_metric(
                tenant_id=context.effective_tenant_id,
                route_type="diagnosis",
                outcome="failed",
                error_code="DIAGNOSIS_FAILED",
                duration_ms=int((time.monotonic() - started_ms) * 1000),
            )
            return
        # A blocked run means the model could not produce the structured
        # diagnostic output (e.g. the provider does not honor strict
        # output_schema), or the run was blocked for a hard reason — that is a
        # failure of the diagnosis task, not "insufficient evidence". Surface
        # it as failed with an explicit error code so the frontend can
        # distinguish provider/task failure from a genuine inconclusive result.
        if result.status.value == "blocked":
            self.store.update_standard_diagnosis(
                diagnosis_id,
                status="failed",
                error_code="DIAGNOSIS_BLOCKED",
                error_message="diagnosis could not complete",
            )
            self._record_metric(
                tenant_id=context.effective_tenant_id,
                route_type="diagnosis",
                outcome="failed",
                error_code="DIAGNOSIS_BLOCKED",
                duration_ms=int((time.monotonic() - started_ms) * 1000),
            )
            return
        public_status = "completed" if result.status.value == "diagnosed" else "inconclusive"
        self.store.update_standard_diagnosis(
            diagnosis_id,
            status=public_status,
            result=result.model_dump(mode="json"),
        )
        dumped = result.model_dump(mode="json")
        # AgentDiagnosis carries summary/root_cause/hypotheses, not blocks[] —
        # estimate tokens from the narrative fields it actually has.
        diagnosis_tokens = (
            sum(len(str(dumped.get(field) or "")) for field in ("summary", "root_cause")) * 2 // 3 + 1
        )
        self._record_metric(
            tenant_id=context.effective_tenant_id,
            route_type="diagnosis",
            outcome="completed" if public_status == "completed" else "failed",
            error_code=None if public_status == "completed" else "DIAGNOSIS_INCONCLUSIVE",
            duration_ms=int((time.monotonic() - started_ms) * 1000),
            token_count=diagnosis_tokens,
        )

    def _execute_assistant_qa(
        self,
        qa_id: str,
        workspace: AgentWorkspace,
        question: str,
        provider: str,
        key_slot: str,
        tenant_id: str | None = None,
        conversation_turn: tuple[str, str, int] | None = None,
        language: str = DEFAULT_LANGUAGE,
        promo_target: str | None = None,
        promo_intent: str | None = None,
    ) -> None:
        def _finish_turn(answer: dict[str, Any] | None, *, cancelled: bool = False) -> None:
            """Write the finished answer into the conversation turn (if any).

            Cancelled/failed turns drop their row: an interrupted generation
            never survives as a complete reply (#172).
            """
            if conversation_turn is None:
                return
            from aiops_diagnostics.conversation_store import ConversationError

            conversation_id, scope_fingerprint, turn_no = conversation_turn
            with contextlib.suppress(ConversationError):
                self.conversation_store.complete_turn(
                    conversation_id,
                    scope_fingerprint,
                    turn_no,
                    answer=answer,
                    token_count=_estimate_turn_tokens(question, answer),
                    cancelled=cancelled or answer is None,
                )

        if not self.store.update_assistant_question(qa_id, status="running"):
            _finish_turn(None, cancelled=True)
            return
        started_ms = time.monotonic()
        # #232 route metric: promotional card runs get their own bucket.
        route_tag = "promo" if promo_intent else "qa"
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        settings.agent.run_root = self.diagnostic_settings.agent.run_root
        rag_result = None
        if promo_intent and (not tenant_id or self.kb_search_client is None or self.media_signer is None):
            from aiops_diagnostics.promo_agents import promo_empty_result

            # No search capability is wired here, so nothing was searched. Same
            # rule as the other card sites: do not claim the library is empty.
            rag_result = promo_empty_result(language, promo_intent, retrieval_status="unavailable")
            self.store.update_assistant_question(qa_id, status="completed", result=rag_result)
        elif tenant_id and self.kb_search_client is not None and self.media_signer is not None:
            rag_result = self._try_customer_rag(
                qa_id,
                question,
                tenant_id,
                settings,
                provider,
                key_slot,
                language,
                promo_target=promo_target,
                promo_intent=promo_intent,
            )
        if rag_result is not None:
            if isinstance(rag_result, dict) and rag_result.get("status") == "failed":
                self._record_metric(
                    tenant_id=tenant_id,
                    route_type=route_tag,
                    outcome="failed",
                    error_code="QA_FAILED",
                    duration_ms=int((time.monotonic() - started_ms) * 1000),
                )
                _finish_turn(None, cancelled=True)
            else:
                self._record_qa_metric(
                    rag_result, tenant_id, started_ms, conversation_turn, route_type=route_tag
                )
                # The metrics-only tag must not persist into the conversation
                # turn (it would surface through GET /v1/conversations/{id}).
                _finish_turn({key: value for key, value in rag_result.items() if key != "agent_version"})
            return
        try:
            answer = run_zero_order_answer(
                question,
                settings,
                provider=provider,
                key_slot=key_slot,
                language=language,
            )
        except (AgentRuntimeError, SourceError, ValueError) as exc:
            self.store.update_assistant_question(
                qa_id,
                status="failed",
                error_code="QA_FAILED",
                error_message=_public_error_message(exc, ""),
            )
            if tenant_id:
                self._record_metric(
                    tenant_id=tenant_id,
                    route_type=route_tag,
                    outcome="failed",
                    error_code="QA_FAILED",
                    duration_ms=int((time.monotonic() - started_ms) * 1000),
                )
            _finish_turn(None, cancelled=True)
            return
        answer = _guard_zero_order_language(answer, language)
        self.store.update_assistant_question(
            qa_id,
            status="completed",
            result=answer,
        )
        if tenant_id:
            self._record_metric(
                tenant_id=tenant_id,
                route_type=route_tag,
                outcome="completed",
                duration_ms=int((time.monotonic() - started_ms) * 1000),
                token_count=_estimate_turn_tokens(question, answer),
            )
        _finish_turn(answer)

    def _record_qa_metric(
        self,
        result: dict[str, Any] | None,
        tenant_id: str | None,
        started_ms: float,
        conversation_turn: tuple[str, str, int] | None,
        *,
        route_type: str = "qa",
    ) -> None:
        """Record a completed RAG QA run from its result payload.

        Only redacted aggregates are read from the result: retrieval status,
        block-kind counts, and the runtime-tagged agent_version. The agent
        id/version/conversation come from the run's own identifiers, never
        from model text.
        """
        if not tenant_id:
            return
        blocks = (result or {}).get("blocks") or []
        media_count = sum(1 for block in blocks if block.get("kind") in ("image", "video"))
        conversation_id = conversation_turn[0] if conversation_turn is not None else None
        agent_version_key = str((result or {}).get("agent_version") or "") or None
        agent_id = agent_version_key.split("#", 1)[0] if agent_version_key else None
        searches = (result or {}).get("searches") or 0
        self._record_metric(
            tenant_id=tenant_id,
            route_type=route_type,
            outcome="completed",
            agent_id=agent_id,
            agent_version_key=agent_version_key,
            conversation_id=conversation_id,
            retrieval_status=str((result or {}).get("retrieval_status") or ""),
            searches=int(searches),
            media_count=media_count,
            duration_ms=int((time.monotonic() - started_ms) * 1000),
            token_count=_estimate_turn_tokens("", result),
        )

    def run_agent_debug(
        self,
        context: Any,
        agent_id: str,
        question: str,
        *,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        """Isolated draft preview turn (T5/#171).

        Runs the draft config through the production customer-QA harness with
        the per-tenant kb client; never creates an assistant job, a
        conversation turn, or touches order data. Raises ``AgentRuntimeError``
        subclass failures for the API layer to map.
        """
        from aiops_diagnostics.agent_debug import run_agent_debug_answer
        from aiops_diagnostics.agent_lifecycle import AgentNotFound

        assert isinstance(self.kb_search_client, KbServiceClient)
        assert self.media_signer is not None
        agent = self.agent_store.get(agent_id, context.effective_tenant_id)  # type: ignore[union-attr]
        if agent.status != "draft":
            raise ValueError("only a draft agent can be debug-run")
        tenant_id = context.effective_tenant_id
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        settings.agent.run_root = self.diagnostic_settings.agent.run_root
        selected_provider = settings.agent.select_provider(None)
        key_slot = validate_key_slot_name(selected_provider.resolved_key_slot())
        started_ms = time.monotonic()
        try:
            result = run_agent_debug_answer(
                question,
                agent_id=agent.agent_id,
                revision=agent.revision,
                prompt=agent.config.prompt,
                knowledge_base_ids=agent.config.knowledge_base_ids,
                agent_settings=settings.agent,
                search_client=self.kb_search_client.for_tenant(tenant_id),
                media_signer=self.media_signer,
                tenant_id=tenant_id,
                provider=selected_provider,
                key_slot=key_slot,
                project_root=reference_root(),
                language=language,
            )
        except KnowledgeSearchUnavailable as exc:
            self._record_metric(
                tenant_id=tenant_id,
                route_type="debug",
                outcome="failed",
                agent_id=agent.agent_id,
                error_code="KB_UNAVAILABLE",
                duration_ms=int((time.monotonic() - started_ms) * 1000),
            )
            raise AgentRuntimeError("kb-service is unavailable for the debug run") from exc
        except AgentNotFound as exc:
            raise AgentRuntimeError("draft agent binding is invalid") from exc
        except AgentRuntimeError:
            self._record_metric(
                tenant_id=tenant_id,
                route_type="debug",
                outcome="failed",
                agent_id=agent.agent_id,
                error_code="AGENT_DEBUG_FAILED",
                duration_ms=int((time.monotonic() - started_ms) * 1000),
            )
            raise
        blocks = result.get("blocks") or []
        self._record_metric(
            tenant_id=tenant_id,
            route_type="debug",
            outcome="completed",
            agent_id=agent.agent_id,
            agent_version_key=str(result.get("agent_version") or ""),
            retrieval_status=str(result.get("retrieval_status") or ""),
            media_count=sum(1 for block in blocks if block.get("kind") in ("image", "video")),
            duration_ms=int((time.monotonic() - started_ms) * 1000),
            token_count=_estimate_turn_tokens("", result),
        )
        return result

    def _try_customer_rag(
        self,
        qa_id: str,
        question: str,
        tenant_id: str,
        settings: Settings,
        provider: str,
        key_slot: str,
        language: str = DEFAULT_LANGUAGE,
        *,
        promo_target: str | None = None,
        promo_intent: str | None = None,
    ) -> dict[str, Any] | None:
        """Run the published customer agent path (T3/#170) or fall back.

        Returns the completed job record when the RAG path produced a result;
        None when there is no published customer agent for this tenant or the
        kb dependency is unavailable, letting the caller keep the existing
        zero-order behavior. With ``promo_intent`` (#231) the pinned
        promotional agent serves instead of the customer-service agent; an
        unresolvable target or empty library returns the honest empty card
        and never falls through to FAQ/customer QA.
        """
        from aiops_diagnostics.qa_rag import run_customer_qa_answer, select_customer_agent

        selection = None
        promo_prompt_text: str | None = None
        if promo_intent:
            from aiops_diagnostics.promo_agents import promo_empty_result, promo_prompt, select_promo_agent

            promo = (
                select_promo_agent(self.agent_store, tenant_id, promo_target)
                if self.agent_store is not None
                else None
            )
            if promo is None:
                # No pin, or the pin does not resolve. Nothing was searched, so
                # the card must not claim the library holds no match: on 41 this
                # is exactly what produced "未检索到匹配的宣传资料" for
                # solution_discovery, which has no target at all and therefore
                # never ran a query. "not_found" is reserved for a search that
                # actually returned nothing.
                result = promo_empty_result(language, promo_intent, retrieval_status="unavailable")
                self.store.update_assistant_question(qa_id, status="completed", result=result)
                return result
            selection = promo
            promo_prompt_text = promo_prompt(promo, question, language=language, intent=promo_intent)
        if selection is None:
            if self.agent_store is None:
                return None
            try:
                selection = select_customer_agent(self.agent_store, tenant_id)
            except Exception:
                selection = None
        if selection is None:
            return None
        assert isinstance(self.kb_search_client, KbServiceClient)
        try:
            result = run_customer_qa_answer(
                question,
                selection,
                settings.agent,
                search_client=self.kb_search_client.for_tenant(tenant_id),
                media_signer=self.media_signer,
                tenant_id=tenant_id,
                provider=None,
                key_slot=key_slot,
                project_root=reference_root(),
                language=language,
                initial_prompt=promo_prompt_text,
            )
        except KnowledgeSearchUnavailable:
            if promo_intent:
                from aiops_diagnostics.promo_agents import promo_empty_result

                # The search failed, so nothing is known about the library's
                # contents. Reporting not_found here would tell the user (and
                # the next debugger) that the library holds no match — a claim
                # nobody made. Verified on 41: kb-service returns 502
                # code=102 while the provider account is in arrears, and the
                # old code turned that outage into "未检索到匹配的宣传资料".
                result = promo_empty_result(language, promo_intent, retrieval_status="unavailable")
                self.store.update_assistant_question(qa_id, status="completed", result=result)
                return result
            # Retrieval dependency is down: per the T1/T3 contract the model
            # can still answer from its own knowledge with
            # retrieval_status=unavailable — but the runtime cannot reach the
            # model without the search guard either, so degrade to the plain
            # zero-order answer instead of failing the job.
            return None
        except AgentContractError as exc:
            # The model answered but violated the blocks contract. That is a
            # defect, not an outage: report it as a failure so it stays visible
            # in the job record, and do NOT let the promo degrade hide it.
            # (2026-09-17: treating this like a provider outage made a real
            # reference-block bug look like "service temporarily unavailable".)
            self.store.update_assistant_question(
                qa_id,
                status="failed",
                error_code="QA_FAILED",
                error_message=_public_error_message(exc, ""),
            )
            return {"status": "failed"}
        except (AgentRuntimeError, ValueError) as exc:
            if promo_intent:
                # A promotional click is a product surface, not a diagnostic
                # run. When the model is genuinely unreachable (41 live: the
                # provider account in arrears made every case_exploration click
                # a hard QA_FAILED), the promotion contract promises an honest
                # card, so report the outage as a card instead of an error the
                # user cannot act on.
                from aiops_diagnostics.promo_agents import promo_empty_result

                self.store.update_assistant_question(
                    qa_id,
                    status="completed",
                    result=promo_empty_result(language, promo_intent, retrieval_status="unavailable"),
                )
                return {"status": "completed"}
            self.store.update_assistant_question(
                qa_id,
                status="failed",
                error_code="QA_FAILED",
                error_message=_public_error_message(exc, ""),
            )
            return {"status": "failed"}
        # Metrics-only attribution tag: the job result and the conversation
        # turn payload stay on the unchanged blocks contract (no agent_version
        # key — it must not leak into stored/public payloads).
        result = {
            **result,
            "agent_version": f"{selection.agent_id}#v{selection.version_no}",
        }
        self.store.update_assistant_question(
            qa_id,
            status="completed",
            result={key: value for key, value in result.items() if key != "agent_version"},
        )
        return result

    def serve_media(
        self,
        signed_id: str,
        *,
        range_header: str | None = None,
    ) -> MediaResponse | None:
        """Serve a signed media URL (#168 media protocol, /v1/media route).

        Returns None when the media plane is not configured (no kb-service or
        no signing secret) so the API layer can answer a uniform 404. The
        URL's HMAC authenticates the request (browsers cannot attach Bearer
        headers to <img>/<video>); the grant's own tenant scope plus the
        ``is_active`` liveness check below re-verify that the tenant's
        customer agent is still published at the same version and still
        bound to the knowledge base the grant cites — so a disabled segment,
        an unpublished agent, or an unbound KB invalidates URLs immediately.
        """
        if self.media_signer is None or self.kb_search_client is None:
            return None

        def _fetch_for_tenant(grant: MediaGrant, *, range_header: str | None = None) -> bytes:
            # The process-level client is bound to the neutral "aiops" tenant;
            # the media document belongs to the grant's tenant, so kb-service
            # must see that tenant's header/token or the download misses
            # (code=102 document not found, observed as deterministic 404s in
            # the #175 C4 video canary on 2026-09-12).
            client = self.kb_search_client
            bound = client.for_tenant(grant.tenant_id) if hasattr(client, "for_tenant") else client
            if range_header is None:
                return bound.fetch_media(grant)
            return bound.fetch_media(grant, range_header=range_header)

        if self._media_proxy is None:
            self._media_proxy = MediaProxy(self.media_signer, _fetch_for_tenant)

        def _is_active(grant: MediaGrant) -> bool:
            """Is the agent version that SIGNED this grant still live?

            Validate the grant against its own agent, not the customer-service
            agent. Asserting `select_customer_agent(...) == grant.agent_version`
            could never hold for a promotional grant: that selector deliberately
            SKIPS promo-pinned agents (#231), so every image and video URL a
            promotional card produced was rejected with 403 — media was
            retrievable but never displayable (41 live, 2026-09-18).

            The grant names its agent, so the honest check is: that agent is
            still published at exactly that version, and still bound to the
            knowledge base the grant cites. A disabled agent version or an
            unbound KB still invalidates URLs immediately.
            """
            if self.agent_store is None:
                return False
            agent_id, _, version_part = str(grant.agent_version).partition("#v")
            if not agent_id or not version_part.isdigit():
                return False
            try:
                published = self.agent_store.get(agent_id, grant.tenant_id)
                version = self.agent_store.version(agent_id, grant.tenant_id, int(version_part))
            except Exception:
                return False
            if published.status != "published" or published.published_version != int(version_part):
                return False
            snapshot_kbs = tuple(version.snapshot.get("knowledge_base_ids") or ())
            return grant.knowledge_base_id in snapshot_kbs

        try:
            return self._media_proxy.serve_signed(signed_id, range_header=range_header, is_active=_is_active)
        except KnowledgeSearchUnavailable:
            return MediaResponse(503, {"Cache-Control": "no-store"}, b"")

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


def _guard_zero_order_language(answer: dict[str, Any], language: str) -> dict[str, Any]:
    """Withhold a zero-order answer that leaked Chinese (#293).

    This surface is reachable, not theoretical: when the customer-RAG path
    degrades on an unavailable knowledge base it deliberately falls through to
    the zero-order answer, so the same user who asked for English gets this
    text instead. Its prompt is also authored in Chinese, so the model is
    copying from Chinese instructions — the same shape as the card headings.

    Returns a localized fallback in place of the leaking text. The payload
    shape is unchanged: the caller stores and returns exactly the keys the
    contract defines.
    """
    if not isinstance(answer, dict):
        return answer
    text = answer.get("text")
    if not isinstance(text, str):
        return answer
    leak = answer_chinese_leak(text, language)
    if not leak:
        return answer
    record_answer_language_fallback(language=language, leaked=leak, surface="zero_order")
    pack = QA_FALLBACK_MESSAGES.get(language) or QA_FALLBACK_MESSAGES[DEFAULT_LANGUAGE]
    return {**answer, "text": pack["unavailable"]}


def _estimate_turn_tokens(question: str, answer: dict[str, Any] | None) -> int:
    """Rough token estimate for the conversation context budget (#172).

    CJK text runs ~1.5 chars/token; this approximation only decides how many
    history turns fit the 8k window — never a billing number.
    """
    text = question or ""
    if answer:
        for block in answer.get("blocks") or []:
            text += str(block.get("text") or "")
        text += str(answer.get("text") or "")
    return max(1, len(text) * 2 // 3)


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
