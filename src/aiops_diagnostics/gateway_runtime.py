from __future__ import annotations

import contextlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from aiops_diagnostics.config import Settings
from aiops_diagnostics.conversation_store import BUSY_LOCK_SECONDS
from aiops_diagnostics.execution.agent_runs import AgentRunStore, scope_from_json, scope_to_json
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import ACTIVE_DIAGNOSIS_STATUSES, GatewayStore
from aiops_diagnostics.health_curves import build_curves
from aiops_diagnostics.health_metrics import enrich_report
from aiops_diagnostics.health_report import (
    HEALTH_RULE_VERSION,
    HealthReportError,
    build_minimal_health_report,
)
from aiops_diagnostics.i18n import (
    DEFAULT_LANGUAGE,
)
from aiops_diagnostics.identity.order_visibility import tenant_matches
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import SCOPE_TYPE_ALL, DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KbServiceClient,
    KnowledgeSearchClient,
    KnowledgeSearchUnavailable,
    MediaAccessDenied,
    MediaGrant,
    MediaProxy,
    MediaResourceSigner,
    MediaResponse,
)
from aiops_diagnostics.ops_analysis import (
    OPERATIONS_CARD_FAILURE_RATE,
    OPERATIONS_CARD_OFFLINE,
    OPERATIONS_CARD_PAYMENT,
    OPERATIONS_CARDS,
    REASON_NO_FAULTS,
    REASON_NO_INCIDENTS,
    REASON_NO_ORDERS,
    REASON_SCOPE_EMPTY,
    AnalysisAggregate,
    FaultCodeAggregate,
    OfflineAggregate,
    PaymentAggregate,
    build_failure_rate_contract,
    build_fault_code_contract,
    build_offline_contract,
    build_payment_contract,
)
from aiops_diagnostics.ops_analysis import (
    RULE_VERSION as OPS_ANALYSIS_RULE_VERSION,
)
from aiops_diagnostics.redaction import redact_text
from aiops_diagnostics.sources import SourceError, scoped_live_sources

_LOGGER = logging.getLogger("aiops.gateway_runtime")


def package_version() -> str:
    """部署版本串。延迟导入：`__init__` 在部署时注入，模块级导入会把它冻住。"""
    from aiops_diagnostics import __version__

    return __version__


@dataclass
class JobRegistration:
    """保存当前 Dify/节点任务的取消回调与会话轮次。"""

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


