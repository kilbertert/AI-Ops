from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
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
from aiops_diagnostics.conversation_store import BUSY_LOCK_SECONDS
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import ACTIVE_DIAGNOSIS_STATUSES, GatewayDevice, GatewayStore
from aiops_diagnostics.health_curves import build_curves
from aiops_diagnostics.health_metrics import enrich_report
from aiops_diagnostics.health_report import (
    HEALTH_RULE_VERSION,
    HealthReportError,
    build_minimal_health_report,
)
from aiops_diagnostics.i18n import (
    DEFAULT_LANGUAGE,
    QA_FALLBACK_MESSAGES,
    effective_language,
)
from aiops_diagnostics.jev_decisions import JevDecisionClient, JevSettings
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
from aiops_diagnostics.order_visibility import TENANT_SCOPE_SOURCE, resolve_device_tenant
from aiops_diagnostics.parsing import parse_request
from aiops_diagnostics.platform_paths import reference_root
from aiops_diagnostics.query_scope import resolve_query_scope
from aiops_diagnostics.redaction import redact_text
from aiops_diagnostics.routing import RoutingThresholds, classify_with_jev
from aiops_diagnostics.scope_context import ScopeContext
from aiops_diagnostics.sources import SourceError, scoped_live_sources

FIXTURE_NAMES = frozenset({"ocpp_consistent.json", "ykc_amount_mismatch.json", "missing_tx_data.json"})

_LOGGER = logging.getLogger("aiops.gateway_runtime")

#: A blocked run whose order turned out to be outside the caller's tenant. It is
#: NOT ``DIAGNOSIS_BLOCKED``: that code means the supplier did not honor
#: ``output_schema``, and an operator must be able to tell the two apart. On the
#: standard API face this is defense in depth rather than an expected terminal
#: state — see ``_blocked_diagnosis_error`` for why, and both contract docs for
#: what a frontend may therefore rely on.
DIAGNOSIS_ORDER_OUT_OF_SCOPE = "DIAGNOSIS_ORDER_OUT_OF_SCOPE"

#: What ``_try_customer_rag`` hands back when the claim-guard refused the job's
#: terminal write: another path (expiry today; cancellation once #357 lands)
#: already put the row in a terminal state, so this worker's result was NOT
#: persisted. The caller stops right there — no metric, and the in-flight turn
#: is dropped rather than filled — because each of those follow-ups asserts
#: "the write landed".
#:
#: A value rather than an exception on purpose: losing the race is a legal
#: outcome, not a failure, and the worker must exit quietly rather than raise.
TERMINAL_WRITE_REFUSED = {"status": "refused"}


@dataclass
class JobRegistration:
    """What a stop request needs in order to interrupt one running job.

    Covers both interruptible lines — an assistant question (#357) and an
    order diagnosis (#499). The diagnosis line used to have no registration at
    all, which is why its stop button could only free the conversation slot and
    never reach the model call; the fields were already line-agnostic, so the
    fix is the plumbing, not a second class.

    A ``Future`` is deliberately NOT what this holds: ``Future.cancel()``
    cannot stop a thread that is already running the model turn, which is
    exactly the case a stop request arrives in. What does stop it is the turn's
    own ``interrupt()`` RPC (the one the turn timeout already uses), filled in
    by the worker the moment its turn starts.

    ``conversation_turn`` is the generation slot this job owns. Cancelling is
    the one path that must free it without waiting for the worker to notice,
    so the stop request carries the slot's identity with it.

    ``route_type`` is the route this job was submitted with (a promotional
    shortcut run, a plain question, or an order diagnosis), which is what the
    stop records: the worker uses the same value for its own metric rows, so a
    stopped promotional click is not counted as a stopped customer question.
    """

    conversation_turn: tuple[str, str, int] | None = None
    interrupt: Callable[[], None] | None = None
    route_type: str = "qa"
    #: Set by a stop request (#499 review). Needed because the worker may reach
    #: its first turn *after* the stop already ran: the stop pops the
    #: registration out of the registry and finds no handle to interrupt, yet
    #: the worker still holds this object and will register into it. Without
    #: this flag the turn would run to completion on a job already reported as
    #: ``cancelled``. Reachable whenever a user stops a job that is still
    #: spinning up its workspace, which is the ordinary case.
    cancelled: bool = False

    def register_interrupt(self, handle: Any) -> None:
        """Adopt the live turn handle's interrupt as this job's interrupt.

        A new turn replaces the previous one (the RAG path runs several), so the
        registration always points at the turn that is running NOW.

        If the job was already stopped, interrupt the handle immediately rather
        than adopting it: the stop request has already returned and nobody will
        come back to look at ``self.interrupt``.
        """
        if self.cancelled:
            handle.interrupt()
            return
        self.interrupt = handle.interrupt


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
        jev_client: JevDecisionClient | None = None,
        routing_thresholds: RoutingThresholds | None = None,
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
        # Routing decisions from typed answers (#392). Absent configuration
        # leaves this None, and every question simply routes as it did before —
        # the dependency is optional by design, not by accident.
        self.jev_client = jev_client
        self.routing_thresholds = routing_thresholds or RoutingThresholds()
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
        # Interruptible assistant questions (#357): what a stop request needs in
        # order to reach the model turn that is burning tokens. Registered when
        # the job is submitted, dropped when it leaves the non-terminal state.
        self._qa_registrations: dict[str, JobRegistration] = {}
        # #499: the diagnosis line gets its own registry — same shape, keyed by
        # diagnosis_id. Kept separate rather than merged into one dict because
        # the two id spaces are distinct (`qa_...` vs `dx_...`) and a shared
        # dict would only make a lookup bug harder to see.
        self._diagnosis_registrations: dict[str, JobRegistration] = {}

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
        # Routing from typed answers (#392): built only when configured, so an
        # unconfigured deployment keeps the previous behaviour exactly.
        jev_client = None
        if gateway_settings.jev_base_url and gateway_settings.jev_api_key:
            jev_client = JevDecisionClient(
                JevSettings(
                    base_url=gateway_settings.jev_base_url,
                    api_key=gateway_settings.jev_api_key,
                    model=gateway_settings.jev_model,
                    timeout=gateway_settings.jev_timeout_seconds,
                )
            )
        return cls(
            store,
            gateway_settings,
            diagnostic_settings,
            kb_search_client=kb_client,
            media_signer=media_signer,
            agent_store=AgentStore(store.path),
            jev_client=jev_client,
            routing_thresholds=RoutingThresholds(
                risk_at_least=gateway_settings.routing_risk_at_least,
                confidence_at_least=gateway_settings.routing_confidence_at_least,
                risk_always_asks=gateway_settings.routing_risk_always_asks,
            ),
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
            language=DEFAULT_LANGUAGE,
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

    def start_health_report(
        self,
        context: ScopeContext,
        order_no: str,
        *,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        job, created = self.store.create_or_reuse_health_job(
            context.scope_fingerprint,
            order_no,
            HEALTH_RULE_VERSION,
            language=language,
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
        *,
        conversation_turn: tuple[str, str, int] | None = None,
    ) -> dict[str, Any]:
        """Start an order diagnosis job.

        ``language`` is normalised to one this pipeline can actually answer in:
        a tag that cannot be PROMPTED (Thai, Khmer — no word boundaries, so the
        matchers and rule layer cannot back a claim to answer in it) falls back
        to the default here, at the point the job is created. Doing it here
        rather than at render time matters: this value is what gets STORED, and
        the stored language is what the response reports for the result's prose
        (#549). Storing `th` while generating Chinese would make the row — and
        every later poll of it — describe text it does not contain.

        With ``conversation_turn`` (T4/#172) the finished diagnosis is written
        back into the conversation's turn row and the generation slot is held
        until the job reaches a terminal state — the same shape the qa line
        (``start_assistant_qa``) already has. Without it nothing about the
        conversation changes.
        """
        language = effective_language(language)
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
            language=language,
        )
        diagnosis = self.store.create_standard_diagnosis(
            context.scope_fingerprint,
            order_no,
            question,
            indicator_code,
            language=language,
            internal_run_id=workspace.run_id,
        )
        # #499: registered before the worker exists, exactly like the qa line —
        # a stop arriving a millisecond after this return must already find the
        # job interruptible and must know which conversation slot to free.
        registration = JobRegistration(conversation_turn=conversation_turn, route_type="diagnosis")
        self._diagnosis_registrations[diagnosis["diagnosis_id"]] = registration
        future = self._executor.submit(
            self._execute_standard_diagnosis,
            diagnosis["diagnosis_id"],
            workspace,
            request,
            context,
            selected_provider.name,
            selected_key_slot,
            language,
            conversation_turn,
            registration.register_interrupt,
        )
        self._futures[diagnosis["diagnosis_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(diagnosis["diagnosis_id"], None))
        future.add_done_callback(lambda _: self._diagnosis_registrations.pop(diagnosis["diagnosis_id"], None))
        return diagnosis

    def cancel_standard_diagnosis(
        self,
        context: ScopeContext,
        diagnosis_id: str,
    ) -> dict[str, Any] | None:
        """Stop one in-flight order diagnosis (#499).

        The same shape as ``cancel_assistant_qa``, for the same reasons — see
        that method for why the terminal row is written before the interrupt,
        and why a repeated stop answers the job's own current state instead of
        an error. What differs is only the job being stopped.

        Idempotent and non-probing: an already-terminal diagnosis is returned
        as it stands, and one outside this caller's scope is indistinguishable
        from a missing one (``None`` ⇒ 404 at the API layer).
        """
        diagnosis = self.store.get_standard_diagnosis(diagnosis_id, context.scope_fingerprint)
        if diagnosis is None:
            return None
        if diagnosis["status"] in ACTIVE_DIAGNOSIS_STATUSES:
            accepted = self.store.update_standard_diagnosis(diagnosis_id, status="cancelled")
            registration = self._diagnosis_registrations.pop(diagnosis_id, None)
            if registration is not None:
                # Same as the qa line: mark before acting, because a worker that
                # has not reached its turn yet still holds this object.
                registration.cancelled = True
                # **The interrupt is unconditional; freeing the slot is
                # conditional on having won the terminal write** (#499 review).
                # A cancel that lost the race (the worker completed between our
                # read and our write) must NOT free the slot: by then the worker
                # has filled this very turn with its answer, and releasing a
                # filled turn DELETES it — the next follow-up would silently
                # lose its context. The interrupt is still right to attempt: the
                # turn is over, so the call is a no-op, never a wrong outcome.
                #
                # The qa line has the same shape and once had this same bug; see
                # the matching comment in cancel_assistant_qa.
                self._interrupt_registered_turn(registration)
                if accepted:
                    self._release_conversation_turn(registration.conversation_turn)
            if accepted:
                self._record_cancelled_job_metric(context, registration)
            return self.store.get_standard_diagnosis(diagnosis_id, context.scope_fingerprint)
        # Already terminal: the caller sees the job's own final state, not a
        # cancellation that lost a race.
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
        skip_retrieval: bool = False,
    ) -> dict[str, Any]:
        """Start a zero-order general-question job (T3/#153).

        ``language`` is normalised to one this pipeline can prompt in, for the
        same reason the diagnosis runtime does it: Thai and Khmer can be RENDERED
        but not reliably ROUTED, so a claim to answer in them is one the rest of
        the pipeline cannot back. The declaration alone would not have stopped
        this route — the diagnosis face was its first consumer, and this is its
        second (#541 review).

        With ``conversation`` + ``conversation_turn_no`` (T4/#172) the finished
        answer is written back into the conversation's turn row; failures drop
        the turn so an interrupted generation never survives as a reply.
        ``promo_target`` + ``promo_intent`` (#231) pin a published promotional
        agent version (``agt_xxx#vN``) and the card kind (case/solution) its
        knowledge bases serve instead of the customer-service agent.

        ``skip_retrieval`` (#391) marks a question already judged as chit-chat:
        it is answered from the staged references without a knowledge search. A
        greeting must not trigger a library lookup just because the run happens
        to have search capability wired.
        """
        language = effective_language(language)
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
            language=language,
        )
        qa = self.store.create_assistant_question(
            context.scope_fingerprint,
            question,
            internal_run_id=workspace.run_id,
        )
        # Registered before the worker exists: a stop request that arrives a
        # millisecond after this return must already find the job interruptible
        # (and must know which conversation slot to free).
        registration = JobRegistration(
            conversation_turn=(
                conversation["conversation_id"],
                conversation["scope_fingerprint"],
                conversation_turn_no,
            )
            if conversation is not None and conversation_turn_no is not None
            else None,
            route_type="promo" if promo_intent else "qa",
        )
        self._qa_registrations[qa["qa_id"]] = registration
        future = self._executor.submit(
            self._execute_assistant_qa,
            qa["qa_id"],
            workspace,
            question,
            selected_provider.name,
            selected_key_slot,
            context.effective_tenant_id or None,
            registration.conversation_turn,
            language,
            promo_target,
            promo_intent,
            registration.register_interrupt,
            skip_retrieval,
        )
        self._futures[qa["qa_id"]] = future
        # Once the future is done the job is terminal (or its worker is gone),
        # so both registries forget it: nothing is left to interrupt.
        future.add_done_callback(lambda _: self._futures.pop(qa["qa_id"], None))
        future.add_done_callback(lambda _: self._qa_registrations.pop(qa["qa_id"], None))
        return qa

    def classify_lightweight(
        self,
        question: str,
        *,
        language: str = DEFAULT_LANGUAGE,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        """The routing decision, from Jev's typed answers (#392).

        ``None`` means no decision was obtained, and the caller answers without
        one — the same path it takes today when the model classifier is
        unreachable. A routing hint is an optimisation; it must never be the
        reason a user gets nothing.

        The model-based classifier remains as ``classify_lightweight_model`` and
        is what runs when Jev is not configured, so the cutover is a change of
        source rather than a removal of the capability.
        """
        if self.jev_client is not None:
            return classify_with_jev(
                question,
                self.jev_client,
                thresholds=self.routing_thresholds,
                metrics=self.metrics_store,
                tenant_id=tenant_id,
            )
        # No Jev configured: the decision still has to be made, so the previous
        # source keeps making it. Falling through to "no decision" would have
        # silently disabled casual handling, the promotional intents and the
        # high-risk clarification rule on every deployment that has not been
        # given Jev credentials — a regression dressed as a default.
        return self.classify_lightweight_model(question, language=language)

    def classify_lightweight_model(
        self,
        question: str,
        *,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        """The previous classifier: a model generating JSON.

        Kept deliberately (expand/contract): the cutover adds the new source
        beside the old one so neither has to be right on the first day. Delete
        once Jev has run on real traffic for an agreed window.
        """
        language = effective_language(language)

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

    def cancel_assistant_qa(
        self,
        context: ScopeContext,
        qa_id: str,
    ) -> dict[str, Any] | None:
        """Stop one in-flight general question (#357).

        Persist first, interrupt second. Writing the terminal row is what makes
        the cancellation real: the claim-guard is what stops the worker's late
        answer from overwriting it, and the caller's input box is unlocked by
        the job reaching a terminal state rather than by the model agreeing to
        stop. Interrupting the live turn is then only a token saving — it is
        fire-and-forget, and its failure changes nothing about the outcome.

        Idempotent by construction: a job that is already terminal is returned
        as it stands (the stop button is a network-flaky entry point, so a
        repeat click must not read as a failure), and a job outside this
        caller's scope is indistinguishable from a missing one — ``None``, which
        the API layer answers with 404.
        """
        qa = self.store.get_assistant_question(qa_id, context.scope_fingerprint)
        if qa is None:
            return None
        if qa["status"] in ACTIVE_DIAGNOSIS_STATUSES:
            accepted = self.store.update_assistant_question(qa_id, status="cancelled")
            registration = self._qa_registrations.pop(qa_id, None)
            if registration is not None:
                # Marked before anything else: a worker that has not started its
                # turn yet still holds this object and will register into it.
                registration.cancelled = True
                # Both savings run before the metric row, so nothing that
                # records rather than decides can keep the turn burning or the
                # input box locked: order is persist, save, then count.
                self._interrupt_registered_turn(registration)
                # **Freeing the slot is conditional on having won the terminal
                # write** (#499 review). ``release_turn`` DELETES the turn row,
                # so a cancel that lost the race would delete a turn the worker
                # had already filled with its answer — the caller would see the
                # job as completed and its own history silently empty. Clearing
                # the slot is only needed when the worker is still running,
                # which is exactly the case ``accepted`` is true for.
                if accepted:
                    self._release_conversation_turn(registration.conversation_turn)
            if accepted:
                # One stop, one row, and never two: the terminal write is what
                # decides the outcome, and only the request that wins it counts.
                self._record_cancelled_job_metric(context, registration)
            return self.store.get_assistant_question(qa_id, context.scope_fingerprint)
        # Already terminal: the answer (or the failure) the caller sees is the
        # job's own final state, not a cancellation that lost a race.
        return qa

    def _record_cancelled_job_metric(
        self,
        context: ScopeContext,
        registration: JobRegistration | None,
    ) -> None:
        """Record a stopped job as its own outcome (#359; both lines since #499).

        Named for the job rather than the question since #499: a stopped
        diagnosis leaves the same row, and a name that says otherwise invites
        the next reader to assume only questions are counted.

        ``cancelled`` has been a valid metric outcome since the metrics table
        was built and nothing ever wrote it, so "which questions do people give
        up on" had no answer at all: the wait ended in a job row that no
        aggregate looks at. The stop now leaves one redacted row behind it —
        tenant, route, and the conversation it belongs to, which is all a metric
        row may carry.

        Written here, where the terminal row is decided, because this is the
        only moment a stop is known to have happened: the worker's late write is
        refused by the claim-guard and records nothing (its result never
        landed), so a cancellation cannot be counted twice.
        """
        conversation_id = (
            registration.conversation_turn[0]
            if registration is not None and registration.conversation_turn is not None
            else None
        )
        self.record_route_metric(
            context,
            registration.route_type if registration is not None else "qa",
            "cancelled",
            conversation_id=conversation_id,
        )

    def _interrupt_registered_turn(self, registration: JobRegistration) -> None:
        """Best-effort interrupt of the turn this job is running (fire and forget).

        The terminal write already decided the outcome, so a failed interrupt is
        logged and dropped: the worst case is a model turn that keeps burning
        tokens until it finishes on its own, never a wrong job state. A job
        whose turn has not started yet (queued) has nothing to interrupt — the
        worker will find the row terminal and exit.
        """
        interrupt = registration.interrupt
        if interrupt is None:
            return
        try:
            interrupt()
        except Exception as exc:  # the interrupt is not the contract
            # The message, not just the class name: it names the turn RPC that
            # failed, which tells a dead worker apart from a turn that outlived
            # its own claim.
            _LOGGER.warning("assistant question interrupt failed: %s", exc)

    def _release_conversation_turn(self, conversation_turn: tuple[str, str, int] | None) -> None:
        """Free the generation slot this job owns, without waiting for its worker.

        The worker frees the same slot when it notices the refused write; doing
        it here too is what makes the input box return immediately. The release
        is scoped to this turn, so a newer turn that took the slot in the
        meantime keeps it.
        """
        if conversation_turn is None:
            return
        from aiops_diagnostics.conversation_store import ConversationError

        conversation_id, scope_fingerprint, turn_no = conversation_turn
        with contextlib.suppress(ConversationError):
            self.conversation_store.release_turn(conversation_id, scope_fingerprint, turn_no)

    def _conversation_history(
        self,
        conversation_turn: tuple[str, str, int] | None,
        language: str,
    ) -> str:
        """The conversation's completed turns, as prompt text (#482).

        Failures are not failures of the question: a window that cannot be read
        yields no history, with one warning carrying the error code so the
        split between "no history" and "history unavailable" is visible in
        logs. Blanking the block on any exception is the same discipline the
        Jev fallback uses — the dependency may be down without the surface
        going down with it.

        The turn being generated is answer-less, so ``context_turns()`` filters
        it out on its own: no caller has to remember to exclude it.
        """
        if conversation_turn is None:
            return ""
        from aiops_diagnostics.conversation_context import build_history

        conversation_id, scope_fingerprint, _ = conversation_turn
        try:
            return build_history(
                self.conversation_store,
                conversation_id,
                scope_fingerprint,
                language,
                max_turns=self.gateway_settings.context_max_turns,
                max_tokens=self.gateway_settings.context_max_tokens,
            )
        except Exception as exc:  # the question must survive a bad window
            _LOGGER.warning("conversation history unavailable: %s: %s", type(exc).__name__, exc)
            return ""

    def _turn_claim_renewer(
        self, diagnosis_id: str, conversation_turn: tuple[str, str, int] | None
    ) -> threading.Event:
        """A stop signal for this diagnosis's claim renewal, and the thread using it.

        A turn claim is deliberately short-lived: ``BUSY_LOCK_SECONDS`` without
        a sign of life means the worker died, and the conversation must not stay
        wedged. A diagnosis outlives that window, so it has to say it is still
        here -- otherwise its own conversation stops being busy mid-generation
        and a second turn starts over the one still running.

        The renewer yields on two conditions, and both matter:

        * the turn is no longer this job's (``renew_turn_claim`` says so) --
          job completed, job stopped, claim already lapsed;
        * **the job row is finished** (``expired``/``cancelled``/terminal) --
          even while this worker is still catching up. Renewing past that point
          would keep holding a conversation for a job the user already polls as
          over, which is the gate outliving the thing it gates.

        It is a stop signal rather than a bare thread so that a worker which is
        done stops renewing *now*: waiting out the sleep interval means a
        finished diagnosis holds the slot for one renewal period longer than it
        lives, and under sustained traffic those sleeping threads pile up.
        """
        stop = threading.Event()
        if conversation_turn is None:
            return stop
        conversation_id, scope_fingerprint, turn_no = conversation_turn

        def renew() -> None:
            from aiops_diagnostics.conversation_store import ConversationError

            while not stop.wait(RENEW_TURN_CLAIM_SECONDS):
                with contextlib.suppress(ConversationError):
                    if self.store.diagnosis_is_finished(diagnosis_id):
                        return
                    if not self.conversation_store.renew_turn_claim(
                        conversation_id, scope_fingerprint, turn_no
                    ):
                        return

        threading.Thread(target=renew, name=f"turn-claim-{turn_no}", daemon=True).start()
        return stop

    def _complete_conversation_turn(
        self,
        conversation_turn: tuple[str, str, int] | None,
        question: str,
        answer: dict[str, Any] | None,
        *,
        cancelled: bool = False,
        guarded: bool = False,
        stop_renewer: threading.Event | None = None,
    ) -> None:
        """Write a finished answer into the conversation turn (if any).

        The ONE implementation of "a job finished, so its turn can be closed":
        both the qa/promo line and the diagnosis line call it, so the two can
        no longer disagree about what a finished turn looks like.

        Cancelled/failed turns drop their row: an interrupted generation never
        survives as a complete reply (#172).

        ``guarded`` names the one case where this job does not own the outcome:
        its own write to the job row was refused, so the job did not end here --
        the stop path or the deadline sweep ended it. The refusal owner closes
        the turn (the stop path releases it itself); when it has not got there
        yet this call closes it instead. Either way the turn ends without an
        answer: an ``expired`` job whose summary is readable in the history is
        the split between what the user polls and what the conversation says.
        """
        if guarded:
            cancelled = True
        if stop_renewer is not None:
            stop_renewer.set()
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

    def _finish_health_job(self, job_id: str, **fields: Any) -> bool:
        """Write a health job's terminal state, and say whether it landed.

        The one place this line decides what "the job is over" means, so the
        four call sites cannot answer that question differently — which is what
        they did when one of them was checked and three were not.

        A refused write is not an error to raise: the row was already terminal,
        which is a state other paths reach on purpose (the deadline sweep, the
        restart hook). Unlike the diagnosis line there is **no metric on this
        path at all**, so a refusal does not make two surfaces disagree — it
        had no observable consequence, which is exactly why it went unnoticed.
        What it left behind was a worker doing work whose result nobody would
        read, with nothing anywhere to say so.

        So the refusal is **logged**: one line naming the code the job would
        have recorded. That is the observable this path can have today, and it
        is what makes "the result is checked" true here rather than nominal.
        """
        if not self.store.update_health_job(job_id, **fields):
            _LOGGER.warning(
                "health job terminal write refused: job=%s status=%s",
                job_id,
                fields.get("status"),
            )
            return False
        return True

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
                # The report's prose is generated ONCE and stored, like a
                # diagnosis result — so it is written in the language the job
                # was CREATED in, read back from the row, not from whatever
                # Accept-Language a later poll happens to carry. Rendering the
                # summary per-poll would also mean the stored report's language
                # changed under a reader who already saw it.
                job_language = str(
                    self.store.get_health_job(job_id, context.scope_fingerprint).get("language")
                    or DEFAULT_LANGUAGE
                )
                report = build_minimal_health_report(
                    sources,
                    order_no,
                    self.diagnostic_settings.safety,
                    language=job_language,
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
                report["curves"] = build_curves(samples, job_language)
                report = enrich_report(report, samples, order)
            # Every terminal write below is checked, and the refusal is the
            # same quiet exit the qa line takes (`_execute_assistant_qa`, #355):
            # a refused write means the row already ended — expired, or driven
            # terminal by another path — so this job's answer is not the answer
            # anyone is waiting for. Reporting success for it is how the metric
            # and the row disagree.
            if time.monotonic() - started > 30:
                self._finish_health_job(
                    job_id,
                    status="failed",
                    error_code="REPORT_TIMEOUT",
                    error_message="health report timed out",
                )
                return
            self._finish_health_job(job_id, status="completed", report=report)
        except HealthReportError as exc:
            self._finish_health_job(
                job_id,
                status="failed",
                error_code=exc.code,
                error_message=str(exc),
            )
        except (SourceError, ValueError) as exc:
            self._finish_health_job(
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
        conversation_turn: tuple[str, str, int] | None = None,
        turn_registrar: Callable[[Any], None] | None = None,
    ) -> None:
        if not self.store.update_standard_diagnosis(diagnosis_id, status="running"):
            # Refused claim: the row was already terminalised by another path,
            # or it expired while queued. Nothing was generated, so the turn
            # must not survive -- an answer-less row would hold the
            # conversation's slot until the claim lapsed and then sit in the
            # history with no answer.
            # No renewer exists yet: the job never started.
            self._complete_conversation_turn(conversation_turn, request.problem, None, cancelled=True)
            return
        started_ms = time.monotonic()
        # The conversation claim is the frontend's concurrency gate while this
        # runs, but a claim only counts as live for BUSY_LOCK_SECONDS (120s) and
        # a diagnosis outlives that. The renewer stops at the terminal writes
        # below, on a stop, on expiry, or when the claim stops being ours.
        claim_stop = self._turn_claim_renewer(diagnosis_id, conversation_turn)
        history = self._conversation_history(conversation_turn, language)
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        settings.agent.run_root = self.diagnostic_settings.agent.run_root
        try:
            query_scope = resolve_query_scope(context)
            # One range object: the tenant is carried once, as the frozen
            # ``QueryScope`` the SQL push-down and the tool layer both read. It
            # used to also arrive as ``allowed_tenants={context.effective_tenant_id}``
            # — the same tenant a second time, as a Python post-filter over rows
            # the push-down had already returned.
            result = run_agent_diagnosis(
                workspace,
                request,
                settings,
                None,
                provider=provider,
                key_slot=key_slot,
                scope=query_scope,
                language=language,
                history=history,
                turn_registrar=turn_registrar,
            )
        except (AgentRuntimeError, SourceError, ValueError) as exc:
            ok = self.store.update_standard_diagnosis(
                diagnosis_id,
                status="failed",
                error_code="DIAGNOSIS_FAILED",
                error_message=_internal_error_message(exc, request.order_no),
            )
            # Only a write that landed has a terminal row to match: when the
            # row was expired or cancelled underneath us, the turn is dropped
            # by the cancellation path (or by the refused-claim path), and it
            # must not claim an answer this job never produced.
            self._complete_conversation_turn(
                conversation_turn,
                request.problem,
                None,
                cancelled=True,
                guarded=not ok,
                stop_renewer=claim_stop,
            )
            # **Only a failure that landed is counted** (#499 review). When a
            # cancel won the race, the interrupt makes this worker raise — and
            # counting that as ``failed`` would record the same job twice, once
            # as cancelled and once as failed, inflating the failure rate with
            # jobs the user deliberately stopped. Mirrors the qa line's early
            # return on a refused terminal write.
            if not ok:
                return
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
            error_code, error_message = _blocked_diagnosis_error(workspace)
            ok = self.store.update_standard_diagnosis(
                diagnosis_id,
                status="failed",
                error_code=error_code,
                error_message=error_message,
            )
            self._complete_conversation_turn(
                conversation_turn,
                request.problem,
                None,
                cancelled=True,
                guarded=not ok,
                stop_renewer=claim_stop,
            )
            self._record_metric(
                tenant_id=context.effective_tenant_id,
                route_type="diagnosis",
                outcome="failed",
                error_code=error_code,
                duration_ms=int((time.monotonic() - started_ms) * 1000),
            )
            return
        public_status = "completed" if result.status.value == "diagnosed" else "inconclusive"
        stored = self.store.update_standard_diagnosis(
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
        # A refused write means the row already ended (expired, or cancelled
        # through the stop path): the job's answer is real, but it is not the
        # answer the conversation's turn is waiting for. Either the stop path
        # already dropped the row -- in which case this write only finds an
        # absent turn and does nothing -- or it is about to, and ``guarded``
        # makes that outcome depend on which write gets there first, never on
        # whether the answer gets stored. What must NOT happen is the
        # reverse: an ``expired`` job whose summary is readable in the history.
        self._complete_conversation_turn(
            conversation_turn,
            request.problem,
            {"summary": dumped.get("summary"), "root_cause": dumped.get("root_cause")},
            guarded=not stored,
            stop_renewer=claim_stop,
        )
        if not stored:
            # The row was already terminal, so this run produced nothing anyone
            # will read. Recording a success here is what makes the metric and
            # the row disagree — the metric says a diagnosis completed, the row
            # says `expired`, and the caller polls the row.
            return
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
        turn_registrar: Callable[[Any], None] | None = None,
        skip_retrieval: bool = False,
    ) -> None:
        def _finish_turn(answer: dict[str, Any] | None, *, cancelled: bool = False) -> None:
            self._complete_conversation_turn(conversation_turn, question, answer, cancelled=cancelled)

        if not self.store.update_assistant_question(qa_id, status="running"):
            _finish_turn(None, cancelled=True)
            return
        started_ms = time.monotonic()
        history = self._conversation_history(conversation_turn, language)
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
            if not self.store.update_assistant_question(qa_id, status="completed", result=rag_result):
                _finish_turn(None, cancelled=True)
                return
        elif (
            not skip_retrieval
            and tenant_id
            and self.kb_search_client is not None
            and self.media_signer is not None
        ):
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
                turn_registrar=turn_registrar,
                history=history,
            )
        if rag_result is not None:
            if rag_result is TERMINAL_WRITE_REFUSED:
                _finish_turn(None, cancelled=True)
                return
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
                turn_registrar=turn_registrar,
                history=history,
            )
        except (AgentRuntimeError, SourceError, ValueError) as exc:
            if not self.store.update_assistant_question(
                qa_id,
                status="failed",
                error_code="QA_FAILED",
                error_message=_internal_error_message(exc, ""),
            ):
                _finish_turn(None, cancelled=True)
                return
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
        if not self.store.update_assistant_question(
            qa_id,
            status="completed",
            result=answer,
        ):
            _finish_turn(None, cancelled=True)
            return
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
        turn_registrar: Callable[[Any], None] | None = None,
        history: str = "",
    ) -> dict[str, Any] | None:
        """Run the published customer agent path (T3/#170) or fall back.

        Returns the completed job record when the RAG path produced a result;
        None when there is no published customer agent for this tenant or the
        kb dependency is unavailable, letting the caller keep the existing
        zero-order behavior; ``TERMINAL_WRITE_REFUSED`` when the claim-guard
        turned the job's terminal write away, which means nothing was
        persisted and the caller must stop without recording a metric or a
        turn. With ``promo_intent`` (#231) the pinned promotional agent serves
        instead of the customer-service agent; an unresolvable target or empty
        library returns the honest empty card and never falls through to
        FAQ/customer QA.
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
                if not self.store.update_assistant_question(qa_id, status="completed", result=result):
                    return TERMINAL_WRITE_REFUSED
                return result
            selection = promo
            promo_prompt_text = promo_prompt(
                promo, question, language=language, intent=promo_intent, history=history
            )
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
                turn_registrar=turn_registrar,
                history=history,
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
                if not self.store.update_assistant_question(qa_id, status="completed", result=result):
                    return TERMINAL_WRITE_REFUSED
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
            if not self.store.update_assistant_question(
                qa_id,
                status="failed",
                error_code="QA_FAILED",
                error_message=_internal_error_message(exc, ""),
            ):
                return TERMINAL_WRITE_REFUSED
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

                if not self.store.update_assistant_question(
                    qa_id,
                    status="completed",
                    result=promo_empty_result(language, promo_intent, retrieval_status="unavailable"),
                ):
                    return TERMINAL_WRITE_REFUSED
                return {"status": "completed"}
            if not self.store.update_assistant_question(
                qa_id,
                status="failed",
                error_code="QA_FAILED",
                error_message=_internal_error_message(exc, ""),
            ):
                return TERMINAL_WRITE_REFUSED
            return {"status": "failed"}
        # Metrics-only attribution tag: the job result and the conversation
        # turn payload stay on the unchanged blocks contract (no agent_version
        # key — it must not leak into stored/public payloads).
        result = {
            **result,
            "agent_version": f"{selection.agent_id}#v{selection.version_no}",
        }
        if not self.store.update_assistant_question(
            qa_id,
            status="completed",
            result={key: value for key, value in result.items() if key != "agent_version"},
        ):
            return TERMINAL_WRITE_REFUSED
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
            error_message = _internal_error_message(exc, request.order_no)
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
            error_message = _internal_error_message(exc, request.order_no)
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


#: How often a running diagnosis says its conversation claim is still alive.
#: Comfortably below ``BUSY_LOCK_SECONDS`` so one missed tick cannot lose the
#: gate, and large enough that a minutes-long job renews a handful of times.
RENEW_TURN_CLAIM_SECONDS = BUSY_LOCK_SECONDS / 3


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
        # Diagnosis turns carry summary/root_cause, not blocks[] — the same
        # two fields the diagnosis metric's token estimate uses.
        for field in ("summary", "root_cause"):
            text += str(answer.get(field) or "")
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


def _internal_error_message(error: Exception, order_no: str | None) -> str:
    """The redacted reason, for the RECORD only — never for the response.

    ``str(exc)`` is whatever the failing layer raised: an upstream SDK message,
    a socket error, a parser's complaint. The contract already promised this
    surface never carries internal run information (`standard-api-contract.md`
    §error.message), and the promise was not kept: the raw string was returned
    verbatim as the user-facing `message`, so a 管家端 user could read the
    harness's internals in whatever language that layer happened to use.

    The caller stores this in the job row, where an engineer reads it; the
    response gets the bounded, coded copy instead (``DIAGNOSIS_ERROR_MESSAGES``).
    """
    message = redact_text(str(error), preserve=(order_no or "",))
    return message[:1000] if message else error.__class__.__name__


def _blocked_diagnosis_error(workspace: AgentWorkspace) -> tuple[str, str]:
    """Decide how one blocked run is reported on the standard API surface.

    ``DIAGNOSIS_BLOCKED`` is the provider/task failure code: the model could not
    produce the structured output. "This order is not in the caller's tenant"
    used to be absorbed into the same code, which left an operator unable to tell
    an authorization failure from a supplier failure — the two have opposite
    remedies.

    The shared rule records that condition exactly once, as a coded blocked
    entry, so this surface reads the record instead of re-deciding the condition
    (re-deciding is how one request used to collect two status codes). The
    message states the reason without naming the foreign tenant: the caller is a
    delegated end user, and which tenant owns an order they cannot see is not
    theirs to learn.

    Reachability on this face is deliberately one-directional. The worker runs
    with a frozen ``QueryScope`` and the scoped source set, so the tenant is
    pushed into SQL and the row-level rule only ever sees rows the predicate
    already admitted; an out-of-scope order is refused earlier still, at create
    time, by the same scope. ``DIAGNOSIS_ORDER_OUT_OF_SCOPE`` is therefore
    defense in depth, not a terminal state the frontend should wait for: it
    appears only when a row survives the push-down and *still* fails the
    row-level rule — a source that cannot push the rule down, or a scope
    re-resolved differently between create and worker. Both contract docs say
    so, so nobody promises a signal production does not emit.
    """
    try:
        entries = EvidenceJournal(workspace, workspace.load_manifest()).entries()
    except (FileNotFoundError, ValueError, OSError):
        entries = []
    for entry in entries:
        if entry.status == "blocked" and entry.source == TENANT_SCOPE_SOURCE:
            return DIAGNOSIS_ORDER_OUT_OF_SCOPE, "order is outside the authorized tenant scope"
    return "DIAGNOSIS_BLOCKED", "diagnosis could not complete"


def close_gateway_runtime(runtime: GatewayRuntime) -> None:
    with contextlib.suppress(Exception):
        runtime.shutdown()