def _as_datetime(value):
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class GatewayRuntime:
    """现役业务任务、凭据与异步交付的服务端装配。"""

    def __init__(
        self,
        store: GatewayStore,
        gateway_settings: GatewayServerSettings,
        diagnostic_settings: Settings,
        *,
        kb_search_client: KnowledgeSearchClient | None = None,
        media_signer: MediaResourceSigner | None = None,
        agent_store: Any = None,
        agent_run_store: Any = None,
        dify_workspaces: Any = None,
        knowledge_dataset_sync_guard: Any = None,
    ) -> None:
        self.store = store
        self.gateway_settings = gateway_settings
        self.diagnostic_settings = diagnostic_settings
        # Customer QA RAG path (T3/#170): bounded kb-service search + signed
        # media. Both stay None unless configured, in which case the QA branch
        # keeps the plain zero-order answer (no knowledge, no media).
        self.kb_search_client = kb_search_client
        self.media_signer = media_signer
        self.agent_store = agent_store
        # 选择器打开时把 RAGFlow 的新库收敛成 Dify dataset（#1056）。两个都可为 None：
        # 缺任一个 ⇒ 那条路由只是不同步，清单照常返回。
        self.dify_workspaces = dify_workspaces
        # 选择器打开时的同步闸（#1056）：一次只跑一个 pass + 租户级下限。
        self.knowledge_dataset_sync_guard = knowledge_dataset_sync_guard
        # APP-RUNTIME-01：聊天入口把「等节点」搬进后台，worker 需要同一份 run 存储。
        # 缺省为 None ⇒ 聊天节点路线不可用（与未配置绑定等价，fail closed）。
        self.agent_run_store = agent_run_store
        # APP-RUNTIME-02：Dify 路线的执行记录与取数凭据。同一个库、独立的表 ——
        # 不复用 `agent_runs`，因为那张表的 `node_id`/`matched` 语义不适用于 Dify。
        # 取消要能同时收掉执行记录与凭据，所以这里必须是一个真实的存储而不是就地新建。
        from aiops_diagnostics.dify.dify_run_store import DifyRunStore

        self.dify_run_store = DifyRunStore(store.path)
        # Routing decisions from typed answers (#392). Absent configuration
        # leaves this None, and every question simply routes as it did before —
        # the dependency is optional by design, not by accident.
        # Lazy media proxy (T3/#170 follow-up): built on first /v1/media hit;
        # without the kb-service + media-signing configuration it stays None
        # and the route answers a uniform 404.
        self._media_proxy: MediaProxy | None = None
        # Conversation turn backfill (T4/#172): same DB file, lazy-built.
        from aiops_diagnostics.conversation_store import ConversationStore

        self.conversation_store = ConversationStore(store.path)
        # Runtime registry (#584): which Dify app serves a (tenant, entry). Same
        # DB file, built here rather than injected so every construction path —
        # tests included — reads the real table. An empty table means the
        # environment has not adopted the registry, and the selection rule is
        # then exactly what it was before this existed.
        from aiops_diagnostics.dify.dify_app_registry import DifyAppRegistry

        # 试运行（#772 F / #781）：并发闸与"作业 → 上下文"登记。刻意不是线程池的一部分 ——
        # 试运行用**独立的闸门**，这样"不许超过 N 条试运行"与"诊断池还有没有位置"互不牵连，
        # 一次试运行不会因为占满池子而拖住线上诊断的提交。默认 1（按人串行；本票固定工作区
        # 只有 owner/admin，所以"按人"与"全局"在 V1 是同一个数）。
        self._trial_lock = threading.Lock()
        self._trial_jobs: set[str] = set()
        self._trial_contexts: dict[str, tuple[Any, Any, str, str]] = {}
        self._trial_max_concurrent = 1
        #: 运营商站点范围解析器（#782）：由装配层注入；None ⇒ 空范围（fail-closed）。
        self._trial_operator_scope: Any = None
        #: 订单授权器（APP-RUNTIME-01 续办）：聊天 worker 在**写结果之前**要用它再判一次
        #: 本人订单归属。与 `_trial_operator_scope` 同一条理由 —— 由装配层注入，
        #: 不在这里 import（会形成 gateway_api → gateway_runtime → gateway_api 的环）。
        #: 未注入 ⇒ worker 判不出归属 ⇒ 落 failed（fail closed），不写正文。
        self._order_authorizer: Any = None
        from aiops_diagnostics.consumer.consumer_photo import PhotoStore

        self.consumer_photos = PhotoStore()

        self.dify_app_registry = DifyAppRegistry(store.path)
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

    @classmethod
    def from_settings(
        cls,
        store: GatewayStore,
        gateway_settings: GatewayServerSettings,
    ) -> GatewayRuntime:
        from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
        from aiops_diagnostics.knowledge.knowledge_dataset_sync_guard import KnowledgeDatasetSyncGuard
        from aiops_diagnostics.publication.agent_lifecycle import AgentStore

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
            agent_run_store=AgentRunStore(store.path),
            dify_workspaces=(
                DifyWorkspaces(store.path) if gateway_settings.dify_external_knowledge_api_id else None
            ),
            knowledge_dataset_sync_guard=(
                KnowledgeDatasetSyncGuard() if gateway_settings.dify_external_knowledge_api_id else None
            ),
        )

    def shutdown(self) -> None:
        self.consumer_photos.close()
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
            tenant_id=context.effective_tenant_id,
        )
        if not created:
            return job
        future = self._executor.submit(self._execute_health_report, job["job_id"], context, order_no)
        self._futures[job["job_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(job["job_id"], None))
        return job

    def start_operations_analysis(
        self,
        context: ScopeContext,
        *,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        """Start an operations-analysis job (#697): site set + time window.

        ``site_ids=None`` means the caller's whole resolved site set — the same
        convention ``QueryScope.site_ids`` already uses (``None`` = no site
        narrowing) — while ``()`` is an explicit empty set and is refused here
        rather than silently turning into a full-tenant scan.

        The work happens on the worker, not here: the aggregate needs the SSH
        tunnel and the read-only connection, neither of which belongs on the
        request path.
        """
        if site_ids is not None and not site_ids:
            raise ValueError("analysis requires at least one site")
        job = self.store.create_analysis_job(
            context.scope_fingerprint,
            site_ids=site_ids,
            window_start=window_start,
            window_end=window_end,
            rule_version=OPS_ANALYSIS_RULE_VERSION,
            language=language,
            card=OPERATIONS_CARD_FAILURE_RATE,
            tenant_id=context.effective_tenant_id,
        )
        future = self._executor.submit(
            self._execute_operations_analysis,
            job["diagnosis_id"],
            context,
            site_ids,
            window_start,
            window_end,
            language,
        )
        self._futures[job["diagnosis_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(job["diagnosis_id"], None))
        return job

    def get_operations_analysis(self, context: ScopeContext, analysis_id: str) -> dict[str, Any] | None:
        return self.store.get_standard_diagnosis(analysis_id, context.scope_fingerprint)

    def start_operations_card(
        self,
        context: ScopeContext,
        *,
        card: str,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        """Start one of the #698 cards (offline piles / fault codes) on the same surface.

        Same job row, same window semantics, same scope push-down as the failure-rate
        card — a third and fourth *kind* rather than a third and fourth mechanism.
        ``card`` is stored on the row so the poller gets the same shape back that it
        asked for, instead of having to remember which route it called.
        """
        if site_ids is not None and not site_ids:
            raise ValueError("analysis requires at least one site")
        if card not in OPERATIONS_CARDS:
            raise ValueError("unknown operations card")
        job = self.store.create_analysis_job(
            context.scope_fingerprint,
            site_ids=site_ids,
            window_start=window_start,
            window_end=window_end,
            rule_version=OPS_ANALYSIS_RULE_VERSION,
            language=language,
            card=card,
            tenant_id=context.effective_tenant_id,
        )
        future = self._executor.submit(
            self._execute_operations_card,
            job["diagnosis_id"],
            context,
            card,
            site_ids,
            window_start,
            window_end,
            language,
        )
        self._futures[job["diagnosis_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(job["diagnosis_id"], None))
        return job

    def _end_analysis_job(
        self,
        analysis_id: str,
        tenant_id: str,
        *,
        result: dict[str, Any] | None,
        started_ms: float,
        error_code: str | None = None,
        error_message: str = "",
    ) -> None:
        """End one operations-analysis job, and never let that write escape.

        The shape this exists for (#700, seen on 41): the terminal write sits
        *after* the worker's ``except`` block, so an exception raised by the
        write itself — a locked database, a disk error — escaped the worker with
        no handler reached: no terminal row, no metric, no log line, and the job
        sat on ``running`` until the 15-minute deadline expired. Enumerating
        exception classes cannot cover a write that happens outside their
        ``try``; the only shape that can is "try to end the job, and if even
        that fails, end it anyway".

        Both workers share this so the two cannot drift: the failure-rate path
        and the card path had byte-identical terminal blocks before, which is
        exactly how one of them would have kept the hole.
        """
        status = "failed" if error_code else "completed"
        try:
            stored = self.store.update_standard_diagnosis(
                analysis_id,
                status=status,
                result=result,
                error_code=error_code,
                error_message=error_message or None,
            )
        except Exception:  # noqa: BLE001 - the whole point is that this cannot escape
            stored = False
        if not stored:
            # Either the write failed or the row was already terminal. One more
            # attempt, terminal regardless: a row nothing will ever terminate is
            # worse than one that reports it could not record its outcome.
            with contextlib.suppress(Exception):
                self.store.update_standard_diagnosis(
                    analysis_id,
                    status="failed",
                    error_code=error_code or "ANALYSIS_FAILED",
                    error_message=error_message or "analysis job could not record its outcome",
                )
            return
        self._record_metric(
            tenant_id=tenant_id,
            route_type="operations_analysis",
            outcome="completed" if status == "completed" else "failed",
            error_code=error_code,
            duration_ms=int((time.monotonic() - started_ms) * 1000),
        )

    def _execute_operations_card(
        self,
        analysis_id: str,
        context: ScopeContext,
        card: str,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str,
    ) -> None:
        """Compute the card's aggregate, let the model explain it, and store both."""
        if not self.store.update_standard_diagnosis(analysis_id, status="running"):
            return  # 行已终态：没有生成任何东西，直接退出
        started_ms = time.monotonic()
        try:
            scope = resolve_query_scope(context)
            effective_sites = _intersect_sites(site_ids, scope.site_ids)
            if effective_sites == ():
                result = self._empty_card_result(card, language, site_ids, window_start, window_end)
            else:
                with scoped_live_sources(self.diagnostic_settings, scope=scope) as sources:
                    result = self._compute_card(
                        card,
                        sources,
                        effective_sites,
                        window_start,
                        window_end,
                        language,
                        site_ids,
                    )
        # `except Exception`, not an enumerated tuple, and that is the whole point
        # (#700 → #721). Three separate passes added one class each time —
        # `AgentRuntimeError` (a `RuntimeError`), then `TypeError` — and each fix
        # left the next class still escaping, because the failure mode is not
        # "which exceptions can be raised" but "a worker that raises anything
        # unlisted writes no terminal row at all". The row then sits on `running`
        # until the deadline expires, and the caller cannot tell it apart from
        # work still in progress.
        #
        # The fourth instance is why the shape changed rather than the tuple
        # growing again: `resolve_query_scope` (called inside this `try`) raises
        # `ScopeError`, a `RuntimeError` that was in **neither** list — so an
        # oversized data scope, a `self` range with no bound C-end user, or a
        # missing Dis directory hung the job exactly like the three before it.
        # Enumerating classes cannot converge; ending the job can.
        #
        # `except Exception` does not swallow `KeyboardInterrupt`/`SystemExit`
        # (not `Exception` subclasses), and it matches what `_end_analysis_job`
        # already does for the terminal write — "a row nothing will ever
        # terminate is worse than one that reports it could not record its
        # outcome" applies to the computation too. A genuine bug now shows up as
        # a failed job carrying its message, instead of a silent hang.
        except Exception as exc:  # noqa: BLE001 - see above: escaping here means no terminal row
            ok = self.store.update_standard_diagnosis(
                analysis_id,
                status="failed",
                error_code="ANALYSIS_FAILED",
                error_message=_internal_error_message(exc, ""),
            )
            if ok:
                self._record_metric(
                    tenant_id=context.effective_tenant_id,
                    route_type="operations_analysis",
                    outcome="failed",
                    error_code="ANALYSIS_FAILED",
                    duration_ms=int((time.monotonic() - started_ms) * 1000),
                )
            return
        self._end_analysis_job(
            analysis_id,
            context.effective_tenant_id,
            result=result,
            started_ms=started_ms,
        )

    def _empty_card_result(
        self,
        card: str,
        language: str,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        """The honest empty for a card whose scope resolved to nothing.

        Which reason code it carries depends on the card, because they空的是不同的表:
        an operator reading `NO_INCIDENTS` looks at the offline monitor, while
        `SCOPE_EMPTY` means their own account resolved to no sites.
        """
        kwargs = dict(
            language=language,
            site_ids=site_ids,
            window_start=_as_datetime(window_start),
            window_end=_as_datetime(window_end),
            reason_code=REASON_SCOPE_EMPTY,
        )
        if card == OPERATIONS_CARD_OFFLINE:
            return build_offline_contract(OfflineAggregate(0, 0, 0, 0), None, **kwargs)
        if card == OPERATIONS_CARD_PAYMENT:
            return build_payment_contract(PaymentAggregate(0, 0, 0, 0, 0), None, **kwargs)
        return build_fault_code_contract(FaultCodeAggregate(0, 0, ()), None, **kwargs)

    def _compute_card(
        self,
        card: str,
        sources: Any,
        effective_sites: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str,
        site_ids: tuple[str, ...] | None,
    ) -> dict[str, Any]:
        """One card end to end: aggregate → optional model turn → assembled contract."""
        db_start, db_end = _db_window_bound(window_start), _db_window_bound(window_end)
        devices_total = sources.mysql.count_devices(effective_sites)
        window = dict(
            language=language,
            site_ids=site_ids,
            window_start=_as_datetime(window_start),
            window_end=_as_datetime(window_end),
        )
        if card == OPERATIONS_CARD_OFFLINE:
            raw = sources.mysql.aggregate_offline_incidents(effective_sites, db_start, db_end)
            if raw is None:
                return self._empty_card_result(card, language, site_ids, window_start, window_end)
            aggregate = OfflineAggregate(
                incidents=int(raw["incidents"]),
                offline_devices=int(raw["offline_devices"]),
                open_incidents=int(raw["open_incidents"]),
                offline_minutes=int(raw["offline_minutes"]),
                devices_total=devices_total,
            )
            if aggregate.incidents == 0:
                return build_offline_contract(aggregate, None, reason_code=REASON_NO_INCIDENTS, **window)
            payload = self._narrative_for(card, aggregate, window)
            return build_offline_contract(aggregate, payload, **window)
        if card == OPERATIONS_CARD_PAYMENT:
            raw_payment = sources.mysql.aggregate_payment_split(effective_sites, db_start, db_end)
            if raw_payment is None:
                return self._empty_card_result(card, language, site_ids, window_start, window_end)
            payment = PaymentAggregate(
                total=int(raw_payment["total"]),
                paid=int(raw_payment["paid"]),
                paid_refunded=int(raw_payment["paid_refunded"]),
                paid_net=int(raw_payment["paid_net"]),
                unpaid=int(raw_payment["unpaid"]),
            )
            if payment.total == 0:
                return build_payment_contract(payment, None, reason_code=REASON_NO_ORDERS, **window)
            payload = self._narrative_for(card, payment, window)
            return build_payment_contract(payment, payload, **window)
        raw = sources.mysql.aggregate_fault_codes(effective_sites, db_start, db_end)
        if raw is None:
            return self._empty_card_result(card, language, site_ids, window_start, window_end)
        aggregate = FaultCodeAggregate(
            total_faults=int(raw["total_faults"]),
            faulty_devices=int(raw["faulty_devices"]),
            top_codes=tuple(raw.get("top_codes") or ()),
            devices_total=devices_total,
        )
        if aggregate.total_faults == 0:
            return build_fault_code_contract(aggregate, None, reason_code=REASON_NO_FAULTS, **window)
        # #700: this call lost its `card` argument when ``_narrative_for`` gained
        # one (#698) — so every fault-code card with data raised `TypeError` here,
        # inside the worker, where no handler catches it: no terminal write, and
        # the job sat on `running` until it expired. It survived two review passes
        # and the local suite because the local double never reaches this line
        # (the aggregate is non-empty only against real data).
        payload = self._narrative_for(card, aggregate, window)
        return build_fault_code_contract(aggregate, payload, **window)

    def _narrative_for(self, card: str, aggregate: Any, window: dict[str, Any]) -> Any:
        """The model's two prose fields for a card (never its numbers).

        The prompt carries the **already-computed** contract — built with ``payload=None``,
        which yields every number and no prose — so the model explains a settled result
        rather than producing one. A failure here raises and fails the job: delivering
        numbers with an empty explanation reads as "the aggregate had nothing to say",
        which is a different and false claim.
        """
        from aiops_diagnostics.ops_analysis_narrative import run_narrative_turn

        if card == OPERATIONS_CARD_OFFLINE:
            facts = build_offline_contract(aggregate, None, **window)
        elif card == OPERATIONS_CARD_PAYMENT:
            facts = build_payment_contract(aggregate, None, **window)
        else:
            facts = build_fault_code_contract(aggregate, None, **window)
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        selected = self.diagnostic_settings.agent.select_provider(None)
        return run_narrative_turn(
            facts,
            settings.agent,
            provider=selected,
            key_slot=selected.resolved_key_slot(),
            language=str(window["language"]),
        )

    def _execute_operations_analysis(
        self,
        analysis_id: str,
        context: ScopeContext,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str,
    ) -> None:
        """Compute the aggregate, let the model explain it, and store both.

        The numbers are computed first and the narrative second, but the two are
        independent: a narrative failure ends the job as failed rather than
        delivering numbers with an empty explanation — that shape reads as "the
        aggregate had nothing to say", which is a different and false claim.
        """
        if not self.store.update_standard_diagnosis(analysis_id, status="running"):
            return  # 行已终态（过期/被另一条路径收敛）：没有生成任何东西，直接退出
        started_ms = time.monotonic()
        try:
            scope = resolve_query_scope(context)
            # The job's own site set is the SUBSET the caller asked for; the
            # scope's is the superset they may see. Empty after the intersection
            # means the request was wider than the scope in the narrowing
            # direction, so nothing is computed — the same honest empty the
            # empty-scope short-circuit gives.
            effective_sites = _intersect_sites(site_ids, scope.site_ids)
            if effective_sites == ():
                result = build_failure_rate_contract(
                    AnalysisAggregate(0, 0, 0, ()),
                    None,
                    language=language,
                    site_ids=site_ids,
                    window_start=_as_datetime(window_start),
                    window_end=_as_datetime(window_end),
                    reason_code=REASON_SCOPE_EMPTY,
                )
            else:
                with scoped_live_sources(self.diagnostic_settings, scope=scope) as sources:
                    raw = sources.mysql.aggregate_order_statuses(
                        effective_sites,
                        _db_window_bound(window_start),
                        _db_window_bound(window_end),
                    )
                if raw is None:
                    result = build_failure_rate_contract(
                        AnalysisAggregate(0, 0, 0, ()),
                        None,
                        language=language,
                        site_ids=site_ids,
                        window_start=_as_datetime(window_start),
                        window_end=_as_datetime(window_end),
                        reason_code=REASON_SCOPE_EMPTY,
                    )
                else:
                    aggregate = AnalysisAggregate(
                        total=int(raw["total"]),
                        abnormal_ended=int(raw["abnormal_ended"]),
                        normal_ended=int(raw["normal_ended"]),
                        top_sites=tuple(raw.get("top_sites") or ()),
                    )
                    if aggregate.total == 0:
                        result = build_failure_rate_contract(
                            aggregate,
                            None,
                            language=language,
                            site_ids=site_ids,
                            window_start=_as_datetime(window_start),
                            window_end=_as_datetime(window_end),
                            reason_code=REASON_NO_ORDERS,
                        )
                    else:
                        result = self._analyze_with_narrative(
                            aggregate, context, site_ids, window_start, window_end, language
                        )
        # #700: 叙述轮在模型回散文/供应商失败时抛 ``AgentRuntimeError``（一个
        # ``RuntimeError``），而这里只接了源与取值两类 —— 于是那次运行**没有写任何
        # 终态**，作业停在 ``running`` 直到过期。
        #
        # 这已经是**第四次**同一个缺口，所以改的是**形状**而不是再往元组里塞一个名字：
        # 第一版只接 ``SourceError``/``ValueError``，#700 补了 ``TypeError``，而
        # ``resolve_query_scope`` 在上面那个 ``try`` 里抛的 ``ScopeError`` 同样是个
        # ``RuntimeError``，**两个版本都没接** —— 范围超限 / ``self`` 范围但主体没绑
        # C 端用户 / 代查缺 Dis 配置，都会让作业停在 ``running``。
        # 在一个不断增长的名单上收敛不了；把作业终结掉可以。
        # 与 ``_end_analysis_job`` 同一条理由：一个永远不会终结的行比一行「报了它记不下结果」
        # 更糟。``except Exception`` 不会吞 ``KeyboardInterrupt``/``SystemExit``。
        except Exception as exc:  # noqa: BLE001 - 见上：从这里逃出去就是没有终态行
            ok = self.store.update_standard_diagnosis(
                analysis_id,
                status="failed",
                error_code="ANALYSIS_FAILED",
                error_message=_internal_error_message(exc, ""),
            )
            if ok:
                self._record_metric(
                    tenant_id=context.effective_tenant_id,
                    route_type="operations_analysis",
                    outcome="failed",
                    error_code="ANALYSIS_FAILED",
                    duration_ms=int((time.monotonic() - started_ms) * 1000),
                )
            return
        self._end_analysis_job(
            analysis_id,
            context.effective_tenant_id,
            result=result,
            started_ms=started_ms,
        )

    def _analyze_with_narrative(
        self,
        aggregate: Any,
        context: ScopeContext,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str,
    ) -> dict[str, Any]:
        """The narrative turn, then assembly. Split out so the job-end writes above stay readable."""
        from aiops_diagnostics.ops_analysis_narrative import run_narrative_turn

        selected_provider = self.diagnostic_settings.agent.select_provider(None)
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        facts = build_failure_rate_contract(
            aggregate,
            None,
            language=language,
            site_ids=site_ids,
            window_start=_as_datetime(window_start),
            window_end=_as_datetime(window_end),
        )
        payload = run_narrative_turn(
            facts,
            settings.agent,
            provider=selected_provider,
            key_slot=selected_provider.resolved_key_slot(),
            language=language,
        )
        return build_failure_rate_contract(
            aggregate,
            payload,
            language=language,
            site_ids=site_ids,
            window_start=_as_datetime(window_start),
            window_end=_as_datetime(window_end),
        )

    def get_health_report(self, context: ScopeContext, job_id: str) -> dict[str, Any] | None:
        return self.store.get_health_job(job_id, context.scope_fingerprint)

    # 试用按应用绑定执行 Dify/Java 草稿，复用作业终态与并发保护。

    def start_trial(
        self,
        initiator: str,
        order_no: str,
        draft: Any,
        language: str,
        trial_initiator: Any = None,
    ) -> dict[str, Any] | None:
        """提交一次试运行，或返回 ``None`` 表示并发超限（路由转 429）。

        **执行范围不来自调用方**：它由配置的调试租户决定（见 :meth:`trial_run_context`）。
        ``initiator`` 只进审计，**不是**授权依据（授权在这一层之外：钥匙决定能不能调，
        配置的租户决定能看什么）。
        """
        run_context = (
            self.trial_context_for(trial_initiator)
            if trial_initiator is not None
            else self.trial_run_context()
        )
        with self._trial_lock:
            if len(self._trial_jobs) >= self._trial_max_concurrent:
                # 并发闸：试运行打的是**真数据源**与真模型，本票刻意串行 ——
                # "一次一人一条"是这个入口对自己可能压垮生产只读库的唯一防线。
                return None
            trial = self.store.create_trial_job(
                run_context.scope_fingerprint,
                order_no=order_no,
                draft_fingerprint=draft.fingerprint(),
                initiator=initiator,
                language=language,
                tenant_id=run_context.effective_tenant_id,
                workflow_trial=callable(getattr(draft, "execute", None)),
                node_trial=getattr(draft, "engine", "") == "node",
            )
            trial_id = str(trial["diagnosis_id"])
            self._trial_jobs.add(trial_id)
            # 登记未落库前把闸门信息放好：worker 需要 run_context（范围）与草稿。
            self._trial_contexts[trial_id] = (run_context, draft, order_no, language)
            future = self._executor.submit(self._execute_trial, trial_id)
            self._futures[trial_id] = future
            future.add_done_callback(lambda _: self._futures.pop(trial_id, None))
            future.add_done_callback(lambda _: self._release_trial(trial_id))
        return trial

    def _release_trial(self, trial_id: str) -> None:
        with self._trial_lock:
            self._trial_jobs.discard(trial_id)
            self._trial_contexts.pop(trial_id, None)

    def trial_run_context(self) -> ScopeContext:
        """试运行的**执行范围**：配置的固定调试租户，按**顶层账号口径**（整租户可见）。

        **为什么不是 ``admin_context``**：那个合成上下文是 ``data_scope(type="self")``
        且主体没有 C 端用户，``resolve_query_scope`` 对它直接抛 ``SCOPE_ERROR_EMPTY_SCOPE``
        —— 它是给"不碰订单数据"的发布/对账用的。试运行要真的查一张订单，所以它要的是
        顶层账号那一个口径（``type="all"`` = 不受站点维度约束），这也是客户身份在
        ``third_session_auth`` / ``company_token_auth`` 里走的那条。写成字面量而不是
        从某处"借"一个，是因为这个范围**本票就是硬编码的固定调试租户** —— #782 会把它
        换成发起人的公司身份（``operator`` 口径），届时只改这一个方法，路由与 worker 不动。

        **路由与 worker 必须用同一个范围**（路由拿它问订单授权、worker 拿它执行），
        所以它只在这里构造一次，不让两处各算一遍 —— 授权问的是 A、执行看的是 B 是
        "越权只在问了之后才发生"的形状。
        """
        return _tenant_wide_context(self.gateway_settings.dify_trial_debug_tenant)

    def trial_context_for(self, initiator: Any) -> ScopeContext:
        """发起人的**按人范围**（#782）：公司身份 → 运营商站点范围。

        与 ``trial_run_context`` 的区别只在数据范围来自谁：那个是固定的调试租户，
        这个是发起人本人在管家的可见范围。**规则只有一份** —— 站点解析复用网关已有
        的 ``UpmsOperatorSiteScope``（UPMS ``/shopuser/getShops`` + 既有隧道映射），
        ``build_trial_scope`` 把它折成 ``ScopeContext``。

        这条**同一个** ``ScopeContext`` 既用于订单授权（路由问它），也用于执行
        （worker 拿它跑），所以"问的"与"跑的"不可能漂。
        """
        from aiops_diagnostics.dify.dify_trial import build_trial_scope

        return build_trial_scope(initiator, operator_scope=self._trial_operator_scope)

    def set_trial_operator_scope(self, scope: Any) -> None:
        """注入运营商站点范围解析器（#782），由 API 装配层负责构造。

        **为什么是注入而不是在这里 import**：``gateway_api`` 已经 import 本模块，
        反向 import 会形成 ``gateway_api -> gateway_runtime -> gateway_api`` 的环
        （README 的分层说明会被 ``test_package_import_graph_stays_acyclic`` 判为失效）。
        构造它需要 ``Settings`` 与内部凭据，那正是装配层（``create_gateway_app``）
        已经持有的东西，所以放在那边构造、这里只接。

        未注入 ⇒ 运营商范围解析不出 ⇒ 试运行落到**空站点集合**（fail-closed），
        绝不回落成租户级。
        """
        self._trial_operator_scope = scope

    def set_order_authorizer(self, authorizer: Any) -> None:
        """注入订单授权器，由 API 装配层负责构造（理由同 `set_trial_operator_scope`）。

        聊天 worker 在节点跑完之后、把结果写进 QA **之前**必须再判一次本人订单归属：
        `final_release` 是**本地**判定，它按 docstring 的要求假定调用方已经做完可能阻塞的
        外部授权。worker 原先没有做，于是「节点跑完后订单转归他人」仍会写 `completed`
        并存下会话答案（Codex 复核必修 3 实测：授权计数在节点结束时与写完之后相同）。

        复用的是**同一个**授权器与同一套判据，没有为这条路新造权限规则。
        """
        self._order_authorizer = authorizer

    def get_trial(self, context: ScopeContext, trial_id: str) -> dict[str, Any] | None:
        return self.store.get_standard_diagnosis(trial_id, context.scope_fingerprint)

    def _execute_trial(self, trial_id: str) -> None:
        """作业体：认领一行 → 跑草稿 → 写终态。与诊断 worker 同一条纪律。

        **每一个失败都必须落一个终态**：worker 若从 ``running`` 逃逸出去，这一行会一直
        挂到 deadline，而试运行的并发闸是按"在跑的作业数"算的 —— 一行挂死会让那个发起人
        永久占掉一个名额。
        """
        entry = self._trial_contexts.get(trial_id)
        if entry is None:  # pragma: no cover - 只在释放竞态下发生
            return
        run_context, draft, order_no, language = entry
        if not self.store.update_standard_diagnosis(trial_id, status="running"):
            return
        started_ms = time.monotonic()
        try:
            result = draft.execute(self, trial_id, run_context, order_no, language)
        except Exception as exc:  # noqa: BLE001 - 逃出去就等于没有终态行
            from aiops_diagnostics.consumer.consumer_intent import TRIAL_MESSAGES
            from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

            code = (
                exc.code
                if isinstance(exc, ChatRunUnavailable) and exc.code in TRIAL_MESSAGES
                else "TRIAL_FAILED"
            )
            self.store.update_standard_diagnosis(
                trial_id,
                status="failed",
                error_code=code,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            self._record_trial_metric(run_context, "failed", started_ms)
            return
        # 「超出预算时结果标为未完成」：``blocked`` / ``inconclusive`` 都是**正常终态**，
        # 照实落库；只有真正的异常才是 failed。状态用字符串原样存，与诊断行同一套词表。
        self.store.update_standard_diagnosis(
            trial_id,
            status="completed",
            result={
                "status": result.status,
                "report": result.report,
                "evidence_ids": list(result.evidence_ids),
                "blocked_tools": list(result.blocked_tools),
                "failed_sources": list(result.failed_sources),
                "language": result.language,
            },
        )
        self._record_trial_metric(run_context, "completed", started_ms)

    def _record_trial_metric(self, context: ScopeContext, outcome: str, started_ms: float) -> None:
        self._record_metric(
            tenant_id=context.effective_tenant_id,
            route_type="trial",
            outcome=outcome,
            duration_ms=int((time.monotonic() - started_ms) * 1000),
        )

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
        from aiops_diagnostics.publication.agent_lifecycle import VIEW_ROLES

        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_roles.intersection(VIEW_ROLES):
            raise RuntimeError("agent access is not permitted")
        tenant_id = getattr(context, "effective_tenant_id", "") or ""
        summary = self.metrics_store.summary(
            tenant_id,
            agent_id=agent_id,
            limit_hours=window_hours,
        )
        # `expired` is not an outcome the metrics store ever records — the expiry
        # sweep runs as raw SQL and writes no metric row — so a tenant whose jobs
        # all ran out their deadline read as "nothing failed, nothing cancelled".
        # The count comes from the JOB rows instead, which now carry the tenant.
        #
        # TWO review findings shaped this, and both were right:
        #
        # ① `agent_id` narrows every other number in this payload, so an
        #    unfiltered expiry count would contradict them (X shows 0 runs and 1
        #    expiry). Job rows carry no agent id — `agent_id` is a QA/RAG notion
        #    and these rows predate it — so the count cannot be narrowed. It is
        #    therefore **omitted entirely** under an agent filter rather than
        #    reported as a number that means something else.
        #
        # ② The job tables have no tenant predicate to apply here (that is the
        #    reason this counter exists), so the store scans every tenant and the
        #    result comes back keyed by tenant. Values for OTHER tenants must
        #    never leave this method: a count of another tenant's expired jobs is
        #    cross-tenant data, and `expired_unattributed` (the NULL-tenant rows)
        #    is the worst case because it is one global number shared by all.
        from datetime import UTC, datetime, timedelta

        totals = dict(summary.get("totals") or {})
        if agent_id is None:
            cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
            expired = self.store.count_expired_jobs_by_tenant(since=cutoff)
            totals["expired"] = expired.get(tenant_id, 0)
        summary["totals"] = totals
        return summary

    def list_agent_metrics_runs(
        self,
        context: ScopeContext,
        *,
        agent_id: str | None = None,
        route_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from aiops_diagnostics.publication.agent_lifecycle import VIEW_ROLES

        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_roles.intersection(VIEW_ROLES):
            raise RuntimeError("agent access is not permitted")
        return self.metrics_store.list_runs(
            getattr(context, "effective_tenant_id", "") or "",
            agent_id=agent_id,
            route_type=route_type,
            limit=limit,
        )

    def start_consumer_chatflow(
        self,
        context,
        question,
        *,
        binding,
        conversation,
        conversation_turn_no,
        language,
        order_no="",
        data_kind="status",
        photo_id="",
        uses_context=True,
        expected_previous_qa_id=None,
    ):
        from aiops_diagnostics.consumer.consumer_chat_service import start

        return start(
            self,
            context,
            question,
            binding=binding,
            conversation=conversation,
            order_no=order_no,
            data_kind=data_kind,
            photo_id=photo_id,
            uses_context=uses_context,
            expected_previous_qa_id=expected_previous_qa_id,
            registration=JobRegistration(
                conversation_turn=(
                    conversation["conversation_id"],
                    context.scope_fingerprint,
                    conversation_turn_no,
                )
            ),
            language=language,
        )

    def start_operator_cpms(
        self,
        service,
        caller,
        binding,
        order,
        question,
        *,
        bearer,
        authorize,
        base,
        proxy,
        conversation_turn,
        language="zh",
        trial_manifest=None,
    ):
        return service.start(
            caller,
            binding,
            order,
            question,
            bearer=bearer,
            authorize=authorize,
            base=base,
            proxy=proxy,
            registration=JobRegistration(conversation_turn=conversation_turn, route_type="qa"),
            language=language,
            trial_manifest=trial_manifest,
        )

    def start_chat_order_qa(
        self,
        context: ScopeContext,
        question: str,
        *,
        order_no: str,
        binding: Any,
        language: str = DEFAULT_LANGUAGE,
        conversation: dict[str, Any] | None = None,
        conversation_turn_no: int | None = None,
    ) -> dict[str, Any]:
        """APP-RUNTIME-01：把「本人订单状态」交给客户节点，**立即返回**。

        与 `start_assistant_qa` 的分工：那一支是零阶知识问答，会起模型循环；这一支
        **不起第二个模型循环** —— 网关只派发 run 并等结果，答案来自节点交回的快照。

        顺序是刻意的：先建 QA（`queued`）并立刻返回（App 的 POST 不能等节点整轮），
        后台线程才去签发/派发/等结果；失败一律落 `failed`，**不回落直读**。

        `binding` 由调用方（路由层）按**可信的**环境/租户/入口/场景选出并冻结进 run，
        后台不再"再选一次"：重选就可能换到另一条，而用户看到的仍是同一个 qa_id。

        取消复用既有守卫：`cancel_assistant_qa` 把 QA 写成 `cancelled` 后，worker 的
        终态写会被 claim-guard 拒掉；这里额外在写之前确认自己没被取消。
        """
        from aiops_diagnostics.execution import chat_execution

        qa = self.store.create_assistant_question(
            context.scope_fingerprint,
            question,
            # **必须**用同一个常量：读取守卫按这个前缀认出「这条 QA 走的是节点路线」。
            # 这里写一个近似的字面量，GET 就会退回到直读的判据上。
            # 两条路线各有自己的前缀：读取守卫按前缀分流到**不同**的证据来源。
            internal_run_id=(
                chat_execution.DIFY_RUN_PREFIX
                if getattr(binding, "engine", "node") == "dify"
                else chat_execution.CHAT_RUN_PREFIX
            )
            + order_no,
            tenant_id=context.effective_tenant_id,
        )
        registration = JobRegistration(
            conversation_turn=(
                (conversation["conversation_id"], conversation["scope_fingerprint"], conversation_turn_no)
                if conversation is not None and conversation_turn_no is not None
                else None
            ),
            route_type="qa",
        )
        self._qa_registrations[qa["qa_id"]] = registration
        # APP-RUNTIME-02：一条登记只指向一个引擎。这里按 `binding.engine` 分派一次，
        # **没有**任何"节点失败再试 Dify"的回落 —— ADR-0079 的判据是一次请求只有一个
        # 模型循环，跨路线自动回落会改变执行边界与证据归属。
        worker = (
            self._execute_chat_dify_qa
            if getattr(binding, "engine", "node") == "dify"
            else self._execute_chat_order_qa
        )
        future = self._executor.submit(
            worker,
            qa["qa_id"],
            order_no,
            context,
            language,
            registration,
            question,
            binding,
        )
        self._futures[qa["qa_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(qa["qa_id"], None))
        future.add_done_callback(lambda _: self._qa_registrations.pop(qa["qa_id"], None))
        return qa

    def _execute_chat_dify_qa(
        self,
        qa_id: str,
        order_no: str,
        context: ScopeContext,
        language: str,
        registration: JobRegistration,
        question: str,
        binding: Any,
    ) -> None:
        """后台：签发取数凭据 → 建执行记录 → 调 Dify → 核结果 → 放行 → 写终态。

        与节点 worker 的关系是**并列**而不是复用：节点那条要派发、等领取、核对执行配置，
        这条要调 Dify 并核对「这次运行确实取到了这一单的数据」。两条共用的部分是
        **规则**（写前再授权、失败落 failed、不回落），不是流程。

        Dify 侧的成功判据有三条，缺一条都算失败：

        1. 实际运行的 workflow 身份与绑定一致（不是别的图、不是草稿）；
        2. 这一次有**同一轮**的取数记录（没有取数却自称成功 ⇒ 失败）；
        3. 输出能适配成 blocks，且订单号与本次冻结的订单一致。
        """
        from aiops_diagnostics.dify import dify_chat_workflow
        from aiops_diagnostics.dify.dify_run_store import DifyRunStore
        from aiops_diagnostics.execution import chat_data_grant, chat_execution

        dify_store = DifyRunStore(self.store.path)
        grant_store = chat_data_grant.DataGrantStore(self.store.path)
        if registration.cancelled:
            return
        if not self.store.update_assistant_question(qa_id, status="running"):
            return
        run_id: str | None = None
        try:
            workflow_key = dify_chat_workflow.load_workflow_key(binding.workflow_key_file)
            token, grant = grant_store.issue(
                scope_fingerprint=context.scope_fingerprint,
                order_no=order_no,
                environment=str(self.gateway_settings.execution_environment or ""),
                tenant_id=context.effective_tenant_id,
                qa_id=qa_id,
                workflow_id=binding.workflow_id,
            )
            run_id = "dify_" + secrets.token_urlsafe(16)
            dify_store.issue(
                run_id=run_id,
                qa_id=qa_id,
                scope_fingerprint=context.scope_fingerprint,
                scope_json=scope_to_json(context),
                binding_key=binding.key,
                order_no=order_no,
                environment=str(self.gateway_settings.execution_environment or ""),
                tenant_id=context.effective_tenant_id,
                app_id=getattr(binding, "app_id", ""),
                workflow_id=binding.workflow_id,
                grant_id=grant.grant_id,
                # 冻结"签发时对外的那一版"：撤回或重新发布之后，回读靠它判断这条 run
                # 跑的那一版还是不是现在对外的那一版。
                agent_version_no=int(getattr(binding, "agent_version_no", 0) or 0),
                route_revision=int(getattr(binding, "route_revision", 0) or 0),
            )
            if registration.cancelled:
                self._settle_dify_cancelled(dify_store, grant_store, run_id, qa_id)
                return
            client = dify_chat_workflow.DifyWorkflowClient(
                str(self.gateway_settings.dify_chat_base_url),
                # 必须短于取数凭据的有效期：还在等 Dify 时凭据已经过期，回调会被自己的
                # 有效期判据挡住，那是一个必然发生的假失败。
                timeout_seconds=chat_data_grant.GRANT_TTL_SECONDS - 30,
            )
            stop_lock = threading.Lock()
            stop_sent = False

            def started(workflow_run_id: str, task_id: str) -> None:
                if not dify_store.bind_workflow(run_id, workflow_run_id=workflow_run_id, task_id=task_id):
                    raise chat_execution.ChatRunUnavailable("DIFY_WORKFLOW_INVALID", "dify identity changed")

                def stop() -> None:
                    nonlocal stop_sent
                    with stop_lock:
                        if stop_sent:
                            return
                        stop_sent = True
                    client.stop(workflow_key=workflow_key, task_id=task_id, user=f"aiops:{grant.grant_id}")

                # 先安装再复查，覆盖取消发生在开始事件之前或安装钩子期间的交错。
                registration.interrupt = stop
                current = dify_store.get(run_id)
                if registration.cancelled or current is None or current.state != "running":
                    stop()

            result = client.run(
                workflow_key=workflow_key,
                workflow_id=binding.workflow_id,
                # 只有取数凭据、订单与一次性说明进 inputs。模型与 Dify 用户字段都不是授权。
                inputs={
                    "data_grant": token,
                    "order_no": order_no,
                    "question": question,
                    "language": language,
                },
                user=f"aiops:{grant.grant_id}",
                on_started=started,
            )
            if result.workflow_id != binding.workflow_id:
                raise chat_execution.ChatRunUnavailable(
                    "DIFY_WORKFLOW_MISMATCH", "dify ran a different workflow"
                )
            if registration.cancelled:
                self._settle_dify_cancelled(dify_store, grant_store, run_id, qa_id)
                return
            # 先做可能阻塞的外部授权，再做本地判定 —— 与节点 worker 同一条顺序。
            self._require_chat_order_visible(context, order_no)
            answer = self._release_dify_answer(
                run_id=run_id,
                grant_id=grant.grant_id,
                binding=binding,
                context=context,
                order_no=order_no,
                language=language,
                outputs=result.outputs,
            )
            # 写成功之前再核一次绑定：取数已经完成，但运维可能在这一轮模型执行或上面
            # 那次外部授权期间删掉登记、切成 node 引擎、换掉 workflow。不核的话这一轮
            # 会被写成 succeeded/completed 并写入会话答案 —— 而 GET 守卫随后持续拒绝它，
            # 用户既拿不到正文，也拿不到一个正常的失败终态。
            self._require_dify_binding_current(binding)
            if not dify_store.succeed(run_id):
                raise chat_execution.ChatRunUnavailable("DIFY_RUN_NOT_ACTIVE", "dify run is no longer active")
        except chat_execution.ChatRunUnavailable as exc:
            self._settle_dify_failed(dify_store, grant_store, run_id, qa_id, exc.code)
            _LOGGER.warning("chat dify route rejected: %s: %s", exc.code, exc)
            return
        except dify_chat_workflow.DifyWorkflowError as exc:
            self._settle_dify_failed(dify_store, grant_store, run_id, qa_id, exc.code)
            _LOGGER.warning("chat dify route failed: %s: %s", exc.code, exc)
            return
        except Exception as exc:  # noqa: BLE001 — 未预期失败也是失败，不是"换个引擎再试"
            self._settle_dify_failed(dify_store, grant_store, run_id, qa_id, "QA_FAILED")
            _LOGGER.warning("chat dify route failed: %s: %s", type(exc).__name__, exc)
            return
        finally:
            # 完成/失败/取消三条路都撤销取数能力；`revoke_for_qa` 是幂等的。
            with contextlib.suppress(Exception):
                grant_store.revoke_for_qa(qa_id)
        if not self.store.update_assistant_question(qa_id, status="completed", result=answer):
            return
        with contextlib.suppress(Exception):
            self._complete_conversation_turn(
                registration.conversation_turn, question, answer, status_qa_id=qa_id
            )

    def _settle_dify_failed(self, dify_store, grant_store, run_id, qa_id: str, code: str) -> None:
        if run_id is not None:
            with contextlib.suppress(Exception):
                dify_store.fail(run_id, code.lower())
        with contextlib.suppress(Exception):
            grant_store.revoke_for_qa(qa_id)
        self.store.update_assistant_question(qa_id, status="failed", error_code=code)
        registration = self._qa_registrations.get(qa_id)
        if registration is not None:
            self._release_conversation_turn(registration.conversation_turn)

    def settle_retracted_runs(self, *, route: Any) -> dict[str, Any]:
        """收敛撤回时冻结的路由代次；之后发布或恢复的任务不属于本次操作。"""
        from aiops_diagnostics.dify import dify_chat_workflow
        from aiops_diagnostics.dify.dify_run_store import DifyRunStore
        from aiops_diagnostics.execution import chat_data_grant

        store = DifyRunStore(self.store.path)
        grants = chat_data_grant.DataGrantStore(self.store.path)
        settled: list[str] = []
        for run in store.running_for(
            tenant_id=route.tenant_id, environment=route.environment, binding_key=route.key
        ):
            if run.route_revision > route.revision:
                continue
            if not store.cancel(run.run_id, "retracted_by_operator"):
                continue
            grants.revoke_for_qa(run.qa_id)
            registration = self._qa_registrations.get(run.qa_id)
            if registration is not None:
                registration.cancelled = True
            if run.kind in {"trial", "service_trial"}:
                self.store.update_standard_diagnosis(run.qa_id, status="cancelled")
            else:
                self.store.update_assistant_question(run.qa_id, status="cancelled")
                if registration is not None:
                    self._release_conversation_turn(registration.conversation_turn)
            if registration is not None and registration.interrupt is not None:
                self._interrupt_registered_turn(registration)
            elif run.task_id and run.kind == "chat" and run.app_id == route.app_id:
                # 无进程内钩子时只能使用撤回快照的凭据，不能读后来发布的新路由。
                with contextlib.suppress(Exception):
                    dify_chat_workflow.DifyWorkflowClient(self.gateway_settings.dify_chat_base_url).stop(
                        workflow_key=dify_chat_workflow.load_workflow_key(route.workflow_key_ref),
                        task_id=run.task_id,
                        user=f"aiops:{run.grant_id}",
                    )
            settled.append(run.run_id)
        if getattr(route, "engine", "dify") == "node" and self.agent_run_store is not None:
            with self.agent_run_store._connection() as connection:
                rows = connection.execute(
                    "SELECT run_id, qa_id FROM agent_runs WHERE chat_binding=? "
                    "AND route_revision<=? AND state IN ('issued','running')",
                    (route.key, route.revision),
                ).fetchall()
            for row in rows:
                self.agent_run_store.revoke(row["run_id"], "retracted_by_operator")
                if row["qa_id"]:
                    if row["qa_id"].startswith("tr_"):
                        self.store.update_standard_diagnosis(row["qa_id"], status="cancelled")
                    else:
                        self.store.update_assistant_question(row["qa_id"], status="cancelled")
                settled.append(row["run_id"])
        return {"settled_runs": settled}

    def _settle_dify_cancelled(self, dify_store, grant_store, run_id, qa_id: str) -> None:
        with contextlib.suppress(Exception):
            dify_store.cancel(run_id, "cancelled_by_caller")
        with contextlib.suppress(Exception):
            grant_store.revoke_for_qa(qa_id)
        registration = self._qa_registrations.get(qa_id)
        if registration is not None:
            self._release_conversation_turn(registration.conversation_turn)

    def _release_dify_answer(
        self,
        *,
        run_id: str,
        grant_id: str,
        binding: Any,
        context: ScopeContext,
        order_no: str,
        language: str,
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        """把 Dify 的输出适配成原 blocks 形状，并对**本轮快照**做完整性校验。

        事实必须来自本轮取数写入的快照 —— Dify 的文本只用来渲染，不用来声称状态。
        模型写了什么状态码，这里都不采信。
        """
        from aiops_diagnostics.consumer.consumer_order_chat import status_answer
        from aiops_diagnostics.execution import chat_data_grant

        grant = chat_data_grant.usable(chat_data_grant.DataGrantStore(self.store.path).by_id(grant_id))
        if grant.order_no != order_no:
            raise self._chat_unavailable("DIFY_ORDER_MISMATCH", "dify read a different order")
        # 必须真的有**同一轮**的取数记录：图没调取数端点、或者把同订单的旧快照
        # 当成本轮输出，在形状上都可能合法。没有这一条，`succeed()` 会把一次没取过数的
        # 运行写成 completed，而 GET 要等到回读时才 503 —— 那已经不叫"失败落库"了。
        from aiops_diagnostics.dify.dify_run_store import DifyRunStore

        stored = DifyRunStore(self.store.path).snapshot(run_id)
        if stored is None:
            raise self._chat_unavailable("DIFY_NO_DATA", "dify produced no data")
        snapshot = outputs.get("snapshot")
        # Dify 的 HTTP 步骤把响应体当**文本**（`graphon` 的 `HttpResponse.text`），
        # 所以端节点交回来的是 JSON 字符串而不是对象。这里严格解析：解不出对象、
        # 或字段不是恰好那五个，一律按"没有数据"拒绝 —— 不采信任何别的东西。
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except ValueError:
                snapshot = None
        if not isinstance(snapshot, dict):
            # 把 Dify 实际给的 outputs **键名与类型**记下来：只说 "no data" 时，读者无法
            # 区分"端节点没接变量"、"body 是字符串"、"Dify 把对象序列化了"这三种成因。
            _LOGGER.warning(
                "chat dify outputs shape: keys=%s snapshot_type=%s",
                sorted(outputs),
                type(outputs.get("snapshot")).__name__,
            )
            raise self._chat_unavailable("DIFY_NO_DATA", "dify produced no data")
        if snapshot.get("order_no") != order_no:
            raise self._chat_unavailable("DIFY_ORDER_MISMATCH", "dify read a different order")
        # 快照必须**恰好**是那五个字段：多一个就说明有人往里塞了别的来源。
        from aiops_diagnostics.execution import chat_execution as _chat

        if set(snapshot) != _chat.SNAPSHOT_FIELDS:
            raise self._chat_unavailable("DIFY_NO_DATA", "dify data is not a status snapshot")
        # 输出必须就是**本轮落库的那一份**：不是"形状相近的另一份"。
        if {key: str(value) for key, value in snapshot.items()} != {
            key: str(value) for key, value in stored.items()
        }:
            raise self._chat_unavailable("DIFY_SNAPSHOT_MISMATCH", "dify answered from a different read")
        return status_answer(snapshot, language)

    def _require_dify_binding_current(self, binding: Any) -> None:
        """那条绑定**此刻**仍然成立，且仍是签发时那一条。

        `binding` 是签发时冻结下来的那一条 —— 它能证明"当初选的是它"，不能证明
        "现在还能走它"。所以要按 `binding_key` 从**当前**绑定表里重新找回一条，
        并逐字比 workflow。找不到、引擎变了、workflow 换了，一律按不可交付处理。
        """
        from aiops_diagnostics.execution import chat_execution

        try:
            current = chat_execution.binding_for_key(self.gateway_settings, getattr(binding, "key", None))
        except chat_execution.ChatRunUnavailable as exc:
            raise chat_execution.ChatRunUnavailable(exc.code, str(exc)) from exc
        if (
            current is None
            or current.engine != chat_execution.ENGINE_DIFY
            or current.workflow_id != getattr(binding, "workflow_id", None)
            or (getattr(binding, "route_revision", 0) and current.route_revision != binding.route_revision)
            # 版本也要对上：导出的图变了但 workflow 身份没变时（Dify 的保存不换
            # workflow id），光比身份会让旧 worker 把上一版的结果写成成功。
            or (
                int(getattr(binding, "agent_version_no", 0) or 0)
                and int(getattr(binding, "agent_version_no", 0) or 0)
                != int(getattr(current, "agent_version_no", 0) or 0)
            )
        ):
            raise chat_execution.ChatRunUnavailable(
                "CHARGING_STATUS_DISABLED", "dify binding is no longer active"
            )

    @staticmethod
    def _chat_unavailable(code: str, message: str):
        from aiops_diagnostics.execution import chat_execution

        return chat_execution.ChatRunUnavailable(code, message)

    def _require_chat_order_visible(self, context: ScopeContext, order_no: str) -> None:
        """写结果之前再判一次本人订单归属。**判据与 API 层那一条是同一个授权器、同一条规则**。

        三种结局都**不是**"先写着，让 GET 去挡"：

        * 不可见 ⇒ `ORDER_NOT_VISIBLE`，worker 落 failed，不写正文、不写会话答案；
        * 授权源不可用 ⇒ `AUTHORIZATION_UNAVAILABLE`，同样落 failed —— 判不出来就是不能交付，
          不能把"故障"当成"默认可见"；
        * 授权器没注入 ⇒ `AUTHORIZATION_UNAVAILABLE`：fail closed，不猜。
        """
        from aiops_diagnostics.execution import chat_execution

        authorizer = self._order_authorizer
        if authorizer is None:
            raise chat_execution.ChatRunUnavailable(
                "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
            )
        try:
            allowed = authorizer.can_access(context, order_no)
        except Exception as exc:  # noqa: BLE001 — 授权源故障与"不可见"同样是拒绝
            raise chat_execution.ChatRunUnavailable(
                "ORDER_AUTHORIZATION_UNAVAILABLE", "order authorization unavailable", retryable=True
            ) from exc
        if not allowed:
            raise chat_execution.ChatRunUnavailable(
                "ORDER_NOT_VISIBLE", "order not visible to this caller", http_status=404
            )

    def _execute_chat_order_qa(
        self,
        qa_id: str,
        order_no: str,
        context: ScopeContext,
        language: str,
        registration: JobRegistration,
        question: str,
        binding: Any,
    ) -> None:
        """后台：签发 → 绑定 QA↔run → 派发 → 等结果 → 放行 → 写终态。

        **任何**失败都落 `failed`；没有一条分支会去调直读、旧模型或别的节点。

        交付前必须过 `chat_execution.final_release` —— 与委派入口、聊天回读共用同一份
        判据。少了它，「节点交回了快照」就等于「可以交付」，而那两者之间还差着
        撤销、过期、停用、执行配置是否真的核对过。
        """
        from aiops_diagnostics.execution import chat_execution

        store = self.agent_run_store
        if store is None:
            self.store.update_assistant_question(qa_id, status="failed", error_code="AGENT_RUN_DISABLED")
            return
        if registration.cancelled:
            return
        if not self.store.update_assistant_question(qa_id, status="running"):
            return  # 已被取消或已终态：不再派发
        run_id: str | None = None
        try:
            run, config = chat_execution.issue_for_store(
                store=store,
                settings=self.gateway_settings,
                binding=binding,
                caller=context,
                order_no=order_no,
                gateway_version=package_version(),
            )
            run_id = run.run_id
            store.bind_qa(run.run_id, qa_id)
            if registration.cancelled:
                store.revoke(run.run_id, "cancelled_before_dispatch")
                return
            chat_execution.dispatch_run(store=store, config=config, run=run)
            chat_execution.settled_run(store=store, run_id=run.run_id)
            # 先做**可能阻塞的**外部授权，再做本地最终放行 —— `final_release` 的 docstring
            # 明确要求调用方先授权，它自己不做。少了这一步，节点跑完后订单转归他人仍会写
            # `completed` 并存下会话答案；GET 后来拒绝不能算 worker 处理对了。
            self._require_chat_order_visible(context, order_no)
            released = chat_execution.final_release(
                store=store,
                settings=self.gateway_settings,
                caller=context,
                run_id=run.run_id,
            )
            answer = chat_execution.answer_from_run(released, language=language)
        except chat_execution.ChatRunUnavailable as exc:
            if run_id is not None:
                store.fail(run_id, exc.code.lower())
            _LOGGER.warning("chat node route rejected: %s: %s", exc.code, exc)
            self.store.update_assistant_question(qa_id, status="failed", error_code=exc.code)
            self._release_conversation_turn(registration.conversation_turn)
            return
        except Exception as exc:  # noqa: BLE001 — 未预期失败也必须是失败，不是"再试直读"
            if run_id is not None:
                store.fail(run_id, "unexpected_error")
            self.store.update_assistant_question(qa_id, status="failed", error_code="QA_FAILED")
            self._release_conversation_turn(registration.conversation_turn)
            _LOGGER.warning("chat node route failed: %s: %s", type(exc).__name__, exc)
            return
        # 迟到结果不能覆盖取消：claim-guard 会拒绝这次写，这里只是不假装写成功。
        if not self.store.update_assistant_question(qa_id, status="completed", result=answer):
            return
        with contextlib.suppress(Exception):
            self._complete_conversation_turn(
                registration.conversation_turn, question, answer, status_qa_id=qa_id
            )

    def classify_consumer(
        self, question, *, language, previous_question="", previous_order=False, selected_order=False
    ):
        from aiops_diagnostics.consumer.consumer_intent import classify
        from aiops_diagnostics.model_request import resolve_provider_api_key

        settings = Settings.from_config(self.gateway_settings.server_config_file)
        provider = settings.agent.select_provider(None)
        return classify(
            question,
            provider=provider,
            api_key=resolve_provider_api_key(settings.agent, provider=provider),
            language=language,
            previous_question=previous_question,
            previous_order=previous_order,
            selected_order=selected_order,
        )

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
            # APP-RUNTIME-01：节点路线把等待放在后台，取消必须**同时**撤销已关联的 run，
            # 否则节点会继续跑完并把结果交回来。撤销是幂等的。
            if accepted and self.agent_run_store is not None:
                linked = self.agent_run_store.by_qa(qa_id)
                if linked is not None:
                    with contextlib.suppress(Exception):
                        self.agent_run_store.revoke(linked.run_id, "cancelled_by_caller")
            # APP-RUNTIME-02：Dify 路线同理，而且要多撤一步 —— 那条链路上的取数凭据
            # 是**这次 QA 的**，取消之后它必须立刻失效；执行记录也要落 cancelled，
            # 否则 worker 会在 Dify 回来之后把一条已经取消的回答放出去。
            if accepted and self.dify_run_store is not None:
                from aiops_diagnostics.execution import chat_data_grant

                linked_dify = self.dify_run_store.by_qa(qa_id)
                if linked_dify is not None:
                    with contextlib.suppress(Exception):
                        self.dify_run_store.cancel(linked_dify.run_id, "cancelled_by_caller")
                with contextlib.suppress(Exception):
                    chat_data_grant.DataGrantStore(self.store.path).revoke_for_qa(qa_id)
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

    def _complete_conversation_turn(
        self,
        conversation_turn: tuple[str, str, int] | None,
        question: str,
        answer: dict[str, Any] | None,
        *,
        cancelled: bool = False,
        guarded: bool = False,
        stop_renewer: threading.Event | None = None,
        status_qa_id: str | None = None,
    ) -> None:
        """Write a finished answer into the conversation turn (if any).

        ``status_qa_id`` 是**个人订单状态**那条分支的关联。直读路线一直带着它，
        节点路线起初漏了 —— 后果不是显示问题：会话详情按这个链接重新授权，
        下一轮模型上下文也按它排除状态答案。少了它，清空绑定或撤权挡住了 QA 端点，
        同一个答案仍能从会话详情读到，还会被当模型上下文复用。

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
                status_qa_id=status_qa_id,
            )

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
        # `except Exception`, for the reason written out in `_execute_operations_card`
        # (#700 → #721 → #725): an escaping exception means no terminal row at all,
        # so the job sits on `running` until the deadline. Three passes adding one
        # class at a time left `ScopeError` (a `RuntimeError`, raised by
        # `resolve_query_scope` inside this very `try`) in neither list.
        #
        # The message must be safe to RETURN, not merely redacted. This surface
        # hands the stored `error_message` to the caller verbatim
        # (`_health_job_response`), so "redacted" is not enough — `redact_text`
        # removes PII and leaves internal diagnostics in place, which is the
        # right trade for a record an engineer reads and the wrong one for a
        # response body. The two original classes therefore keep their existing
        # "Class: message" shape (unchanged behaviour), and a newly-caught one
        # contributes its CLASS NAME only — no upstream SDK text, no socket
        # error, no internal wording. The code stays `SOURCE_UNAVAILABLE` so an
        # engineer can still tell which failure it was, and the specific text is
        # still in the worker's log.
        #
        # (Pre-existing and NOT made worse here: a source failure that carries
        # detail in `str(exc)` was returned verbatim before this change too.
        # Fixing that means changing this surface's message contract for an
        # already-shipped code, which is a separate decision.)
        except Exception as exc:  # noqa: BLE001 - see above: escaping here means no terminal row
            self._finish_health_job(
                job_id,
                status="failed",
                error_code="SOURCE_UNAVAILABLE",
                error_message=(
                    f"{exc.__class__.__name__}: {exc}"
                    if isinstance(exc, (SourceError, ValueError))
                    else exc.__class__.__name__
                ),
            )

    def _registered_agent(self, tenant_id: str, business_entry: str) -> tuple[str | None, bool]:
        """The runtime registry's answer for this pair: ``(agent_name, configured)``.

        A store failure reads as "not configured", which keeps the pre-registry
        rule — the same degrade direction ``select_customer_agent`` already uses
        when it cannot read the agent store. The opposite direction (a read
        failure meaning "configured, nothing mapped") would turn a database
        hiccup into a blank answer for every tenant at once.
        """
        try:
            registry = self.dify_app_registry
            if not registry.is_configured():
                return None, False
            binding = registry.lookup(tenant_id, business_entry or "")
        except Exception:  # noqa: BLE001 - unreadable registry keeps the old rule
            return None, False
        return (binding.agent_name if binding is not None else None), True

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
            body = (
                bound.fetch_media(grant)
                if range_header is None
                else bound.fetch_media(grant, range_header=range_header)
            )
            # WF01素材取回期间可能撤回或过期，读取前的检查不能覆盖这段等待。
            if grant.agent_version.startswith("consumer:") and not _is_active(grant):
                raise MediaAccessDenied("consumer media is no longer active")
            return body

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

            The rule is about the AGENT, not about which version is newest:

              * the agent is still published (a disabled agent invalidates its
                URLs immediately), and
              * the version that signed the grant still exists and is bound to
                the knowledge base the grant cites.

            It deliberately does NOT require the signing version to still be the
            agent's CURRENT ``published_version``. That extra condition was here
            and it broke every promotional image and video on 41 from
            2026-09-20 onward: the shortcut pins `canary-宣传案例#v2`, the agent
            was republished as v3 that day, and `3 != 2` made every URL a 403 —
            so media was produced and never once displayable. Publishing a
            TUNED prompt must not 403 the images the previous version already
            produced; nothing about that republish makes those bytes wrong.

            The cost of dropping it is explicit: an image or video signed by an
            older-but-still-present version keeps working for the rest of its
            short TTL (~10 minutes) even after a newer version is published.
            That is the correct trade — the URL expires on its own, and the
            alternative is a product surface that silently shows nothing.
            Disabling the agent still cuts URLs off immediately, which is the
            revocation the acceptance criterion actually asked for.

            The knowledge-base binding is checked against BOTH the signing
            version and the agent's CURRENT published version. Checking only the
            historical snapshot would keep serving media after a republish that
            REMOVED that knowledge base — the bytes would stay reachable for the
            rest of the TTL even though the operator just took that library away
            from this agent. Requiring the current binding too keeps "the agent
            may still serve this library" true at the moment of the fetch, while
            still allowing a plain prompt/keyword republish (which is what broke
            on 41) because that leaves the binding intact.

            A grant signed by an **agent-less** caller (``agent_version`` that is
            not ``<agent_id>#v<n>``, which is what the Dify knowledge adapter
            issues) has no agent to be live or dead. Its liveness is the
            registry row it was signed from, which the retrieval route already
            resolved and which is re-read on every retrieval — treating "no
            agent" as "not live" made /v1/dify/retrieval hand Dify a signed URL
            that 403'd on the very next request, so every image and video in a
            Dify record was undisplayable (found on 41, 2026-10-21).
            """
            if grant.agent_version.startswith("consumer:"):
                run = self.dify_run_store.by_qa(grant.session_id or "")
                if run is None or run.kind != "service":
                    return False
                from aiops_diagnostics.consumer.consumer_chat_service import require_result

                try:
                    caller = scope_from_json(run.scope_json)
                    qa = self.store.get_assistant_question(run.qa_id, run.scope_fingerprint)
                    if (
                        qa is None
                        or qa["status"] != "completed"
                        or run.order_no
                        or not tenant_matches(grant.tenant_id, run.tenant_id)
                    ):
                        return False
                    require_result(self, caller, qa)
                    snapshot = self.dify_run_store.snapshot(run.run_id) or {}
                    return any(
                        block.get("media", {}).get("resource_id") == grant.resource_id
                        for block in snapshot.get("blocks", [])
                    )
                except Exception:
                    return False
            if self.agent_store is None:
                return False
            agent_id, _, version_part = str(grant.agent_version).partition("#v")
            if not agent_id or not version_part.isdigit():
                return str(grant.agent_version).startswith("dify:")
            try:
                published = self.agent_store.get(agent_id, grant.tenant_id)
                version = self.agent_store.version(agent_id, grant.tenant_id, int(version_part))
                current = self.agent_store.version(agent_id, grant.tenant_id, published.published_version)
            except Exception:
                return False
            if published.status != "published":
                return False
            signed_kbs = tuple(version.snapshot.get("knowledge_base_ids") or ())
            current_kbs = tuple(current.snapshot.get("knowledge_base_ids") or ())
            return grant.knowledge_base_id in signed_kbs and grant.knowledge_base_id in current_kbs

        try:
            return self._media_proxy.serve_signed(signed_id, range_header=range_header, is_active=_is_active)
        except KnowledgeSearchUnavailable:
            return MediaResponse(503, {"Cache-Control": "no-store"}, b"")


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


def _intersect_sites(
    requested: tuple[str, ...] | None,
    allowed: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """The site set the job actually reads: what was asked for ∩ what is visible.

    The three-valued convention is ``sources._bounded_sites``'s, because that is
    what the aggregate layer downstream actually consumes: ``site_ids is None``
    means "a site predicate is not needed here" (the tenant predicate still
    narrows), while ``()`` means "the intersection was empty, read nothing".
    ``None`` on either side means "no narrowing at that layer", so:

    * both ``None`` → ``None``; a tenant-wide account asks for nothing narrower
      than its whole tenant. Returning ``()`` here instead turned every
      whole-tenant run into a ``SCOPE_EMPTY`` card that counted no orders at
      all — the account the feature is *for* was the one account it never
      answered (found in the review of #721).
    * either side ``None``, the other a tuple → that tuple (the ``None`` side
      narrows nothing).
    * both tuples → the intersection, which may be legitimately empty.

    The intersection is computed here as well as ANDed into the SQL on purpose
    (defence in depth, matching how the diagnosis line carries one range
    object): a mistranslated scope must not be the only thing keeping a request
    inside its tenant.
    """
    if requested is None:
        return None if allowed is None else tuple(allowed)
    if allowed is None:
        return tuple(requested)
    return tuple(sorted(set(requested) & set(allowed)))


def _db_window_bound(value: str) -> str:
    """The timestamp string the SQL compares against, normalised to UTC.

    The store already parsed and validated it; normalising again here keeps the
    string the database sees independent of the offset a caller happened to
    write (``+08:00`` and the equivalent ``Z`` must select the same rows).
    """
    return _as_datetime(value).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


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


def close_gateway_runtime(runtime: GatewayRuntime) -> None:
    with contextlib.suppress(Exception):
        runtime.shutdown()


def _tenant_wide_context(tenant_id: str) -> ScopeContext:
    """固定调试租户的**顶层账号**范围：整个租户可见，不受站点维度约束。

    与 ``agent_manifest.admin_context`` 的区别是数据范围：那个是 ``self``（不碰订单），
    这个是 ``all``。合成身份的字段照抄那条既有约定（``b_user_id="aiops-admin"``），
    为的是审计里能看出"这不是一个真实公司用户"。
    """
    subject = SubjectRecord(b_user_id="aiops-admin", tenant_id=tenant_id)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant_id,
        data_scope=DataScope(type=SCOPE_TYPE_ALL),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )
